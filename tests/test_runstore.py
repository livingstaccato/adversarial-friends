from pathlib import Path

import pytest

from adversarial_friends import runstore as runstore_module
from adversarial_friends.errors import UsageError
from adversarial_friends.outcomes import MAX_JSON_NODES
from adversarial_friends.runstore import RunStore


def test_layout_is_created(tmp_path):
    store = RunStore(tmp_path, "run-001")
    raw, parsed, meta = store.friend_paths(1, "codex-ops")
    assert raw.parent == store.round_dir(1)
    assert raw.name.endswith(".raw")
    assert parsed.name.endswith(".json")
    assert meta.name.endswith(".meta")


def test_friend_err_path_sits_next_to_the_other_friend_files(tmp_path):
    """I1 (whole-branch review): SpawnResult.stderr was captured but written
    nowhere. A separate method (not a 4th element of friend_paths' tuple)
    so `raw, parsed, meta = store.friend_paths(...)` keeps unpacking
    exactly 3 values everywhere it already does."""
    store = RunStore(tmp_path, "run-001")
    err = store.friend_err_path(1, "codex-ops")
    assert err.parent == store.round_dir(1)
    assert err.name == "codex-ops.err"


def test_friend_err_path_rejects_a_name_that_would_escape_the_run_dir(tmp_path):
    store = RunStore(tmp_path, "run-001")
    with pytest.raises(UsageError):
        store.friend_err_path(1, "../../../../tmp/owned")


def test_artifact_is_frozen_and_hashed(tmp_path):
    src = tmp_path / "spec.md"
    src.write_text("# spec\n")
    store = RunStore(tmp_path / "runs", "run-001")
    copied, digest = store.artifact_copy(src)
    assert copied.read_text() == "# spec\n"
    assert digest.startswith("sha256:")


def test_friend_name_cannot_escape_the_run_dir(tmp_path):
    store = RunStore(tmp_path, "run-001")
    with pytest.raises(UsageError):
        store.friend_paths(1, "../../../../tmp/owned")


def test_run_json_is_written(tmp_path):
    store = RunStore(tmp_path, "run-001")
    store.write_run_json({"mode": "report"})
    assert '"mode": "report"' in (store.run_dir / "run.json").read_text()


def test_oversized_checkpoint_metadata_is_refused_without_replacing_prior_state(tmp_path):
    store = RunStore(tmp_path, "run-bounded-checkpoint")
    store.write_run_json({"state": "before"})
    run_json = store.run_dir / "run.json"
    before = run_json.read_bytes()

    with pytest.raises(ValueError, match="expanded JSON node count"):
        store.write_run_json({"payload": [None] * MAX_JSON_NODES})

    assert run_json.read_bytes() == before


# --- Adversarial additions beyond the brief's four required tests --------


def test_reusing_a_run_id_fails_cleanly_instead_of_mixing_ledgers(tmp_path):
    """A second RunStore constructed with the same (root, run_id) must not
    silently reuse -- and thereby mix its ledger/round outputs into -- a
    directory a previous run already populated. `mkdir(..., exist_ok=True)`
    would do exactly that silently; this must raise instead."""
    RunStore(tmp_path, "run-001")
    with pytest.raises(UsageError):
        RunStore(tmp_path, "run-001")


def test_friend_paths_rejects_a_name_with_a_path_separator(tmp_path):
    """A friend name containing a literal '/' passes no farther than
    ids.validate_friend_name -- confirms the escape check is not
    accidentally dead code because validate_friend_name already rejects
    every case contain_path would also catch."""
    store = RunStore(tmp_path, "run-001")
    with pytest.raises(UsageError):
        store.friend_paths(1, "sub/dir")


def test_two_rounds_get_independent_directories(tmp_path):
    store = RunStore(tmp_path, "run-001")
    first = store.round_dir(1)
    second = store.round_dir(2)
    assert first != second
    assert first.exists() and second.exists()


def test_write_report_roundtrips(tmp_path):
    store = RunStore(tmp_path, "run-001")
    path = store.write_report("# Adversarial review\n")
    assert path.read_text() == "# Adversarial review\n"


def _waiting_artifacts(store):
    store.write_run_json({"lifecycle_state": "waiting-for-orchestrator"})
    store.write_report("# waiting\n")
    return (
        (store.run_dir / "run.json").read_bytes(),
        (store.run_dir / "report.md").read_bytes(),
    )


def _assert_waiting_artifacts(store, expected):
    assert (store.run_dir / "run.json").read_bytes() == expected[0]
    assert (store.run_dir / "report.md").read_bytes() == expected[1]
    assert not list(store.run_dir.glob(".*.terminal-*"))


def test_terminal_artifacts_are_replaced_as_one_consistent_pair(tmp_path):
    store = RunStore(tmp_path, "run-terminal")
    _waiting_artifacts(store)

    store.write_terminal_artifacts(
        {"lifecycle_state": "terminal", "exit_code": 0},
        "# terminal\n",
    )

    assert '"lifecycle_state": "terminal"' in (store.run_dir / "run.json").read_text()
    assert (store.run_dir / "report.md").read_text() == "# terminal\n"
    assert not list(store.run_dir.glob(".*.terminal-*"))


def test_oversized_terminal_metadata_is_refused_before_staging(tmp_path):
    store = RunStore(tmp_path, "run-bounded-terminal")
    expected = _waiting_artifacts(store)

    with pytest.raises(ValueError, match="expanded JSON node count"):
        store.write_terminal_artifacts(
            {"payload": [None] * MAX_JSON_NODES},
            "# must not replace waiting report\n",
        )

    _assert_waiting_artifacts(store, expected)


def test_terminal_staging_failure_preserves_both_prior_artifacts(monkeypatch, tmp_path):
    store = RunStore(tmp_path, "run-stage-failure")
    expected = _waiting_artifacts(store)
    original_write = runstore_module.secure_write_text

    def fail_report_stage(path, text, **kwargs):
        if path.name == ".report.md.terminal-new":
            raise OSError("simulated report staging failure")
        return original_write(path, text, **kwargs)

    monkeypatch.setattr(runstore_module, "secure_write_text", fail_report_stage)
    with pytest.raises(OSError, match="report staging"):
        store.write_terminal_artifacts({"lifecycle_state": "terminal"}, "# terminal\n")
    _assert_waiting_artifacts(store, expected)


@pytest.mark.parametrize("failed_target", ["report.md", "run.json"])
def test_terminal_replacement_failure_rolls_back_the_first_replacement(
    monkeypatch, tmp_path, failed_target
):
    store = RunStore(tmp_path, f"run-replace-{failed_target}")
    expected = _waiting_artifacts(store)
    original_replace = runstore_module.secure_replace
    failed = False

    def fail_selected_replace(path, target, *, root):
        nonlocal failed
        if (
            not failed
            and path.name.endswith(".terminal-new")
            and Path(target).name == failed_target
        ):
            failed = True
            raise OSError(f"simulated {failed_target} replacement failure")
        return original_replace(path, target, root=root)

    monkeypatch.setattr(runstore_module, "secure_replace", fail_selected_replace)
    with pytest.raises(OSError, match=rf"{failed_target} replacement"):
        store.write_terminal_artifacts({"lifecycle_state": "terminal"}, "# terminal\n")
    _assert_waiting_artifacts(store, expected)

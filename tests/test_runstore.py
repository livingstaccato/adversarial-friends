import pytest

from adversarial_friends.errors import UsageError
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

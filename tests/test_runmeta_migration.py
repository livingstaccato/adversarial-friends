"""Migration coverage for run.json files written by adversarial-friends 0.2.0."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adversarial_friends.adapters import FriendSpec
from adversarial_friends.authority import DENY_ALL
from adversarial_friends.commands.runmeta import (
    CURRENT_SCHEMA_VERSION,
    _base_meta,
    _validated_roster_entries,
    migrate_meta,
)
from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import Claim, Ledger
from adversarial_friends.snapshots import SnapshotIdentity

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_v020_terminal_meta_is_readable_and_marks_unknowns():
    migrated = migrate_meta(load_fixture("run_meta_v020_terminal.json"))

    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION == 3
    assert migrated["external_tool_policy"] == "legacy-unknown"
    assert migrated["started_at"] is None
    assert migrated["finished_at"] is None
    assert migrated["duration_s"] is None
    assert migrated["exit_code"] is None
    assert migrated["stop_reason"] is None


def test_v020_halt_preserves_budget_tracker_and_snapshot():
    raw = load_fixture("run_meta_v020_halted.json")

    migrated = migrate_meta(raw)

    assert migrated["attempted_calls"] == migrated["spent_calls"] == 4
    assert migrated["repeat_tracker"]["disabled"]
    assert migrated["snapshot"]["commit"] == migrated["snapshot_sha"]
    assert migrated["snapshot_history"] == [migrated["snapshot"]]
    assert migrated["external_tool_policy"] == "legacy-unknown"


@pytest.mark.parametrize("version", [True, False, "2", 0, -1, 4])
def test_invalid_or_unsupported_schema_versions_are_refused(version):
    with pytest.raises(UsageError, match=rf"unsupported run metadata schema {version!r}"):
        migrate_meta({"schema_version": version})


@pytest.mark.parametrize("version", [1, 2, 3])
def test_migration_always_returns_a_deep_copy(version):
    raw = {
        "schema_version": version,
        "invocation": {"friend": ["fake:ops"]},
        "snapshot": {"artifact_path": "artifact/spec.md"},
    }
    before = copy.deepcopy(raw)

    migrated = migrate_meta(raw)
    migrated["invocation"]["friend"].append("fake:security")
    migrated["snapshot"]["artifact_path"] = "changed"

    assert raw == before


def test_legacy_existing_values_are_preserved_instead_of_reconstructed():
    raw = load_fixture("run_meta_v020_terminal.json")
    raw.update(
        {
            "started_at": "recorded-start",
            "finished_at": None,
            "exit_code": 17,
            "external_tool_policy": "recorded-legacy-value",
            "attempted_calls": 9,
            "spent_calls": 4,
            "repeat_tracker": {"last": {}, "count": {}, "disabled": {}},
        }
    )

    migrated = migrate_meta(raw)

    for field in (
        "started_at",
        "finished_at",
        "exit_code",
        "external_tool_policy",
        "attempted_calls",
        "spent_calls",
        "repeat_tracker",
    ):
        assert migrated[field] == raw[field]


def test_an_existing_snapshot_and_history_are_never_replaced():
    raw = load_fixture("run_meta_v020_halted.json")
    existing = {
        "repo_root": None,
        "commit": None,
        "tree": None,
        "artifact_path": "artifact/existing.md",
        "artifact_hash": "sha256:" + "3" * 64,
        "predecessor": None,
    }
    raw["snapshot"] = existing
    raw["snapshot_history"] = [existing, {**existing, "predecessor": existing["artifact_hash"]}]

    migrated = migrate_meta(raw)

    assert migrated["snapshot"] == raw["snapshot"]
    assert migrated["snapshot_history"] == raw["snapshot_history"]


def test_migration_synthesizes_snapshot_only_from_v020_compatibility_keys():
    raw = load_fixture("run_meta_v020_halted.json")

    snapshot = migrate_meta(raw)["snapshot"]

    assert snapshot == {
        "repo_root": raw["repo_root"],
        "commit": raw["snapshot_sha"],
        "tree": None,
        "artifact_path": raw["artifact_path"],
        "artifact_hash": raw["artifact_hash"],
        "predecessor": None,
        "source_path": None,
    }


def test_a_legacy_authority_grant_remains_audit_data():
    migrated = migrate_meta(load_fixture("run_meta_v020_halted.json"))

    assert migrated["invocation"]["allow_unsandboxed_friend"] is True
    assert migrated["invocation"]["i_accept_unsandboxed"] is True
    assert migrated["invocation"]["unsafe_extra_args"] == "--legacy-option"
    assert migrated["invocation"]["pass_env"] == ["LEGACY_TOKEN"]
    assert migrated["external_tool_policy"] == "legacy-unknown"
    assert migrated["external_tool_grants"] == []


def test_legacy_external_tool_allow_migrates_to_global_audit_grant():
    raw = load_fixture("run_meta_v020_halted.json")
    raw["invocation"]["allow_external_tools"] = True

    migrated = migrate_meta(raw)

    assert migrated["external_tool_grants"] == ["*"]
    assert migrated["invocation"]["allow_external_tools"] == ["*"]


def test_legacy_denial_migrates_to_an_empty_audit_grant_set():
    raw = load_fixture("run_meta_v020_terminal.json")
    raw["invocation"]["allow_external_tools"] = False

    migrated = migrate_meta(raw)

    assert migrated["external_tool_grants"] == []


def test_old_possible_host_roster_rows_default_to_non_independent_unknown_role():
    entry = {
        "name": "codex-ops",
        "cli": "codex",
        "lens": "ops",
        "model": None,
        "effort": None,
        "scope": "doc",
        "timeout": 30,
    }

    restored = FriendSpec(**_validated_roster_entries([entry])[0])

    assert restored.independent is False
    assert restored.host_self_review is False


def test_saved_roster_preserves_host_role_audit_fields():
    entry = {
        "name": "codex-ops",
        "cli": "codex",
        "lens": "ops",
        "model": None,
        "effort": None,
        "scope": "doc",
        "timeout": 30,
        "independent": False,
        "host_self_review": True,
    }

    restored = FriendSpec(**_validated_roster_entries([entry])[0])

    assert restored.independent is False
    assert restored.host_self_review is True


@pytest.mark.parametrize("field", ["independent", "host_self_review"])
def test_saved_roster_rejects_non_boolean_host_role_fields(field):
    entry = {
        "name": "codex-ops",
        "cli": "codex",
        "lens": "ops",
        "model": None,
        "effort": None,
        "scope": "doc",
        "timeout": 30,
        field: "false",
    }

    with pytest.raises(UsageError, match=field):
        _validated_roster_entries([entry])


def test_saved_roster_rejects_host_role_claimed_independent():
    entry = {
        "name": "codex-ops",
        "cli": "codex",
        "lens": "ops",
        "model": None,
        "effort": None,
        "scope": "doc",
        "timeout": 30,
        "independent": True,
        "host_self_review": True,
    }

    with pytest.raises(UsageError, match=r"host_self_review.*independent"):
        _validated_roster_entries([entry])


def test_fresh_metadata_freezes_detected_host_and_effective_self_inclusion(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    snapshot = SnapshotIdentity(
        None,
        None,
        None,
        str(artifact),
        "sha256:" + "1" * 64,
    )

    meta = _base_meta(
        SimpleNamespace(mode="report", merge="exact", friend=[]),
        artifact,
        snapshot.artifact_hash,
        [],
        [],
        [],
        snapshot,
        [snapshot],
        DENY_ALL,
        detected_host="codex",
        effective_include_self=True,
    )

    assert meta["detected_host"] == "codex"
    assert meta["effective_include_self"] is True


def _resume_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        resume=str(run_dir),
        out=None,
        artifact=None,
        friend=[],
        allow_external_tools=[],
        allow_unsandboxed_friend=False,
        unsafe_extra_args=None,
        i_accept_unsandboxed=False,
        pass_env=[],
    )


def _run_dir(tmp_path: Path, meta: dict[str, object]) -> Path:
    run_dir = tmp_path / "run-v020"
    run_dir.mkdir()
    round_dir = run_dir / "round-1"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_dir.name,
                "round": 1,
                "question": "merge",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def _resume_meta() -> dict[str, object]:
    meta = load_fixture("run_meta_v020_halted.json")
    meta["invocation"].update(
        {
            "allow_unsandboxed_friend": False,
            "i_accept_unsandboxed": False,
            "unsafe_extra_args": None,
            "pass_env": [],
        }
    )
    return meta


def _legacy_host_resume_meta(mode: str, *, frozen_host: bool) -> dict[str, object]:
    meta = _resume_meta()
    meta["repo_root"] = None
    meta["snapshot_sha"] = None
    meta["invocation"].update(
        {
            "mode": mode,
            "include_self": None,
            "host_provider": None,
        }
    )
    meta["mode"] = mode
    meta["roster"] = [
        {
            "name": "codex-ops",
            "cli": "codex",
            "lens": "ops",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        },
        {
            "name": "fake-security",
            "cli": "fake",
            "lens": "security",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        },
    ]
    meta["friends"] = [
        {
            "name": "codex-ops",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        },
        {
            "name": "fake-security",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        },
    ]
    if frozen_host:
        meta["detected_host"] = "codex"
        meta["effective_include_self"] = True
    return meta


def _legacy_judging_meta(mode: str) -> dict[str, object]:
    meta = _legacy_host_resume_meta(mode, frozen_host=True)
    meta["invocation"]["max_rounds"] = 3
    meta["roster"].append(
        {
            "name": "fake-author",
            "cli": "fake",
            "lens": "author",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        }
    )
    meta["friends"].append(
        {
            "name": "fake-author",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        }
    )
    return meta


def _legacy_claim() -> Claim:
    return Claim(
        id="c-0001@1",
        supersedes=None,
        origin=["fake/author"],
        lens="author",
        round=1,
        advisory=False,
        severity="high",
        claim="unsafe default",
        location="src/app.py:1",
        evidence="the guard is absent",
        failure_scenario="the operation proceeds",
        suggested_fix="add the guard",
    )


def _append_ledger(run_dir: Path, *records: object) -> None:
    ledger = Ledger(run_dir / "claims.jsonl", root=run_dir.parent)
    for record in records:
        ledger.append(record)


def _friend_audit(name: str, round_no: int, status: str) -> dict[str, object]:
    return {
        "name": name,
        "model": None,
        "effort": None,
        "round": round_no,
        "status": status,
    }


def _write_outstanding_request(run_dir: Path, round_no: int) -> None:
    round_dir = run_dir / f"round-{round_no}"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_dir.name,
                "round": round_no,
                "question": "merge",
            }
        ),
        encoding="utf-8",
    )


def _make_second_loop_halt(meta: dict[str, object]) -> None:
    meta["invocation"]["mode"] = "loop"
    meta["mode"] = "loop"
    meta.update(
        {
            "iterations_run": 2,
            "resume_iteration": 2,
            "rounds_run": 4,
        }
    )
    meta["friends"].extend(
        [
            _friend_audit("codex-ops", 4, "ok"),
            _friend_audit("fake-security", 4, "ok"),
            _friend_audit("fake-author", 4, "ok"),
        ]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lifecycle_state", []),
        ("snapshot", []),
        (
            "snapshot",
            {
                "repo_root": [],
                "commit": None,
                "tree": None,
                "artifact_path": "",
                "artifact_hash": "",
                "predecessor": None,
            },
        ),
        ("repeat_tracker", []),
        ("invocation", []),
        ("roster", ["not-an-object"]),
    ],
)
def test_hostile_resume_shapes_are_rejected_before_namespace_construction(
    monkeypatch, tmp_path, field, value
):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    meta[field] = value
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before all saved shapes were validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match=field):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"timeout": 0}, "timeout"),
        ({"max_friends": 0}, "max-friends"),
        ({"max_calls": -1}, "max-calls"),
        ({"require_friends": 0}, "require-friends"),
        ({"max_rounds": 0}, "max-rounds"),
        ({"max_wall_clock": 0}, "max-wall-clock"),
        ({"max_loop_iterations": 0}, "max-loop-iterations"),
        ({"model": "--provider-flag"}, "model"),
        ({"roster": ""}, "roster"),
        ({"roster": "bad\x00path"}, "roster"),
        ({"artifact": ""}, "artifact"),
        ({"artifact": "bad\x00path"}, "artifact"),
        ({"mode": "crossexam", "max_rounds": 1}, "judging round"),
        (
            {"enable_provider": ["codex"], "disable_provider": ["codex"]},
            "both --enable-provider and --disable-provider",
        ),
    ],
)
def test_saved_invocation_semantics_are_rejected_before_namespace(
    monkeypatch, tmp_path, changes, error
):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    meta["invocation"].update(changes)
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before invocation semantics were validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match=error):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("timeout", 0, "timeout"),
        ("model", "--provider-flag", "model"),
        ("name", "../escape", "friend name"),
        ("cli", "", "required key: cli"),
        ("lens", "", "required key: lens"),
        ("scope", "outside", "scope"),
    ],
)
def test_saved_roster_semantics_are_rejected_before_friendspec(
    monkeypatch, tmp_path, field, value, error
):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    meta["roster"][0][field] = value
    run_dir = _run_dir(tmp_path, meta)

    def friendspec_must_not_be_constructed(**_kwargs):
        raise AssertionError("FriendSpec constructed before roster semantics were validated")

    monkeypatch.setattr(runmeta, "FriendSpec", friendspec_must_not_be_constructed)
    with pytest.raises(UsageError, match=error):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize("mode", ["report", "crossexam"])
def test_saved_roster_uniqueness_is_checked_before_friendspec(monkeypatch, tmp_path, mode):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    first = meta["roster"][0]
    second = dict(first)
    if mode == "report":
        second["lens"] = "security"
    else:
        second["name"] = "fake-ops-1"
    meta["roster"] = [first, second]
    meta["invocation"]["mode"] = mode
    run_dir = _run_dir(tmp_path, meta)

    def friendspec_must_not_be_constructed(**_kwargs):
        raise AssertionError("FriendSpec constructed before roster uniqueness was validated")

    monkeypatch.setattr(runmeta, "FriendSpec", friendspec_must_not_be_constructed)
    with pytest.raises(UsageError, match=r"duplicate friend name|same friend"):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spent_calls", -1),
        ("attempted_calls", -1),
        ("iterations_run", -1),
        ("rounds_run", -1),
        ("dry_streak", -1),
        ("resume_iteration", 0),
        ("active_elapsed_s", -1),
        ("required_friends", 0),
    ],
)
def test_checkpoint_numeric_semantics_are_rejected_before_namespace(
    monkeypatch, tmp_path, field, value
):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    meta[field] = value
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before checkpoint semantics were validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match=field):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize("lifecycle", [None, "running", "terminal"])
def test_current_schema_requires_waiting_lifecycle_before_namespace(
    monkeypatch, tmp_path, lifecycle
):
    from adversarial_friends.commands import runmeta

    meta = migrate_meta(_resume_meta())
    if lifecycle is None:
        meta.pop("lifecycle_state", None)
    else:
        meta["lifecycle_state"] = lifecycle
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before lifecycle was validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match="waiting-for-orchestrator"):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize("request_data", [None, {}, {"question": "unknown"}])
def test_legacy_resume_requires_a_valid_outstanding_request_before_namespace(
    monkeypatch, tmp_path, request_data
):
    from adversarial_friends.commands import runmeta

    run_dir = _run_dir(tmp_path, _resume_meta())
    request_path = run_dir / "round-1" / "REQUEST.json"
    if request_data is None:
        request_path.unlink()
    else:
        request_path.write_text(json.dumps(request_data), encoding="utf-8")

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed without a pending legacy halt")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match="outstanding orchestrator halt"):
        runmeta._restore_args(_resume_args(run_dir))


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "repo_root": None,
            "commit": None,
            "tree": None,
            "artifact_path": "",
            "artifact_hash": "",
            "predecessor": None,
        },
        {
            "repo_root": None,
            "commit": None,
            "tree": None,
            "artifact_path": "artifact/spec.md",
            "artifact_hash": "not-a-sha256",
            "predecessor": None,
        },
    ],
)
def test_snapshot_semantics_are_rejected_before_namespace(monkeypatch, tmp_path, snapshot):
    from adversarial_friends.commands import runmeta

    meta = _resume_meta()
    meta["snapshot"] = snapshot
    meta["snapshot_history"] = [snapshot]
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before snapshot semantics were validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match="snapshot"):
        runmeta._restore_args(_resume_args(run_dir))


def test_migration_rejects_deep_metadata_before_copying():
    raw: dict[str, object] = {}
    cursor = raw
    for _ in range(500):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    with pytest.raises(UsageError, match="metadata bound"):
        migrate_meta(raw)

    assert "schema_version" not in raw


def test_migration_rejects_wide_metadata_without_mutating_input():
    values = list(range(9_000))
    raw = {"wide": values}

    with pytest.raises(UsageError, match="metadata bound"):
        migrate_meta(raw)

    assert raw == {"wide": values}


def test_v020_security_grants_must_be_reacknowledged_by_the_current_cli(tmp_path):
    from adversarial_friends.commands.runmeta import _restore_args

    run_dir = _run_dir(tmp_path, load_fixture("run_meta_v020_halted.json"))

    with pytest.raises(UsageError, match="allow-unsandboxed-friend"):
        _restore_args(_resume_args(run_dir))


def test_sparse_legacy_snapshot_history_is_validated_before_namespace(monkeypatch, tmp_path):
    from adversarial_friends.commands import runmeta

    meta = {
        "invocation": {"artifact": "spec.md", "friend": []},
        "roster": [],
        "snapshot": {
            "repo_root": None,
            "commit": None,
            "tree": None,
            "artifact_path": "artifact/spec.md",
            "artifact_hash": "sha256:" + "1" * 64,
            "predecessor": None,
        },
        "snapshot_history": [{"repo_root": []}],
    }
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before snapshot_history validation")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match="snapshot_history"):
        runmeta._restore_args(_resume_args(run_dir))

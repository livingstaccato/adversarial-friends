"""Migration coverage for run.json files written by adversarial-friends 0.2.0."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adversarial_friends.commands.runmeta import CURRENT_SCHEMA_VERSION, migrate_meta
from adversarial_friends.errors import UsageError

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_v020_terminal_meta_is_readable_and_marks_unknowns():
    migrated = migrate_meta(load_fixture("run_meta_v020_terminal.json"))

    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION == 2
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


@pytest.mark.parametrize("version", [True, False, "2", 0, -1, 3])
def test_invalid_or_unsupported_schema_versions_are_refused(version):
    with pytest.raises(UsageError, match=rf"unsupported run metadata schema {version!r}"):
        migrate_meta({"schema_version": version})


@pytest.mark.parametrize("version", [1, 2])
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
    }


def test_a_legacy_authority_grant_remains_audit_data():
    migrated = migrate_meta(load_fixture("run_meta_v020_halted.json"))

    assert migrated["invocation"]["allow_unsandboxed_friend"] is True
    assert migrated["invocation"]["i_accept_unsandboxed"] is True
    assert migrated["invocation"]["unsafe_extra_args"] == "--legacy-option"
    assert migrated["invocation"]["pass_env"] == ["LEGACY_TOKEN"]
    assert migrated["external_tool_policy"] == "legacy-unknown"


def _resume_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        resume=str(run_dir),
        out=None,
        artifact=None,
        friend=[],
        allow_external_tools=False,
        allow_unsandboxed_friend=False,
        unsafe_extra_args=None,
        i_accept_unsandboxed=False,
        pass_env=[],
    )


def _run_dir(tmp_path: Path, meta: dict[str, object]) -> Path:
    run_dir = tmp_path / "run-v020"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


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

    meta = load_fixture("run_meta_v020_halted.json")
    meta["invocation"].update(
        {
            "allow_unsandboxed_friend": False,
            "i_accept_unsandboxed": False,
            "unsafe_extra_args": None,
            "pass_env": [],
        }
    )
    meta[field] = value
    run_dir = _run_dir(tmp_path, meta)

    def namespace_must_not_be_constructed(**_kwargs):
        raise AssertionError("Namespace constructed before all saved shapes were validated")

    monkeypatch.setattr(runmeta.argparse, "Namespace", namespace_must_not_be_constructed)
    with pytest.raises(UsageError, match=field):
        runmeta._restore_args(_resume_args(run_dir))


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
            "artifact_path": "",
            "artifact_hash": "",
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

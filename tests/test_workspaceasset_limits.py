"""Resource and namespace bounds for adapter workspace assets."""

import hashlib
import threading

import pytest

from adversarial_friends import rounds as rounds_mod, workspaceassets
from adversarial_friends.adapters import Adapter, Capability, FriendSpec
from adversarial_friends.authority import ExternalToolPolicy
from adversarial_friends.commands.checkpoint import normalize_friend_rows
from adversarial_friends.errors import UsageError
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult
from adversarial_friends.workspaceassets import (
    WorkspaceAsset,
    parse_workspace_assets,
    stage_workspace_assets,
    validate_workspace_assets,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _entry(target: str, payload: bytes = b"x") -> dict[str, str]:
    return {"source": "payload", "target": target, "sha256": _digest(payload)}


def _adapter(name: str, assets: tuple[WorkspaceAsset, ...] = ()) -> Adapter:
    return Adapter(
        name=name,
        binary=name,
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        workspace_assets=assets,
    )


def _success() -> SpawnResult:
    return SpawnResult(
        argv=["friend"],
        exit_code=0,
        stdout='{"no_findings": true}',
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({"findings": None, "no_findings": True}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )


def _audit(source: str = "payload", target: str = ".friend/payload") -> dict[str, str | None]:
    digest = "a" * 64
    return {
        "source": source,
        "target": target,
        "expected_sha256": digest,
        "observed_sha256": digest,
        "status": "staged",
    }


def test_declarations_reject_more_than_the_bounded_asset_count(tmp_path):
    entries = [
        _entry(f"targets/{index}") for index in range(workspaceassets.MAX_WORKSPACE_ASSETS + 1)
    ]

    with pytest.raises(UsageError, match="count"):
        parse_workspace_assets(entries, transport="exec", source_root=tmp_path)


@pytest.mark.parametrize("field", ["source", "target"])
def test_declarations_bound_path_utf8_bytes_before_source_access(tmp_path, field):
    oversized = "é" * (workspaceassets.MAX_WORKSPACE_ASSET_PATH_BYTES // 2 + 1)
    entry = _entry("target")
    entry[field] = oversized

    with pytest.raises(UsageError, match=r"UTF-8|byte"):
        parse_workspace_assets([entry], transport="exec", source_root=tmp_path)


def test_declarations_bound_their_future_audit_footprint_before_source_access(tmp_path):
    long_source = "s/" * 511 + "s"
    long_target_prefix = "t/" * 500
    entries = [
        _entry(f"{long_target_prefix}{index:04}")
        for index in range(workspaceassets.MAX_WORKSPACE_ASSETS)
    ]
    for entry in entries:
        entry["source"] = long_source

    with pytest.raises(UsageError, match="audit aggregate"):
        parse_workspace_assets(entries, transport="exec", source_root=tmp_path)


@pytest.mark.parametrize(
    "targets",
    [("config", "config/child"), ("config/child", "config")],
)
def test_declarations_reject_ancestor_descendant_target_pairs(tmp_path, targets):
    payload = b"x"
    tmp_path.joinpath("payload").write_bytes(payload)

    with pytest.raises(UsageError, match=r"ancestor|descendant|overlap"):
        parse_workspace_assets(
            [_entry(targets[0], payload), _entry(targets[1], payload)],
            transport="exec",
            source_root=tmp_path,
        )


def test_validation_counts_reused_source_bytes_once_per_target(monkeypatch, tmp_path):
    payload = b"four"
    tmp_path.joinpath("payload").write_bytes(payload)
    assets = (
        WorkspaceAsset("payload", "one/payload", _digest(payload)),
        WorkspaceAsset("payload", "two/payload", _digest(payload)),
    )
    monkeypatch.setattr(workspaceassets, "MAX_WORKSPACE_ASSET_TOTAL_BYTES", 7)

    with pytest.raises(UsageError, match="aggregate"):
        validate_workspace_assets(assets, transport="exec", source_root=tmp_path)


def test_runtime_checks_aggregate_bytes_before_any_target_write(monkeypatch, tmp_path):
    payload = b"four"
    source_root = tmp_path / "assets"
    source_root.mkdir()
    source_root.joinpath("payload").write_bytes(payload)
    isolation = tmp_path / "isolation"
    isolation.mkdir()
    assets = (
        WorkspaceAsset("payload", "one/payload", _digest(payload)),
        WorkspaceAsset("payload", "two/payload", _digest(payload)),
    )
    monkeypatch.setattr(workspaceassets, "MAX_WORKSPACE_ASSET_TOTAL_BYTES", 7)

    with pytest.raises(workspaceassets.WorkspaceAssetStagingError, match="aggregate"):
        stage_workspace_assets(assets, isolation, source_root=source_root)

    assert list(isolation.iterdir()) == []


def _friend_row() -> dict[str, object]:
    return {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "ok",
    }


def test_persisted_audit_bounds_count():
    row = _friend_row()
    row["workspace_assets"] = [
        _audit(target=f"target/{index}")
        for index in range(workspaceassets.MAX_WORKSPACE_ASSETS + 1)
    ]

    with pytest.raises(UsageError, match="count"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_persisted_audit_bounds_path_field_utf8_bytes():
    row = _friend_row()
    row["workspace_assets"] = [
        _audit(source="é" * (workspaceassets.MAX_WORKSPACE_ASSET_PATH_BYTES // 2 + 1))
    ]

    with pytest.raises(UsageError, match=r"UTF-8|byte"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_persisted_audit_bounds_aggregate_bytes():
    row = _friend_row()
    long_source = "s" * workspaceassets.MAX_WORKSPACE_ASSET_PATH_BYTES
    long_target = "t" * workspaceassets.MAX_WORKSPACE_ASSET_PATH_BYTES
    row["workspace_assets"] = [
        _audit(source=long_source, target=long_target)
        for _ in range(workspaceassets.MAX_WORKSPACE_ASSETS)
    ]

    with pytest.raises(UsageError, match="aggregate"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_persisted_audit_aggregate_bound_is_independent(monkeypatch):
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "ok",
    }
    row["workspace_assets"] = [_audit()]
    monkeypatch.setattr(workspaceassets, "MAX_WORKSPACE_ASSET_AUDIT_BYTES", 1)
    with pytest.raises(UsageError, match="aggregate"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_runtime_bound_refuses_only_affected_friend_before_contact(monkeypatch, tmp_path):
    excessive = tuple(
        WorkspaceAsset("payload", f"targets/{index}", _digest(b"x"))
        for index in range(workspaceassets.MAX_WORKSPACE_ASSETS + 1)
    )
    specs = [
        FriendSpec("bad-ops-0", "bad", "ops", None, None, "doc", 30),
        FriendSpec("good-ops-0", "good", "ops", None, None, "doc", 30),
    ]
    store = RunStore(tmp_path, "run-asset-limit")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompts = {}
    for spec in specs:
        prompt = store.friend_prompt_path(1, spec.name)
        prompt.write_text("prompt\n")
        prompts[spec.name] = prompt
    contacted = []

    def fake_dispatch(spec, *_args, **_kwargs):
        contacted.append(spec.name)
        return spec, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY

    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)

    batch = rounds_mod.dispatch_round(
        specs,
        1,
        prompts,
        store,
        {"bad": _adapter("bad", excessive), "good": _adapter("good")},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert contacted == ["good-ops-0"]
    assert batch.error is None
    assert batch.results[0][1].workspace_assets[0].status == "failed-invalid-declaration"
    assert batch.results[1][2].result.succeeded

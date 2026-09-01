"""Digest-pinned adapter assets staged into one friend's isolation."""

import hashlib
import threading

import pytest

from adversarial_friends import rounds as rounds_mod
from adversarial_friends.adapters import Adapter, Capability, FriendSpec
from adversarial_friends.authority import ExternalToolPolicy
from adversarial_friends.commands.checkpoint import normalize_friend_rows
from adversarial_friends.errors import UsageError
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.report import render
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.rounds import persist_result
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult
from adversarial_friends.workspaceassets import (
    WorkspaceAsset,
    WorkspaceAssetAudit,
    WorkspaceAssetStagingError,
    parse_workspace_assets,
    stage_workspace_assets,
    validate_workspace_assets,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _asset(source: str, target: str, payload: bytes) -> WorkspaceAsset:
    return WorkspaceAsset(source=source, target=target, sha256=_digest(payload))


def _adapter(
    name: str,
    assets: tuple[WorkspaceAsset, ...] = (),
    *,
    external_tools: str = "none",
    external_tool_sources: tuple[str, ...] = (),
    deny_external_tools_argv: tuple[str, ...] = (),
) -> Adapter:
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
        external_tools=external_tools,
        external_tool_sources=external_tool_sources,
        deny_external_tools_argv=deny_external_tools_argv,
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


def test_parse_accepts_digest_pinned_relative_assets(tmp_path):
    payload = b"controlled harness\n"
    source_root = tmp_path / "assets"
    (source_root / "harnesses").mkdir(parents=True)
    (source_root / "harnesses" / "friend.md").write_bytes(payload)

    parsed = parse_workspace_assets(
        [
            {
                "source": "harnesses/friend.md",
                "target": ".friend/harness.md",
                "sha256": _digest(payload),
            }
        ],
        transport="exec",
        source_root=source_root,
    )

    assert parsed == (
        WorkspaceAsset(
            source="harnesses/friend.md",
            target=".friend/harness.md",
            sha256=_digest(payload),
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", ""),
        ("source", "/absolute/file"),
        ("source", "harnesses//file"),
        ("source", "harnesses/./file"),
        ("source", "harnesses/../file"),
        ("source", "harnesses\\file"),
        ("target", ""),
        ("target", "/absolute/file"),
        ("target", "."),
        ("target", "../outside"),
        ("target", ".friend//file"),
        ("target", ".friend/./file"),
        ("target", ".friend\\file"),
    ],
)
def test_parse_rejects_noncanonical_or_escaping_paths(tmp_path, field, value):
    payload = b"asset"
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "file").write_bytes(payload)
    entry = {"source": "file", "target": ".friend/file", "sha256": _digest(payload)}
    entry[field] = value

    with pytest.raises(UsageError, match=field):
        parse_workspace_assets([entry], transport="exec", source_root=source_root)


@pytest.mark.parametrize(
    "digest",
    ["", "0" * 63, "0" * 65, "G" * 64, "A" * 64, "sha256:" + "0" * 64],
)
def test_parse_rejects_invalid_sha256(tmp_path, digest):
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "file").write_bytes(b"asset")

    with pytest.raises(UsageError, match="sha256"):
        parse_workspace_assets(
            [{"source": "file", "target": ".friend/file", "sha256": digest}],
            transport="exec",
            source_root=source_root,
        )


def test_parse_rejects_mismatched_digest(tmp_path):
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "file").write_bytes(b"actual")

    with pytest.raises(UsageError, match="digest mismatch"):
        parse_workspace_assets(
            [{"source": "file", "target": ".friend/file", "sha256": "0" * 64}],
            transport="exec",
            source_root=source_root,
        )


def test_parse_rejects_duplicate_targets_but_allows_reused_sources(tmp_path):
    payload = b"asset"
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "file").write_bytes(payload)
    digest = _digest(payload)

    reused = parse_workspace_assets(
        [
            {"source": "file", "target": "one/file", "sha256": digest},
            {"source": "file", "target": "two/file", "sha256": digest},
        ],
        transport="exec",
        source_root=source_root,
    )
    assert len(reused) == 2

    with pytest.raises(UsageError, match=r"duplicate.*target"):
        parse_workspace_assets(
            [
                {"source": "file", "target": "same/file", "sha256": digest},
                {"source": "file", "target": "same/file", "sha256": digest},
            ],
            transport="exec",
            source_root=source_root,
        )


def test_parse_rejects_http_adapter_assets(tmp_path):
    with pytest.raises(UsageError, match="HTTP adapters"):
        parse_workspace_assets(
            [{"source": "file", "target": "file", "sha256": "0" * 64}],
            transport="http",
            source_root=tmp_path,
        )


@pytest.mark.parametrize(
    "shape",
    [
        None,
        {},
        ["not-a-table"],
        [{"source": "file"}],
        [{"source": 1, "target": "file", "sha256": "0" * 64}],
        [{"source": "file", "target": "file", "sha256": 1}],
    ],
)
def test_parse_rejects_malformed_declarations(tmp_path, shape):
    with pytest.raises(UsageError, match="workspace_assets"):
        parse_workspace_assets(shape, transport="exec", source_root=tmp_path)


@pytest.mark.parametrize("target", ["../outside", "/absolute/file", "dir//file"])
def test_runtime_revalidation_refuses_invalid_targets_as_staging_errors(tmp_path, target):
    payload = b"approved"
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "payload").write_bytes(payload)
    isolation = tmp_path / "isolation"
    isolation.mkdir()
    asset = _asset("payload", target, payload)

    with pytest.raises(WorkspaceAssetStagingError) as caught:
        stage_workspace_assets((asset,), isolation, source_root=source_root)

    assert caught.value.audits[0].status == "failed-invalid-declaration"
    assert caught.value.audits[0].target == "invalid"
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("symlink_part", ["leaf", "parent"])
def test_validation_rejects_source_symlinks_and_escape(tmp_path, symlink_part):
    source_root = tmp_path / "assets"
    source_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"outside")
    if symlink_part == "leaf":
        (source_root / "payload").symlink_to(outside / "payload")
        source = "payload"
    else:
        (source_root / "harnesses").symlink_to(outside, target_is_directory=True)
        source = "harnesses/payload"
    asset = _asset(source, ".friend/payload", b"outside")

    with pytest.raises(UsageError, match="source"):
        validate_workspace_assets((asset,), transport="exec", source_root=source_root)


def test_validation_rejects_nonregular_source(tmp_path):
    source_root = tmp_path / "assets"
    (source_root / "directory").mkdir(parents=True)
    asset = _asset("directory", ".friend/payload", b"")

    with pytest.raises(UsageError, match="source"):
        validate_workspace_assets((asset,), transport="exec", source_root=source_root)


def test_staging_revalidates_source_digest_after_adapter_load(tmp_path):
    source_root = tmp_path / "assets"
    source_root.mkdir()
    source = source_root / "payload"
    source.write_bytes(b"approved")
    asset = _asset("payload", ".friend/payload", b"approved")
    validate_workspace_assets((asset,), transport="exec", source_root=source_root)
    source.write_bytes(b"changed")
    isolation = tmp_path / "isolation"
    isolation.mkdir()

    with pytest.raises(WorkspaceAssetStagingError) as caught:
        stage_workspace_assets((asset,), isolation, source_root=source_root)

    audit = caught.value.audits[0]
    assert audit.status == "failed-digest-mismatch"
    assert audit.expected_sha256 == _digest(b"approved")
    assert audit.observed_sha256 == _digest(b"changed")
    assert not (isolation / ".friend" / "payload").exists()


@pytest.mark.parametrize("attack", ["first-parent", "deep-parent", "leaf", "regular-leaf"])
def test_staging_refuses_symlinked_or_preexisting_destinations(tmp_path, attack):
    payload = b"approved"
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "payload").write_bytes(payload)
    isolation = tmp_path / "isolation"
    isolation.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = ".friend/deep/payload"
    if attack == "first-parent":
        (isolation / ".friend").symlink_to(outside, target_is_directory=True)
    else:
        (isolation / ".friend").mkdir()
        if attack == "deep-parent":
            (isolation / ".friend" / "deep").symlink_to(outside, target_is_directory=True)
        else:
            (isolation / ".friend" / "deep").mkdir()
            leaf = isolation / target
            if attack == "leaf":
                leaf.symlink_to(outside / "escaped")
            else:
                leaf.write_bytes(b"caller-owned")
    asset = _asset("payload", target, payload)

    with pytest.raises(WorkspaceAssetStagingError) as caught:
        stage_workspace_assets((asset,), isolation, source_root=source_root)

    assert caught.value.audits[0].status == "failed-target-exists-or-unsafe"
    assert not (outside / "escaped").exists()
    if attack == "regular-leaf":
        assert (isolation / target).read_bytes() == b"caller-owned"


def test_successful_staging_is_relative_to_isolation_and_audited(tmp_path):
    payload = b"approved harness\n"
    source_root = tmp_path / "assets"
    (source_root / "harnesses").mkdir(parents=True)
    (source_root / "harnesses" / "payload").write_bytes(payload)
    isolation = tmp_path / "isolation"
    isolation.mkdir()
    asset = _asset("harnesses/payload", ".friend/deep/payload", payload)

    audits = stage_workspace_assets((asset,), isolation, source_root=source_root)

    assert (isolation / ".friend" / "deep" / "payload").read_bytes() == payload
    assert audits[0].source == "harnesses/payload"
    assert audits[0].target == ".friend/deep/payload"
    assert audits[0].expected_sha256 == _digest(payload)
    assert audits[0].observed_sha256 == _digest(payload)
    assert audits[0].status == "staged"
    assert str(tmp_path) not in repr(audits[0])


def test_no_workspace_assets_stage_nothing(tmp_path):
    isolation = tmp_path / "isolation"
    isolation.mkdir()

    assert stage_workspace_assets((), isolation, source_root=tmp_path / "missing") == ()
    assert list(isolation.iterdir()) == []


def test_persist_result_binds_actual_workspace_asset_outcome_in_sidecar(tmp_path):
    spec = FriendSpec("friend-ops-0", "fake", "ops", None, None, "doc", 30)
    store = RunStore(tmp_path, "run-asset-audit")
    store.write_sensitive(store.friend_prompt_path(1, spec.name), "prompt\n")
    audit = WorkspaceAssetAudit(
        source="harnesses/controlled.md",
        target=".friend/controlled.md",
        expected_sha256="a" * 64,
        observed_sha256="b" * 64,
        status="failed-digest-mismatch",
    )

    row = persist_result(
        store,
        1,
        spec,
        Capability(False, False, "none", workspace_assets=(audit,)),
        _success(),
        "exec",
        None,
    )

    assert row["external_tool_policy"] == "unknown"
    assert row["workspace_assets"] == [audit.as_dict()]
    recovered = rounds_mod.recover_result_audit(store, 1, spec)
    assert recovered["workspace_assets"] == [audit.as_dict()]


def test_persist_result_without_assets_keeps_the_previous_audit_shape(tmp_path):
    spec = FriendSpec("friend-ops-0", "fake", "ops", None, None, "doc", 30)
    store = RunStore(tmp_path, "run-no-assets")
    store.write_sensitive(store.friend_prompt_path(1, spec.name), "prompt\n")

    row = persist_result(
        store,
        1,
        spec,
        Capability(False, False, "none"),
        _success(),
        "exec",
        ExternalToolPolicy.DENY,
    )

    assert "workspace_assets" not in row
    assert "workspace_assets=" not in store.friend_paths(1, spec.name)[2].read_text()
    recovered = rounds_mod.recover_result_audit(store, 1, spec)
    assert "workspace_assets" not in recovered


def test_fake_friend_never_enters_workspace_asset_staging(monkeypatch, tmp_path):
    spec = FriendSpec("fake-ops-0", "fake", "ops", None, None, "doc", 30)
    store = RunStore(tmp_path, "run-fake-hermetic")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompt = store.friend_prompt_path(1, spec.name)
    prompt.write_text("prompt\n")
    monkeypatch.setattr(
        rounds_mod,
        "stage_workspace_assets",
        lambda *_args, **_kwargs: pytest.fail("fake staging must stay hermetic"),
    )
    monkeypatch.setattr(
        rounds_mod,
        "_dispatch",
        lambda dispatched, *_args, **_kwargs: (
            dispatched,
            Capability(False, False, "none"),
            _success(),
            ExternalToolPolicy.DENY,
        ),
    )

    batch = rounds_mod.dispatch_round(
        [spec],
        1,
        {spec.name: prompt},
        store,
        {},
        ["fake"],
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert batch.results[0][2].result.succeeded
    assert batch.results[0][1].workspace_assets == ()


def test_staging_failure_refuses_only_affected_friend_before_dispatch_and_cleans_up(
    monkeypatch, tmp_path
):
    payload = b"approved"
    source_root = tmp_path / "package-assets"
    source_root.mkdir()
    (source_root / "payload").write_bytes(b"changed")
    bad_adapter = _adapter(
        "bad",
        (WorkspaceAsset("payload", ".friend/payload", _digest(payload)),),
        external_tools="deny-argv",
        external_tool_sources=("managed plugins",),
        deny_external_tools_argv=("--disable-plugins",),
    )
    good_adapter = _adapter("good")
    specs = [
        FriendSpec("bad-ops-0", "bad", "ops", None, None, "doc", 30),
        FriendSpec("good-ops-0", "good", "ops", None, None, "doc", 30),
    ]
    store = RunStore(tmp_path, "run-asset-stage-failure")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompts = {}
    for spec in specs:
        prompt = store.friend_prompt_path(1, spec.name)
        prompt.write_text("prompt\n")
        prompts[spec.name] = prompt
    contacted = []
    good_cwd = None

    def fake_dispatch(spec, cwd, *_args, **_kwargs):
        nonlocal good_cwd
        contacted.append(spec.name)
        good_cwd = cwd
        return spec, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY

    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)
    monkeypatch.setattr("adversarial_friends.workspaceassets.assets_root", lambda: source_root)

    batch = rounds_mod.dispatch_round(
        specs,
        1,
        prompts,
        store,
        {"bad": bad_adapter, "good": good_adapter},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert contacted == ["good-ops-0"]
    assert batch.error is None
    assert [result[0].name for result in batch.results] == ["bad-ops-0", "good-ops-0"]
    bad = batch.results[0]
    assert bad[2].failure_reason.startswith("workspace asset staging refused:")
    assert bad[3] is ExternalToolPolicy.DENY
    assert bad[1].external_tools == "denied"
    assert bad[1].external_tool_sources == ("managed plugins",)
    assert bad[1].deny_external_tools_argv == ("--disable-plugins",)
    assert bad[1].workspace_assets[0].status == "failed-digest-mismatch"
    row = persist_result(store, 1, bad[0], bad[1], bad[2], "exec", bad[3])
    assert row["external_tool_policy"] == "deny"
    assert row["external_tools"] == "denied"
    assert row["external_tool_sources"] == ["managed plugins"]
    assert row["deny_external_tools_argv"] == ["--disable-plugins"]
    recovered = rounds_mod.recover_result_audit(store, 1, specs[0])
    assert recovered == row
    assert batch.results[1][2].result.succeeded
    assert good_cwd is not None and not good_cwd.exists()


def test_successful_asset_is_staged_before_dispatch_audited_and_cleaned(monkeypatch, tmp_path):
    payload = b"approved harness\n"
    source_root = tmp_path / "package-assets"
    source_root.mkdir()
    (source_root / "payload").write_bytes(payload)
    adapter = _adapter(
        "friend", (WorkspaceAsset("payload", ".friend/deep/payload", _digest(payload)),)
    )
    spec = FriendSpec("friend-ops-0", "friend", "ops", None, None, "doc", 30)
    store = RunStore(tmp_path, "run-asset-success")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompt = store.friend_prompt_path(1, spec.name)
    prompt.write_text("prompt\n")
    staged_path = None

    def fake_dispatch(dispatched_spec, cwd, *_args, **_kwargs):
        nonlocal staged_path
        staged_path = cwd / ".friend" / "deep" / "payload"
        assert staged_path.read_bytes() == payload
        return (
            dispatched_spec,
            Capability(False, True, "none"),
            _success(),
            ExternalToolPolicy.DENY,
        )

    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)
    monkeypatch.setattr("adversarial_friends.workspaceassets.assets_root", lambda: source_root)

    batch = rounds_mod.dispatch_round(
        [spec],
        1,
        {spec.name: prompt},
        store,
        {"friend": adapter},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert batch.results[0][1].workspace_assets[0].status == "staged"
    assert staged_path is not None and not staged_path.exists()


def test_staged_audit_survives_an_unexpected_dispatch_failure(monkeypatch, tmp_path):
    payload = b"approved harness\n"
    source_root = tmp_path / "package-assets"
    source_root.mkdir()
    (source_root / "payload").write_bytes(payload)
    adapter = _adapter("friend", (WorkspaceAsset("payload", ".friend/payload", _digest(payload)),))
    spec = FriendSpec("friend-ops-0", "friend", "ops", None, None, "doc", 30)
    store = RunStore(tmp_path, "run-asset-dispatch-error")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompt = store.friend_prompt_path(1, spec.name)
    prompt.write_text("prompt\n")

    def broken_dispatch(_spec, cwd, *_args, **_kwargs):
        assert (cwd / ".friend" / "payload").read_bytes() == payload
        raise RuntimeError("dispatch broke")

    monkeypatch.setattr(rounds_mod, "_dispatch", broken_dispatch)
    monkeypatch.setattr("adversarial_friends.workspaceassets.assets_root", lambda: source_root)

    batch = rounds_mod.dispatch_round(
        [spec],
        1,
        {spec.name: prompt},
        store,
        {"friend": adapter},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert batch.results[0][1].external_tools == "unknown"
    assert batch.results[0][1].workspace_assets[0].status == "staged"


def test_checkpoint_rejects_unsafe_workspace_asset_audit_paths():
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "failed: staging refused",
        "workspace_assets": [
            {
                "source": "/Users/operator/.ssh/id_ed25519",
                "target": "../checkout/file",
                "expected_sha256": "A" * 64,
                "observed_sha256": None,
                "status": "failed-source-unavailable",
            }
        ],
    }

    with pytest.raises(UsageError, match="workspace_assets"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_report_surfaces_workspace_asset_staging_audit():
    audit = {
        "source": "harnesses/controlled.md",
        "target": ".friend/controlled.md",
        "expected_sha256": "a" * 64,
        "observed_sha256": "b" * 64,
        "status": "failed-digest-mismatch",
    }
    run_meta = {
        "mode": "report",
        "preset": "inherit",
        "artifact": "spec.md",
        "friends": [
            {
                "name": "friend-ops-0",
                "model": None,
                "effort": None,
                "independent": True,
                "host_self_review": False,
                "readonly": True,
                "scope": "doc",
                "status": "failed: workspace asset staging refused",
                "workspace_assets": [audit],
            }
        ],
    }

    output = render(ReviewState(), run_meta)

    assert "## Workspace assets" in output
    assert "harnesses/controlled.md" in output
    assert ".friend/controlled.md" in output
    assert "failed-digest-mismatch" in output
    assert "a" * 64 in output and "b" * 64 in output

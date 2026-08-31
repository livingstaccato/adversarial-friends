import argparse
import dataclasses
import json
from pathlib import Path

import pytest

from adversarial_friends.adapters import Adapter, FriendSpec, build_argv, load_adapters
from adversarial_friends.authority import (
    ExternalToolPolicy,
    PolicyError,
    enforce,
)
from adversarial_friends.commands.runmeta import _restore_args
from adversarial_friends.dispatch import _dispatch
from adversarial_friends.errors import UsageError
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.paths import ADAPTER_DIR
from adversarial_friends.providerconfig import ProviderPolicy
from adversarial_friends.readiness import ReadinessState, assess_all
from adversarial_friends.spawn import SpawnResult


@pytest.fixture
def registry():
    return load_adapters(ADAPTER_DIR)


@pytest.fixture
def files(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this")
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    return prompt, schema


def _spec(name: str) -> FriendSpec:
    return FriendSpec(
        name=f"{name}-ops-0",
        cli=name,
        lens="ops",
        model="qwen3:0.6b" if name == "ollama" else None,
        effort=None,
        scope="doc",
        timeout=30,
    )


def test_allow_is_explicit_and_records_declared_sources(registry):
    adapter = registry["agy"]
    decision = enforce(adapter, ExternalToolPolicy.ALLOW)
    assert decision.status == "explicitly-allowed"
    assert decision.argv == ()
    assert decision.sources == adapter.external_tool_sources


@pytest.mark.parametrize("state", ["unknown", "uncontrolled", "deny-argv", "bogus"])
def test_default_policy_fails_closed_without_a_complete_denial(state):
    adapter = Adapter(
        name="opaque",
        binary="opaque",
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        external_tools=state,
        deny_external_tools_argv=(),
    )
    with pytest.raises(PolicyError, match="opaque cannot deny external tools") as exc:
        enforce(adapter, ExternalToolPolicy.DENY)
    assert "--allow-external-tools" in str(exc.value)


def test_none_authority_needs_no_argv(registry):
    decision = enforce(registry["ollama"], ExternalToolPolicy.DENY)
    assert decision.status == "denied"
    assert decision.argv == ()
    assert decision.sources


@pytest.mark.parametrize("name", ["codex", "claude", "agy", "opencode"])
def test_denial_argv_is_in_an_option_position_or_adapter_is_blocked(registry, files, name):
    adapter = registry[name]
    prompt, schema = files
    if adapter.external_tools == "uncontrolled":
        with pytest.raises(PolicyError, match="cannot deny external tools"):
            build_argv(adapter, _spec(name), prompt, schema, ExternalToolPolicy.DENY)
        return

    argv, _stdin, cap = build_argv(adapter, _spec(name), prompt, schema, ExternalToolPolicy.DENY)
    first_deny = argv.index(adapter.deny_external_tools_argv[0])
    if adapter.prompt_mode == "trailing-arg":
        assert first_deny < len(argv) - 1
    elif adapter.prompt_mode == "flag-value":
        assert first_deny < argv.index(adapter.prompt_flag)
    else:
        assert adapter.deny_external_tools_argv[0] in argv
    assert cap.external_tools == "denied"


def test_every_shipped_transport_explicitly_declares_authority(registry):
    assert registry
    for name, adapter in registry.items():
        assert adapter.external_tools in {"none", "deny-argv", "uncontrolled"}, name
        assert adapter.external_tool_sources, name
        if adapter.external_tools == "deny-argv":
            assert adapter.deny_external_tools_argv, name
        else:
            assert not adapter.deny_external_tools_argv, name


def test_missing_authority_declaration_defaults_to_unknown(tmp_path):
    (tmp_path / "legacy.toml").write_text('name = "legacy"\nbinary = "legacy"\n')
    adapter = load_adapters(tmp_path)["legacy"]
    assert adapter.external_tools == "unknown"
    assert adapter.deny_external_tools_argv == ()
    assert adapter.external_tool_sources == ()


@pytest.mark.parametrize(
    "text",
    [
        'name="bad"\nexternal_tools="none"\ndeny_external_tools_argv=["--x"]\n',
        'name="bad"\nexternal_tools="deny-argv"\ndeny_external_tools_argv=[]\n',
        'name="bad"\nexternal_tools="mystery"\n',
        'name="bad"\nexternal_tools="none"\nexternal_tool_sources="config"\n',
    ],
)
def test_malformed_authority_declarations_are_rejected(tmp_path, text):
    (tmp_path / "bad.toml").write_text(text)
    with pytest.raises(UsageError, match="external"):
        load_adapters(tmp_path)


def test_policy_blocking_happens_before_executable_or_http_probes(registry):
    blocked = dataclasses.replace(
        registry["ollama"], external_tools="uncontrolled", external_tool_sources=("MCP",)
    )
    probes: list[str] = []
    rows = assess_all(
        {"ollama": blocked},
        ProviderPolicy({}),
        which=lambda binary: probes.append(binary) or "/bin/fake",
        probe=lambda endpoint: probes.append(endpoint) or True,
        external_tool_policy=ExternalToolPolicy.DENY,
    )
    assert rows["ollama"].state is ReadinessState.POLICY_BLOCKED
    assert "cannot deny external tools" in rows["ollama"].reason
    assert probes == []


@pytest.mark.parametrize(
    ("name", "extra_args"),
    [
        ("codex", ["--enable", "apps", "-c", 'mcp_servers.evil.command="sh"']),
        ("claude", ["--tools", "Read", "--mcp-config", "evil.json"]),
    ],
)
def test_denial_refuses_unvalidated_argv_that_can_reverse_authority(
    monkeypatch, tmp_path, registry, files, name, extra_args
):
    """Denial flags cannot be audited as effective if later argv may undo them."""
    from adversarial_friends import dispatch

    monkeypatch.setattr(dispatch.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        dispatch,
        "run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("authority-bypassing argv reached dispatch")
        ),
    )
    prompt, schema = files
    with pytest.raises(PolicyError, match="--allow-external-tools"):
        _dispatch(
            _spec(name),
            tmp_path,
            registry,
            None,
            prompt,
            schema,
            extra_args=extra_args,
            external_tool_policy=ExternalToolPolicy.DENY,
        )


def test_allow_policy_keeps_extra_argv_and_reports_explicit_authority(
    monkeypatch, tmp_path, registry, files
):
    from adversarial_friends import dispatch

    captured: list[str] = []

    def run_process(argv, *_args, **_kwargs):
        captured.extend(argv)
        return SpawnResult(
            argv=argv,
            exit_code=0,
            stdout='{"no_findings": true}',
            stderr="",
            duration_s=0.0,
            timed_out=False,
            result=NormalizeResult({"no_findings": True}, [], True),
            failure_reason=None,
            orphans_suspected=False,
        )

    monkeypatch.setattr(dispatch.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(dispatch, "run_process", run_process)
    prompt, schema = files
    _spec_out, capability, _outcome = _dispatch(
        _spec("codex"),
        tmp_path,
        registry,
        None,
        prompt,
        schema,
        extra_args=["--enable", "apps"],
        external_tool_policy=ExternalToolPolicy.ALLOW,
    )
    assert captured[-2:] == ["--enable", "apps"]
    assert capability.external_tools == "explicitly-allowed"
    assert capability.deny_external_tools_argv == ()


SECURITY_GRANTS = {
    "allow_external_tools": True,
    "allow_unsandboxed_friend": True,
    "unsafe_extra_args": "--profile trusted",
    "i_accept_unsandboxed": True,
    "pass_env": ["TOKEN"],
}


def _resume_args(run_dir: Path, **overrides):
    values = dict(
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
    values.update(overrides)
    return argparse.Namespace(**values)


def _write_resume_fixture(
    tmp_path: Path,
    invocation: dict[str, object],
    roster: list[dict[str, object]] | None = None,
) -> Path:
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    meta = {
        "invocation": {"artifact": str(tmp_path / "spec.md"), "friend": [], **invocation},
        "roster": roster or [],
    }
    (tmp_path / "spec.md").write_text("# spec\n")
    (run_dir / "run.json").write_text(json.dumps(meta))
    return run_dir


@pytest.mark.parametrize(("name", "value"), SECURITY_GRANTS.items())
def test_saved_security_grant_never_restores_without_exact_cli_reassertion(tmp_path, name, value):
    run_dir = _write_resume_fixture(tmp_path, {name: value})
    with pytest.raises(UsageError, match=name.replace("_", "-")):
        _restore_args(_resume_args(run_dir))


@pytest.mark.parametrize(("name", "value"), SECURITY_GRANTS.items())
def test_saved_security_grant_may_be_reasserted_exactly(tmp_path, name, value):
    run_dir = _write_resume_fixture(tmp_path, {name: value})
    restored = _restore_args(_resume_args(run_dir, **{name: value}))
    assert getattr(restored, name) == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("allow_external_tools", "yes"),
        ("allow_unsandboxed_friend", 1),
        ("unsafe_extra_args", ["--x"]),
        ("i_accept_unsandboxed", "true"),
        ("pass_env", "TOKEN"),
    ],
)
def test_malicious_saved_security_grant_types_are_usage_errors(tmp_path, name, value):
    run_dir = _write_resume_fixture(tmp_path, {name: value})
    with pytest.raises(UsageError, match=name.replace("_", "-")):
        _restore_args(_resume_args(run_dir))


def test_false_and_empty_saved_grants_do_not_require_reassertion(tmp_path):
    run_dir = _write_resume_fixture(
        tmp_path,
        {
            "allow_external_tools": False,
            "allow_unsandboxed_friend": False,
            "unsafe_extra_args": None,
            "i_accept_unsandboxed": False,
            "pass_env": [],
        },
    )
    restored = _restore_args(_resume_args(run_dir))
    assert restored.allow_external_tools is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("mode", "bogus"),
        ("merge", "bogus"),
        ("preset", "bogus"),
    ],
)
def test_malicious_saved_choices_are_rejected_before_roster_restore(tmp_path, name, value):
    run_dir = _write_resume_fixture(tmp_path, {name: value})
    with pytest.raises(UsageError, match=name):
        _restore_args(_resume_args(run_dir))


def _saved_spec(name: str, lens: str = "ops") -> dict[str, object]:
    return {
        "name": name,
        "cli": "fake",
        "lens": lens,
        "model": None,
        "effort": None,
        "scope": "doc",
        "timeout": 30,
    }


def test_malicious_saved_roster_rejects_duplicate_names(tmp_path):
    run_dir = _write_resume_fixture(
        tmp_path,
        {"mode": "report"},
        [_saved_spec("same", "ops"), _saved_spec("same", "security")],
    )
    with pytest.raises(UsageError, match="duplicate friend name"):
        _restore_args(_resume_args(run_dir))


def test_malicious_saved_crossexam_roster_rejects_duplicate_ledger_identity(tmp_path):
    run_dir = _write_resume_fixture(
        tmp_path,
        {"mode": "crossexam"},
        [_saved_spec("judge-a"), _saved_spec("judge-b")],
    )
    with pytest.raises(UsageError, match="same friend"):
        _restore_args(_resume_args(run_dir))

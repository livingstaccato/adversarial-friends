import dataclasses

import pytest

from afriend.adapters import FriendSpec, load_adapters
from afriend.authority import DENY_ALL, AuthorityPolicy
from afriend.cliargs import build_parser
from afriend.commands import friends as friends_module
from afriend.commands.friends import validate_resume_capabilities
from afriend.errors import UsageError
from afriend.paths import ADAPTER_DIR
from afriend.providerconfig import ProviderPolicy
from afriend.readiness import (
    DenyProbeResult,
    ReadinessState,
    assess_all,
    probe_deny_argv,
)


@pytest.fixture
def registry():
    return load_adapters(ADAPTER_DIR)


def _probe_adapter(registry, name="codex"):
    return dataclasses.replace(
        registry[name],
        deny_external_tools_probe_argv=("--deny", "--help"),
        deny_external_tools_probe_markers=("--deny",),
    )


def _shim(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_frozen_resume_skips_fresh_resolution_but_reprobes_capability(monkeypatch):
    frozen = FriendSpec(
        name="codex-ops",
        cli="codex",
        lens="ops",
        model="gpt-5",
        effort="high",
        scope="repo",
        timeout=900,
    )
    args = build_parser().parse_args(["run", "spec.md", "--mode", "report"])
    args._resume_roster = [frozen]
    args._resume_meta = {"roster_source": None}

    def fresh_resolution_would_probe(*_args, **_kwargs):
        raise AssertionError("resume attempted fresh provider resolution")

    capability_checks = []
    monkeypatch.setattr(friends_module, "resolve_friends", fresh_resolution_would_probe)
    monkeypatch.setattr(
        friends_module,
        "validate_resume_capabilities",
        lambda specs, registry, policy: capability_checks.append((specs, registry, policy)),
    )

    resolved, specs = friends_module.roster_for_run(args, {}, None, [])

    assert specs == [frozen]
    assert resolved.specs == [frozen]
    assert capability_checks == [([frozen], {}, DENY_ALL)]


@pytest.mark.parametrize(
    "probe",
    [
        '["--deny", "run"]',
        '["--deny", "--help\\nmodel prompt"]',
        "[" + ", ".join(['"x"'] * 33) + "]",
        '["' + "x" * 257 + '", "--help"]',
    ],
)
def test_deny_capability_probe_is_bounded_and_cannot_invoke_a_model(tmp_path, probe):
    (tmp_path / "bad.toml").write_text(
        "\n".join(
            [
                'name = "bad"',
                'external_tools = "deny-argv"',
                'deny_external_tools_argv = ["--deny"]',
                f"deny_external_tools_probe_argv = {probe}",
                'deny_external_tools_probe_markers = ["--deny"]',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="capability probe"):
        load_adapters(tmp_path)


def test_deny_capability_probe_markers_are_bounded(tmp_path):
    (tmp_path / "bad.toml").write_text(
        "\n".join(
            [
                'name = "bad"',
                'external_tools = "deny-argv"',
                'deny_external_tools_argv = ["--deny"]',
                'deny_external_tools_probe_argv = ["--deny", "--help"]',
                f'deny_external_tools_probe_markers = ["{"x" * 257}"]',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="capability probe markers"):
        load_adapters(tmp_path)


def test_deny_argv_probe_accepts_only_a_successful_bounded_marker_match(tmp_path, registry):
    adapter = _probe_adapter(registry)
    supported = _shim(tmp_path / "supported", 'printf "%s\\n" "--deny supported"')
    rejected = _shim(tmp_path / "rejected", 'printf "%s\\n" "unknown option" >&2; exit 2')
    hostile = _shim(tmp_path / "hostile", 'printf "%070000d" 0')

    assert probe_deny_argv(adapter, str(supported)).supported
    assert not probe_deny_argv(adapter, str(rejected)).supported
    result = probe_deny_argv(adapter, str(hostile))
    assert not result.supported
    assert len(result.reason) <= 300


def test_deny_argv_probe_times_out_without_model_or_network_fallback(tmp_path, registry):
    adapter = _probe_adapter(registry)
    slow = _shim(tmp_path / "slow", "sleep 2")

    result = probe_deny_argv(adapter, str(slow), timeout_s=0.02)

    assert not result.supported
    assert "timed out" in result.reason


def test_readiness_blocks_rejected_deny_flags_and_caches_process_snapshot(tmp_path, registry):
    adapter = _probe_adapter(registry)
    counter = tmp_path / "count"
    shim = _shim(
        tmp_path / "counter",
        f'printf x >> "{counter}"; printf "%s\\n" "unknown option" >&2; exit 2',
    )

    first = assess_all(
        {"codex": adapter},
        ProviderPolicy({}),
        env={},
        which=lambda _binary: str(shim),
        authority_policy=DENY_ALL,
    )
    second = assess_all(
        {"codex": adapter},
        ProviderPolicy({}),
        env={},
        which=lambda _binary: str(shim),
        authority_policy=DENY_ALL,
    )

    assert first["codex"].state is ReadinessState.POLICY_BLOCKED
    assert second["codex"].state is ReadinessState.POLICY_BLOCKED
    assert counter.read_text() == "x"


def test_disabled_and_host_excluded_automatic_providers_are_not_capability_probed(registry):
    adapter = _probe_adapter(registry)
    probes = []
    disabled_policy = ProviderPolicy(
        {"codex": dataclasses.replace(ProviderPolicy({}).setting("codex"), enabled=False)}
    )

    disabled = assess_all(
        {"codex": adapter},
        disabled_policy,
        which=lambda _binary: "/bin/codex",
        authority_policy=DENY_ALL,
        capability_probe=lambda *_args: probes.append("disabled"),
    )
    hosted = assess_all(
        {"codex": adapter},
        ProviderPolicy({}),
        env={"CODEX_SESSION_ID": "present"},
        which=lambda _binary: "/bin/codex",
        authority_policy=DENY_ALL,
        capability_probe=lambda *_args: probes.append("host"),
    )

    assert disabled["codex"].state is ReadinessState.DISABLED
    assert hosted["codex"].state is ReadinessState.HOST_EXCLUDED
    assert probes == []


def test_explicit_provider_override_still_requires_capability_verification(registry):
    adapter = _probe_adapter(registry)
    probes: list[str] = []
    disabled_policy = ProviderPolicy(
        {"codex": dataclasses.replace(ProviderPolicy({}).setting("codex"), enabled=False)}
    )

    rows = assess_all(
        {"codex": adapter},
        disabled_policy,
        env={"CODEX_SESSION_ID": "present"},
        which=lambda _binary: "/bin/codex",
        authority_policy=DENY_ALL,
        selection_policy=False,
        capability_probe=lambda *_args: (
            probes.append("codex") or DenyProbeResult(False, "unsupported explicit provider")
        ),
    )

    assert rows["codex"].state is ReadinessState.POLICY_BLOCKED
    assert probes == ["codex"]


def test_resume_reprobes_frozen_provider_without_discovery_policy(registry, tmp_path):
    adapter = _probe_adapter(registry)
    shim = _shim(tmp_path / "changed-codex", 'printf "%s\\n" "unknown option"; exit 2')
    probes: list[str] = []

    with pytest.raises(UsageError, match=r"saved provider.*policy-blocked"):
        validate_resume_capabilities(
            [FriendSpec("codex-ops-0", "codex", "ops", None, None, "doc", 30)],
            {"codex": adapter},
            AuthorityPolicy.deny_all(),
            which=lambda _binary: str(shim),
            capability_probe=lambda candidate, executable: (
                probes.append(f"{candidate.name}:{executable}")
                or DenyProbeResult(False, "deny flags disappeared")
            ),
        )

    assert probes == [f"codex:{shim}"]


def test_resume_capability_validation_does_not_rediscover_current_host(monkeypatch, registry):
    from afriend import readiness

    def host_detection_is_forbidden(*_args, **_kwargs):
        raise AssertionError("resume capability validation rediscovered the current host")

    monkeypatch.setattr(readiness, "detect_host", host_detection_is_forbidden)

    validate_resume_capabilities(
        [FriendSpec("codex-ops-0", "codex", "ops", None, None, "doc", 30)],
        {"codex": registry["codex"]},
        AuthorityPolicy.deny_all(),
        which=lambda _binary: "/bin/codex",
        capability_probe=lambda *_args: DenyProbeResult(True, "verified"),
    )

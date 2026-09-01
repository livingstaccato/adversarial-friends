"""Canonical provider readiness assessment for discovery and diagnostics."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading

from . import http_transport
from .adapters import Adapter
from .authority import AuthorityPolicy, ExternalToolPolicy, enforce as enforce_authority
from .errors import UsageError
from .procio import _pump_output
from .providerconfig import ProviderPolicy
from .workspaceassets import validate_workspace_assets

HOST_ENV_MARKERS: dict[str, str] = {
    "CLAUDECODE": "claude",
    "CLAUDE_CODE_SESSION": "claude",
    "CODEX_SESSION_ID": "codex",
    "CODEX_THREAD_ID": "codex",
    "CODEX_SANDBOX": "codex",
    "CODEX_COMPANION_SESSION_ID": "codex",
    "OPENCODE_SERVER_PASSWORD": "opencode",
}
NO_HTTP_DISCOVERY_ENV = "AF_NO_HTTP_DISCOVERY"
DENY_PROBE_TIMEOUT_S = 2.0
DENY_PROBE_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class DenyProbeResult:
    supported: bool
    reason: str


_DENY_PROBE_CACHE: dict[tuple[object, ...], DenyProbeResult] = {}


def _probe_key(adapter: Adapter, executable: str) -> tuple[object, ...]:
    try:
        info = Path(executable).stat()
    except OSError as exc:
        return (adapter.name, executable, "unstatable", type(exc).__name__)
    return (
        adapter.name,
        executable,
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        adapter.deny_external_tools_probe_argv,
        adapter.deny_external_tools_probe_markers,
    )


def _bounded_probe_detail(text: str) -> str:
    compact = " ".join(text.split())
    return compact[-240:] if compact else "no diagnostic output"


def probe_deny_argv(
    adapter: Adapter, executable: str, *, timeout_s: float = DENY_PROBE_TIMEOUT_S
) -> DenyProbeResult:
    """Verify deny flags with a bounded, declarative help/version invocation."""
    key = _probe_key(adapter, executable)
    cached = _DENY_PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    if not adapter.deny_external_tools_probe_argv or not adapter.deny_external_tools_probe_markers:
        result = DenyProbeResult(False, "deny-argv capability probe is not declared")
        _DENY_PROBE_CACHE[key] = result
        return result
    try:
        process = subprocess.Popen(
            [executable, *adapter.deny_external_tools_probe_argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        result = DenyProbeResult(False, f"deny-argv capability probe could not start: {exc}")
        _DENY_PROBE_CACHE[key] = result
        return result
    assert process.stdout is not None and process.stderr is not None
    stop = threading.Event()
    overflow = threading.Event()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    pumps = [
        threading.Thread(
            target=_pump_output,
            args=(stream, chunks, stop, DENY_PROBE_OUTPUT_BYTES // 2, overflow),
            daemon=True,
        )
        for stream, chunks in (
            (process.stdout, stdout_chunks),
            (process.stderr, stderr_chunks),
        )
    ]
    for pump in pumps:
        pump.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()
    finally:
        stop.set()
        for pump in pumps:
            pump.join(3.0)
    output = "".join(stdout_chunks + stderr_chunks)
    if timed_out:
        result = DenyProbeResult(
            False, f"deny-argv capability probe timed out after {timeout_s:g}s"
        )
    elif overflow.is_set():
        result = DenyProbeResult(False, "deny-argv capability probe output exceeded 65536 bytes")
    elif process.returncode != 0:
        result = DenyProbeResult(
            False,
            f"deny-argv capability probe exited {process.returncode}: {_bounded_probe_detail(output)}",
        )
    else:
        missing = [
            marker for marker in adapter.deny_external_tools_probe_markers if marker not in output
        ]
        result = (
            DenyProbeResult(False, f"deny-argv capability probe omitted marker {missing[0]!r}")
            if missing
            else DenyProbeResult(True, "deny-argv capability verified")
        )
    _DENY_PROBE_CACHE[key] = result
    return result


class ReadinessState(StrEnum):
    READY = "ready"
    REACHABLE_UNCONFIGURED = "reachable-unconfigured"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    HOST_EXCLUDED = "host-excluded"
    POLICY_BLOCKED = "policy-blocked"


@dataclass(frozen=True)
class FriendReadiness:
    provider: str
    state: ReadinessState
    reason: str
    where: str
    model: str | None

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY


def detect_host(env: Mapping[str, str], *, host_provider: str | None = None) -> str | None:
    if host_provider:
        return host_provider
    for marker, provider in HOST_ENV_MARKERS.items():
        if env.get(marker):
            return provider
    return None


def can_be_host_provider(provider: object) -> bool:
    """Whether the host-detection contract can identify this real provider.

    Explicit host selection deliberately supports every provider in the
    registry, including providers such as Agy that have no automatic
    environment marker. ``fake`` is test-only and cannot be selected through
    the validated ``--host-provider`` path.
    """
    return (
        type(provider) is str
        and bool(provider)
        and provider != "fake"
        and detect_host({}, host_provider=provider) == provider
    )


def _row(
    provider: str,
    state: ReadinessState,
    reason: str,
    where: str,
    model: str | None,
) -> FriendReadiness:
    return FriendReadiness(provider, state, reason, where, model)


def assess_all(
    registry: Mapping[str, Adapter],
    provider_policy: ProviderPolicy,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str], bool] | None = None,
    include_self: bool = False,
    host_provider: str | None = None,
    enforce: Callable[[Adapter], object] | None = None,
    authority_policy: AuthorityPolicy | None = None,
    selection_policy: bool = True,
    capability_probe: Callable[[Adapter, str], DenyProbeResult] | None = None,
) -> dict[str, FriendReadiness]:
    """Assess providers once, optionally ignoring automatic-selection policy.

    Explicitly named friends set ``selection_policy=False``: naming a friend
    overrides enabled/host/discovery selection, but never availability,
    configuration, adapter validation, or authority.
    """
    environ = os.environ if env is None else env
    probe_fn = http_transport.probe if probe is None else probe
    host = detect_host(environ, host_provider=host_provider) if selection_policy else None
    rows: dict[str, FriendReadiness] = {}

    for name, adapter in sorted(registry.items()):
        setting = provider_policy.setting(name)
        declared_where = adapter.endpoint if adapter.transport == "http" else adapter.binary
        if selection_policy and not setting.enabled:
            rows[name] = _row(
                name,
                ReadinessState.DISABLED,
                "disabled by provider policy",
                declared_where,
                setting.model,
            )
            continue
        if selection_policy and adapter.transport == "http" and environ.get(NO_HTTP_DISCOVERY_ENV):
            rows[name] = _row(
                name,
                ReadinessState.DISABLED,
                f"disabled by {NO_HTTP_DISCOVERY_ENV}",
                adapter.endpoint,
                setting.model,
            )
            continue
        if selection_policy and not include_self and name == host:
            where = declared_where
            if adapter.transport != "http" and adapter.binary:
                where = which(adapter.binary) or declared_where
            rows[name] = _row(
                name,
                ReadinessState.HOST_EXCLUDED,
                "excluded because it is the detected host provider",
                where,
                setting.model,
            )
            continue

        # Authority is decidable from the repository-controlled adapter
        # declaration alone. Refuse before testing an executable or endpoint:
        # a provider the policy forbids must never be contacted as a probe.
        try:
            validate_workspace_assets(
                adapter.workspace_assets,
                transport=adapter.transport,
            )
            if authority_policy is not None:
                enforce_authority(adapter, authority_policy.for_provider(name))
        except UsageError as exc:
            rows[name] = _row(
                name,
                ReadinessState.POLICY_BLOCKED,
                str(exc),
                declared_where,
                setting.model,
            )
            continue

        if adapter.transport == "http":
            endpoint = adapter.endpoint
            if not endpoint or not probe_fn(endpoint):
                rows[name] = _row(
                    name,
                    ReadinessState.UNAVAILABLE,
                    "endpoint is unreachable",
                    endpoint,
                    setting.model,
                )
                continue
            where = endpoint
        else:
            executable = which(adapter.binary) if adapter.binary else None
            if not executable:
                binary = adapter.binary or name
                rows[name] = _row(
                    name,
                    ReadinessState.UNAVAILABLE,
                    f"executable {binary!r} was not found",
                    "",
                    setting.model,
                )
                continue
            where = executable

        if (
            authority_policy is not None
            and authority_policy.for_provider(name) is ExternalToolPolicy.DENY
            and adapter.external_tools == "deny-argv"
        ):
            probe_deny = probe_deny_argv if capability_probe is None else capability_probe
            capability = probe_deny(adapter, where)
            if not capability.supported:
                rows[name] = _row(
                    name,
                    ReadinessState.POLICY_BLOCKED,
                    capability.reason,
                    where,
                    setting.model,
                )
                continue
        if enforce is not None:
            try:
                enforce(adapter)
            except UsageError as exc:
                rows[name] = _row(
                    name,
                    ReadinessState.POLICY_BLOCKED,
                    str(exc),
                    where,
                    setting.model,
                )
                continue
        if adapter.transport == "http" and setting.model is None:
            rows[name] = _row(
                name,
                ReadinessState.REACHABLE_UNCONFIGURED,
                "endpoint is reachable but no model is configured",
                where,
                None,
            )
            continue
        reason = (
            "endpoint is reachable" if adapter.transport == "http" else "executable is available"
        )
        rows[name] = _row(name, ReadinessState.READY, reason, where, setting.model)
    return rows

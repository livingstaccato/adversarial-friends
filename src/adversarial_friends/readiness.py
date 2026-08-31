"""Canonical provider readiness assessment for discovery and diagnostics."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
import shutil

from . import http_transport
from .adapters import Adapter
from .authority import ExternalToolPolicy, enforce as enforce_authority
from .errors import UsageError
from .providerconfig import ProviderPolicy

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
    external_tool_policy: ExternalToolPolicy | None = None,
    selection_policy: bool = True,
) -> dict[str, FriendReadiness]:
    """Assess providers once, optionally ignoring automatic-selection policy.

    Explicitly named friends set ``selection_policy=False``: naming a friend
    overrides enabled/host/discovery selection, but never availability,
    configuration, adapter validation, or authority.
    """
    environ = os.environ if env is None else env
    probe_fn = http_transport.probe if probe is None else probe
    host = detect_host(environ, host_provider=host_provider)
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

        # Authority is decidable from the repository-controlled adapter
        # declaration alone. Refuse before testing an executable or endpoint:
        # a provider the policy forbids must never be contacted as a probe.
        try:
            if external_tool_policy is not None:
                enforce_authority(adapter, external_tool_policy)
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

        if selection_policy and not include_self and name == host:
            rows[name] = _row(
                name,
                ReadinessState.HOST_EXCLUDED,
                "excluded because it is the detected host provider",
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

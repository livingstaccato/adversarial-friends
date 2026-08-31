"""Provider-managed tool authority for every friend dispatch."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .errors import UsageError

if TYPE_CHECKING:
    from .adapters import Adapter


class ExternalToolPolicy(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class AuthorityDecision:
    policy: ExternalToolPolicy
    status: str
    argv: tuple[str, ...]
    sources: tuple[str, ...]
    reason: str = ""


class PolicyError(UsageError):
    """The requested provider cannot meet the run's authority policy."""


def enforce_extra_args(policy: ExternalToolPolicy, extra_args: list[str] | None) -> None:
    """Refuse argv whose authority effect is unknown under a deny policy."""
    if policy is ExternalToolPolicy.DENY and extra_args:
        raise PolicyError(
            "cannot deny external tools while passing unvalidated --unsafe-extra-args; "
            "pass --allow-external-tools to opt in explicitly"
        )


def enforce(adapter: "Adapter", policy: ExternalToolPolicy) -> AuthorityDecision:
    """Decide authority before probing or dispatching an adapter."""
    if policy is ExternalToolPolicy.ALLOW:
        return AuthorityDecision(policy, "explicitly-allowed", (), adapter.external_tool_sources)
    if adapter.external_tools == "none":
        return AuthorityDecision(policy, "denied", (), adapter.external_tool_sources)
    if adapter.external_tools == "deny-argv" and adapter.deny_external_tools_argv:
        return AuthorityDecision(
            policy,
            "denied",
            adapter.deny_external_tools_argv,
            adapter.external_tool_sources,
        )
    reason = (
        f"{adapter.name} cannot deny external tools with this installed adapter; "
        "pass --allow-external-tools to opt in explicitly"
    )
    raise PolicyError(reason)

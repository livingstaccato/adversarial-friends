"""Provider-managed tool authority for every friend dispatch."""

from collections.abc import Iterable
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
class AuthorityPolicy:
    """Run-wide provider grants, normalized for comparison and audit."""

    allowed_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = tuple(self.allowed_providers)
        if not all(isinstance(value, str) and value for value in values):
            raise UsageError("--allow-external-tools provider grants must be nonempty strings")
        if len(values) != len(set(values)):
            raise UsageError("--allow-external-tools contains a duplicate provider grant")
        if "*" in values and values != ("*",):
            raise UsageError("--allow-external-tools='*' must be used alone")
        object.__setattr__(self, "allowed_providers", tuple(sorted(values)))

    @classmethod
    def deny_all(cls) -> "AuthorityPolicy":
        return cls()

    @classmethod
    def from_grants(
        cls, grants: Iterable[str] | None, known_providers: Iterable[str]
    ) -> "AuthorityPolicy":
        values = list(grants or ())
        known = set(known_providers)
        if len(values) != len(set(values)):
            raise UsageError("--allow-external-tools contains a duplicate provider grant")
        if "*" in values and values != ["*"]:
            raise UsageError("--allow-external-tools='*' must be used alone")
        unknown = set(values) - known - {"*"}
        if unknown:
            raise UsageError(
                f"unknown --allow-external-tools provider(s) {sorted(unknown)}; "
                f"known: {sorted(known)}"
            )
        return cls(tuple(sorted(values)))

    @property
    def grants(self) -> tuple[str, ...]:
        return self.allowed_providers

    @property
    def allowed(self) -> tuple[str, ...]:
        return self.allowed_providers

    @property
    def allows_all(self) -> bool:
        return self.allowed_providers == ("*",)

    @property
    def audit_summary(self) -> str:
        if self.allows_all:
            return "allow"
        if self.allowed_providers:
            return "scoped-allow"
        return "deny"

    @property
    def summary(self) -> str:
        return self.audit_summary

    def for_provider(self, name: str) -> ExternalToolPolicy:
        if self.allows_all or name in self.allowed_providers:
            return ExternalToolPolicy.ALLOW
        return ExternalToolPolicy.DENY


DENY_ALL = AuthorityPolicy()


@dataclass(frozen=True)
class AuthorityDecision:
    policy: ExternalToolPolicy
    status: str
    argv: tuple[str, ...]
    sources: tuple[str, ...]
    reason: str = ""


class PolicyError(UsageError):
    """The requested provider cannot meet the run's authority policy."""


def enforce_extra_args(policy: AuthorityPolicy, extra_args: list[str] | None) -> None:
    """Require global authority because extra argv targets every provider."""
    if not policy.allows_all and extra_args:
        raise PolicyError(
            "--unsafe-extra-args targets every friend and requires an explicit global '*' "
            "grant via --allow-external-tools='*'"
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

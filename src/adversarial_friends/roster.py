"""Decide which friends run, on which model, under which lens.

Self-exclusion drops the host's (cli, model) pair rather than the whole
binary. Blanket per-binary exclusion would be wrong: a CLI judging a spec its
own model authored, under a different lens and effort level, is sometimes
exactly what you want.
"""

from collections.abc import Callable, Mapping
from dataclasses import replace
import shutil
from typing import Any

from .adapters import Adapter, FriendSpec
from .authority import AuthorityPolicy, enforce as enforce_authority
from .errors import NoFriendsError, UsageError
from .providerconfig import ProviderPolicy
from .readiness import (
    HOST_ENV_MARKERS as HOST_ENV_MARKERS,
    NO_HTTP_DISCOVERY_ENV as NO_HTTP_DISCOVERY_ENV,
    ReadinessState,
    assess_all,
    detect_host as detect_host,
)
from .trust import validate_roster_entry

# opencode exposes no read-only mode, so it may not read the repository
# without an explicit opt-in from the operator.
NO_READONLY_DEFAULT_SCOPE = "doc"
DEGRADED_MODES = frozenset({"report"})
DEFAULT_TIMEOUT = 900


def apply_capacity(
    specs: list[FriendSpec], max_friends: int | None
) -> tuple[list[FriendSpec], list[FriendSpec]]:
    if max_friends is None:
        return specs, []
    if max_friends <= 0:
        raise UsageError("max_friends must be a positive integer")
    return specs[:max_friends], specs[max_friends:]


# Set to any non-empty value to keep HTTP friends out of auto-discovery
# without stopping the server. `--friend ollama:lens:model` still works --
# this only governs whether a reachable endpoint is *enlisted automatically*.
# Someone running ollama for unrelated reasons should not find it silently
# joining every run.
def discover_clis(
    registry: dict[str, Adapter],
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str], bool] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Legacy projection of the canonical readiness assessment.

    Reachable HTTP providers without a model remain visible here because
    `afriend init` historically used this API to write an editable
    placeholder. Automatic run selection consumes only READY rows directly.
    """
    rows = assess_all(
        registry,
        ProviderPolicy({}),
        env=env,
        which=which,
        probe=probe,
        include_self=True,
    )
    eligible = {ReadinessState.READY, ReadinessState.REACHABLE_UNCONFIGURED}
    return [name for name, row in rows.items() if row.state in eligible]


def resolve(
    registry: dict[str, Adapter],
    lenses: list[str],
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    include_self: bool = False,
    overrides: list[dict[str, Any]] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    probe: Callable[[str], bool] | None = None,
    provider_policy: ProviderPolicy | None = None,
    max_friends: int | None = None,
    host_provider: str | None = None,
    enforce: Callable[[Adapter], object] | None = None,
    authority_policy: AuthorityPolicy | None = None,
) -> list[FriendSpec]:
    # NOTE for whoever wires a --roster file flag through `overrides`:
    # `if overrides:` (not `if overrides is not None:`) means an explicit,
    # caller-supplied *empty* list is indistinguishable from "no overrides
    # given" and silently falls through to full auto-discovery below. If a
    # roster file can legitimately name zero friends, check for that case
    # before calling resolve() and raise NoFriendsError yourself -- do not
    # rely on this function to do it. (Task 12's cli.py never triggers this
    # at all: its --friend flag path builds FriendSpecs directly and never
    # calls resolve(overrides=...) -- see cli._specs_from_flags's own
    # docstring.)
    override_specs: list[FriendSpec] | None = None
    if overrides:
        override_specs = []
        seen_names: set[str] = set()
        for _index, entry in enumerate(overrides):
            validate_roster_entry(entry)
            name = entry["name"]
            if name in seen_names:
                # Friend names become path components under the run directory
                # (see ids.py); two entries sharing a name would silently
                # clobber each other's output instead of raising.
                raise UsageError(
                    f"duplicate friend name {name!r} in roster overrides: "
                    "names must be unique because they become output paths"
                )
            seen_names.add(name)
            adapter = registry.get(entry["cli"])
            if adapter is None:
                # NOTE for whoever wires a --roster file flag through
                # `overrides`: this raises NoFriendsError (exit 3) for an
                # unknown cli, but a config typo is a usage error, not "no
                # friends available" -- UsageError (exit 2) fits better.
                # Left unchanged here since fixing it would change this
                # function's behavior for existing callers/tests; Task 12's
                # own --friend flag path (cli._specs_from_flags) raises
                # UsageError directly instead of going through this branch
                # at all, for exactly this reason.
                raise NoFriendsError(f"unknown cli in roster: {entry['cli']!r}")
            default_scope = "repo" if adapter.readonly_argv else NO_READONLY_DEFAULT_SCOPE
            override_specs.append(
                FriendSpec(
                    name=name,
                    cli=entry["cli"],
                    lens=entry["lens"],
                    model=entry.get("model"),
                    effort=entry.get("effort"),
                    scope=entry.get("scope", default_scope),
                    timeout=entry.get("timeout", timeout),
                )
            )

    # An explicitly named roster is still subject to provider authority.
    # Decide that from declarations before readiness performs any executable
    # or endpoint probes, and surface PolicyError directly rather than
    # degrading a security refusal into a generic "no friends" outcome.
    if override_specs is not None and authority_policy is not None:
        for spec in override_specs:
            enforce_authority(registry[spec.cli], authority_policy.for_provider(spec.cli))

    readiness = assess_all(
        registry,
        provider_policy or ProviderPolicy({}),
        env=env,
        which=which,
        probe=probe,
        include_self=include_self,
        host_provider=host_provider,
        enforce=enforce,
        authority_policy=authority_policy,
    )
    if override_specs is not None:
        specs = []
        rejected = []
        for spec in override_specs:
            row = readiness[spec.cli]
            roster_model_makes_ready = (
                row.state is ReadinessState.REACHABLE_UNCONFIGURED and spec.model is not None
            )
            if not row.ready and not roster_model_makes_ready:
                rejected.append(f"{spec.name} ({spec.cli}): {row.reason}")
                continue
            specs.append(replace(spec, model=spec.model or row.model))
        if not specs:
            raise NoFriendsError(
                "no usable friends from roster after readiness filtering: " + "; ".join(rejected)
            )
        selected, _dropped = apply_capacity(specs, max_friends)
        return selected

    available = [name for name, row in readiness.items() if row.ready]
    if not available:
        raise NoFriendsError(
            "no usable friends found. Install a second agent CLI "
            "(codex, agy, opencode) or pass --include-self."
        )
    if not lenses:
        # available is non-empty here, so lenses[index % len(lenses)] below
        # would otherwise raise ZeroDivisionError instead of a clean,
        # actionable error.
        raise UsageError(
            "no lenses configured: at least one lens is required to assign to discovered friends."
        )

    specs = []
    for index, cli in enumerate(available):
        adapter = registry[cli]
        scope = "repo" if adapter.readonly_argv else NO_READONLY_DEFAULT_SCOPE
        specs.append(
            FriendSpec(
                name=f"{cli}-{lenses[index % len(lenses)]}",
                cli=cli,
                lens=lenses[index % len(lenses)],
                model=readiness[cli].model,
                effort=None,
                scope=scope,
                timeout=timeout,
            )
        )
    selected, _dropped = apply_capacity(specs, max_friends)
    return selected

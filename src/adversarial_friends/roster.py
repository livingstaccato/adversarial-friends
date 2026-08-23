"""Decide which friends run, on which model, under which lens.

Self-exclusion drops the host's (cli, model) pair rather than the whole
binary. Blanket per-binary exclusion would be wrong: a CLI judging a spec its
own model authored, under a different lens and effort level, is sometimes
exactly what you want.
"""

from collections.abc import Callable, Mapping
import shutil
from typing import Any

from .adapters import Adapter, FriendSpec
from .errors import NoFriendsError, UsageError
from .trust import validate_roster_entry

HOST_ENV_MARKERS: dict[str, str] = {
    "CLAUDECODE": "claude",
    "CLAUDE_CODE_SESSION": "claude",
    "CODEX_SANDBOX": "codex",
    "CODEX_COMPANION_SESSION_ID": "codex",
    "OPENCODE_SERVER_PASSWORD": "opencode",
}

# opencode exposes no read-only mode, so it may not read the repository
# without an explicit opt-in from the operator.
NO_READONLY_DEFAULT_SCOPE = "doc"
DEGRADED_MODES = frozenset({"report"})
DEFAULT_TIMEOUT = 900


def detect_host(env: Mapping[str, str]) -> str | None:
    for marker, cli in HOST_ENV_MARKERS.items():
        if env.get(marker):
            return cli
    return None


def discover_clis(
    registry: dict[str, Adapter], which: Callable[[str], str | None] = shutil.which
) -> list[str]:
    found = []
    for name, adapter in sorted(registry.items()):
        if adapter.transport == "http":
            continue  # reachability is probed separately, not via PATH
        if adapter.binary and which(adapter.binary):
            found.append(name)
    return found


def resolve(
    registry: dict[str, Adapter],
    lenses: list[str],
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    include_self: bool = False,
    overrides: list[dict[str, Any]] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
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
    if overrides:
        specs = []
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
            specs.append(
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
        return specs

    host = detect_host(env)
    available = discover_clis(registry, which)
    if not include_self and host in available:
        available = [c for c in available if c != host]
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
                model=None,
                effort=None,
                scope=scope,
                timeout=timeout,
            )
        )
    return specs

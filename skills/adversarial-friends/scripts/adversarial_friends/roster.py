"""Decide which friends run, on which model, under which lens.

Self-exclusion drops the host's (cli, model) pair rather than the whole
binary. Blanket per-binary exclusion would be wrong: a CLI judging a spec its
own model authored, under a different lens and effort level, is sometimes
exactly what you want.
"""
import shutil
from typing import Callable

from .adapters import Adapter, FriendSpec
from .errors import NoFriendsError
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


def detect_host(env: dict) -> str | None:
    for marker, cli in HOST_ENV_MARKERS.items():
        if env.get(marker):
            return cli
    return None


def discover_clis(registry: dict[str, Adapter],
                  which: Callable[[str], str | None] = shutil.which) -> list[str]:
    found = []
    for name, adapter in sorted(registry.items()):
        if adapter.transport == "http":
            continue  # reachability is probed separately, not via PATH
        if adapter.binary and which(adapter.binary):
            found.append(name)
    return found


def resolve(registry: dict[str, Adapter], lenses: list[str], env: dict,
            which: Callable[[str], str | None] = shutil.which,
            include_self: bool = False,
            overrides: list[dict] | None = None) -> list[FriendSpec]:
    if overrides:
        specs = []
        for index, entry in enumerate(overrides):
            validate_roster_entry(entry)
            adapter = registry.get(entry["cli"])
            if adapter is None:
                raise NoFriendsError(f"unknown cli in roster: {entry['cli']!r}")
            default_scope = ("repo" if adapter.readonly_argv
                             else NO_READONLY_DEFAULT_SCOPE)
            specs.append(FriendSpec(
                name=entry["name"], cli=entry["cli"], lens=entry["lens"],
                model=entry.get("model"), effort=entry.get("effort"),
                scope=entry.get("scope", default_scope),
                timeout=entry.get("timeout", DEFAULT_TIMEOUT),
            ))
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

    specs = []
    for index, cli in enumerate(available):
        adapter = registry[cli]
        scope = "repo" if adapter.readonly_argv else NO_READONLY_DEFAULT_SCOPE
        specs.append(FriendSpec(
            name=f"{cli}-{lenses[index % len(lenses)]}", cli=cli,
            lens=lenses[index % len(lenses)], model=None, effort=None,
            scope=scope, timeout=DEFAULT_TIMEOUT,
        ))
    return specs

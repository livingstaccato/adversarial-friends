"""Trust boundary for roster files and constructed argv.

A cloned repository is hostile input. Rather than blocklisting dangerous flag
spellings — which missed config overrides, inline settings JSON carrying
hooks, writable --add-dir, and profile layering — the roster is restricted to
values for a fixed set of keys. There is no mechanism for it to inject flags.

The value-level check that remains is direction-aware on purpose: refusing to
start because someone asked for a *safer* sandbox would be its own bug.
"""
from pathlib import Path

from .errors import UsageError
from .ids import validate_friend_name

ROSTER_KEYS = frozenset({"name", "cli", "lens", "model", "effort", "scope", "timeout"})
VALID_SCOPES = frozenset({"repo", "doc"})

DENIED_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--auto",
    "--yolo",
    "-y",
})
DENIED_SANDBOX_VALUES = frozenset({"danger-full-access", "workspace-write"})


def validate_roster_entry(entry: dict) -> dict:
    unknown = set(entry) - ROSTER_KEYS
    if unknown:
        raise UsageError(
            "roster entries may only set "
            f"{sorted(ROSTER_KEYS)}; found {sorted(unknown)}. "
            "Arbitrary flags are available only via --unsafe-extra-args on the "
            "command line, never from a file."
        )
    for required in ("name", "cli", "lens"):
        if not entry.get(required):
            raise UsageError(f"roster entry missing required key: {required}")
    validate_friend_name(entry["name"])
    scope = entry.get("scope", "repo")
    if scope not in VALID_SCOPES:
        raise UsageError(f"invalid scope {scope!r}: expected one of {sorted(VALID_SCOPES)}")
    timeout = entry.get("timeout", 900)
    if not isinstance(timeout, int) or timeout <= 0:
        raise UsageError(f"invalid timeout {timeout!r}: expected a positive integer")
    return entry


def check_denied_values(argv: list[str]) -> None:
    for index, token in enumerate(argv):
        if token in DENIED_FLAGS:
            raise UsageError(
                f"refusing to run: {token} disables the sandbox this tool relies on"
            )
        # A real CLI (e.g. codex, built on clap) accepts both `--sandbox value`
        # and `--sandbox=value`; checking only the following argv element
        # misses the combined-token spelling of the same value.
        flag, has_inline, inline_value = token.partition("=")
        if flag in ("-s", "--sandbox"):
            if has_inline:
                value = inline_value
            elif index + 1 < len(argv):
                value = argv[index + 1]
            else:
                continue
            if value in DENIED_SANDBOX_VALUES:
                raise UsageError(
                    f"refusing to run: sandbox mode {value!r} grants write access"
                )


def contain_path(base: Path, candidate: Path) -> Path:
    """Guarantee a constructed output path stays under the run directory."""
    base_resolved = Path(base).resolve()
    candidate_resolved = Path(candidate).resolve()
    if not candidate_resolved.is_relative_to(base_resolved):
        raise UsageError(
            f"path {candidate_resolved} escapes the run directory {base_resolved}"
        )
    return candidate_resolved

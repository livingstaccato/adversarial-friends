"""Trust boundary for roster files and constructed argv.

A cloned repository is hostile input. Rather than blocklisting dangerous flag
spellings — which missed config overrides, inline settings JSON carrying
hooks, writable --add-dir, and profile layering — the roster is restricted to
values for a fixed set of keys. There is no mechanism for it to inject flags.

The value-level check that remains is direction-aware on purpose: refusing to
start because someone asked for a *safer* sandbox would be its own bug.
"""

from pathlib import Path
import re
from typing import Any

from .errors import UsageError
from .ids import validate_friend_name

ROSTER_KEYS = frozenset({"name", "cli", "lens", "model", "effort", "scope", "timeout"})
VALID_SCOPES = frozenset({"repo", "doc"})

# Model ids seen in the wild: gpt-5.6-sol, claude-sonnet-4-6,
# cloudflare-ai-gateway/openai/gpt-5-nano, gemini-3.1-pro-high, qwen3:0.6b.
# The pattern admits all of those and nothing that begins with a dash, so a
# roster-supplied model string can never be mistaken for a flag.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")

DENIED_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--approve-for-me",
        "--auto",
        "--yolo",
        "-y",
    }
)
DENIED_SANDBOX_VALUES = frozenset({"danger-full-access", "workspace-write"})
# --permission-mode is value-aware and direction-aware, same as --sandbox:
# bypassPermissions/dontAsk skip approval prompts entirely; plan/acceptEdits
# stay permitted (acceptEdits still requires read confirmation and never
# touches shell/bash tools without asking).
DENIED_PERMISSION_MODES = frozenset({"bypassPermissions", "dontAsk"})


def validate_roster_entry(entry: dict[str, Any]) -> dict[str, Any]:
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
    model = entry.get("model")
    if model is not None and MODEL_RE.fullmatch(model) is None:
        raise UsageError(f"invalid model {model!r}: must match {MODEL_RE.pattern!r}")
    scope = entry.get("scope", "repo")
    if scope not in VALID_SCOPES:
        raise UsageError(f"invalid scope {scope!r}: expected one of {sorted(VALID_SCOPES)}")
    timeout = entry.get("timeout", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise UsageError(f"invalid timeout {timeout!r}: expected a positive integer")
    return entry


def check_denied_values(argv: list[str]) -> None:
    for index, token in enumerate(argv):
        # A real CLI (e.g. codex, built on clap) accepts both `--flag value`
        # and `--flag=value`. Partitioning once up front and checking the
        # flag name against every denied set means the combined-token
        # spelling can never slip past a check written for the
        # space-separated form.
        flag, _, inline_value = token.partition("=")
        if flag in DENIED_FLAGS:
            raise UsageError(f"refusing to run: {flag} disables the sandbox this tool relies on")
        if flag in ("-s", "--sandbox"):
            value = inline_value or (argv[index + 1] if index + 1 < len(argv) else "")
            if value in DENIED_SANDBOX_VALUES:
                raise UsageError(f"refusing to run: sandbox mode {value!r} grants write access")
        if flag == "--permission-mode":
            value = inline_value or (argv[index + 1] if index + 1 < len(argv) else "")
            if value in DENIED_PERMISSION_MODES:
                raise UsageError(
                    f"refusing to run: permission mode {value!r} disables approval prompts"
                )


def contain_path(base: Path, candidate: Path) -> Path:
    """Guarantee a constructed output path stays under the run directory."""
    base_resolved = Path(base).resolve()
    candidate_resolved = Path(candidate).resolve()
    if not candidate_resolved.is_relative_to(base_resolved):
        raise UsageError(f"path {candidate_resolved} escapes the run directory {base_resolved}")
    return candidate_resolved

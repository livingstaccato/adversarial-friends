"""Loading a roster from a file -- spec §10, §13, §17.

A roster file replaces repeating `--friend cli:lens` on every invocation. The
machinery to turn one into FriendSpecs already existed in roster.resolve's
`overrides` path; this is the reader that was missing.

**§13's trust boundary is the whole design of this module.** A cloned
repository is hostile input, so:

* Repo-local `.adversarial-friends/` is **never** loaded automatically. A
  cloned repo must not be able to change who reviews it, on what, with what
  flags -- so a repo-local roster is used only when the operator names it
  with `--roster`, which is an explicit act.
* `~/.config/adversarial-friends/roster.toml` **is** trusted and is picked up
  automatically. That is the operator's own machine-wide configuration.
* Entries supply **values only**, for the keys in §10, each validated by
  trust.validate_roster_entry. There is no mechanism for a file to inject a
  flag; `--unsafe-extra-args` exists only on the command line.

The format is TOML, matching the adapter files:

    [[friend]]
    name = "codex-ops"
    cli  = "codex"
    lens = "ops"
    # model, effort, scope and timeout are optional
"""

import os
from pathlib import Path
import tomllib
from typing import Any

from .errors import NoFriendsError, UsageError

ROSTER_FILENAME = "roster.toml"


def user_config_dir() -> Path:
    """The trusted location, per §13."""
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "adversarial-friends"


def default_roster_path() -> Path:
    return user_config_dir() / ROSTER_FILENAME


def load(path: Path) -> list[dict[str, Any]]:
    """Read `[[friend]]` entries from a roster file.

    Entries are returned unvalidated beyond shape: per-entry validation is
    trust.validate_roster_entry's job and happens inside roster.resolve, so
    there is exactly one place that decides what a roster may say.

    An empty roster raises rather than returning `[]`. roster.resolve treats
    an explicit empty override list the same as "no overrides given" and
    falls through to full auto-discovery -- so a file that deliberately names
    zero friends would silently run every discovered CLI instead. That
    landmine is documented in roster.resolve; this is the caller that has to
    respect it.
    """
    path = Path(path)
    if not path.is_file():
        raise UsageError(f"roster file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path} is not valid TOML: {exc}") from exc

    entries = data.get("friend")
    if entries is None:
        raise UsageError(
            f"{path} declares no friends. A roster is a list of [[friend]] "
            "tables, each with at least name, cli and lens."
        )
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise UsageError(f"{path}: 'friend' must be a list of tables ([[friend]])")
    if not entries:
        raise NoFriendsError(
            f"{path} contains no [[friend]] entries. An empty roster is not the "
            "same as no roster: remove --roster (or the file) to fall back to "
            "discovery."
        )
    return entries


def discover() -> Path | None:
    """The trusted user-level roster, if the operator has one.

    Deliberately does NOT look in the repository. See this module's docstring
    and §13: a cloned repo must not be able to choose its own reviewers.
    """
    path = default_roster_path()
    return path if path.is_file() else None


def render(entries: list[dict[str, Any]], notes: list[str] | None = None) -> str:
    """Write a roster file a human is expected to edit.

    §17 calls for "a commented roster reflecting discovered reality -- a file
    to edit, not a wizard to answer", so what comes back is the machine's
    findings with the reasoning attached, not a minimal config.
    """
    lines = [
        "# Adversarial Friends roster.",
        "#",
        "# Written by `afriend init` from what was actually found on this",
        "# machine. Edit freely -- this file is read, never rewritten.",
        "#",
        "# Each [[friend]] needs name, cli and lens. Optional: model, effort",
        "# (low/medium/high/max, per adapter), scope (repo/doc), timeout.",
        "#",
        "# Arbitrary flags are deliberately NOT settable here (§13): a roster",
        "# supplies values only. Use --unsafe-extra-args on the command line",
        "# if you genuinely need more.",
        "",
    ]
    for note in notes or []:
        lines.append(f"# {note}")
    if notes:
        lines.append("")
    for entry in entries:
        lines.append("[[friend]]")
        for key in ("name", "cli", "lens", "model", "effort", "scope", "timeout"):
            value = entry.get(key)
            if value is None:
                continue
            rendered = value if isinstance(value, int) else f'"{value}"'
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)

"""Effort presets -- spec §10, §10.1.

**Inherit, don't override.** Each CLI carries a model and effort its owner
chose deliberately; overriding silently produces surprise behaviour and
surprise cost, and inheriting is the only policy correct on an unseen
machine. So `inherit` is the default and emits no flags at all.

`thorough` is "maximum *available* effort per friend", which is uneven by
construction: claude reaches `xhigh`, codex `xhigh`, agy stops at `high`, and
ollama has no effort concept whatsoever. That unevenness is why the report
header states the effort each friend actually received -- otherwise a weak
critique from a friend that topped out low reads as a signal about the
artifact when it is a signal about the flag matrix.

**A preset cannot promise anything for opencode** (§18.8). Its `--variant`
flag accepts any string silently, so the runner cannot confirm the level a
friend actually ran at; opencode reports `effort: unverified` regardless.
Asking for `thorough` and getting a note saying so is the honest outcome.
"""

from .adapters import Adapter

INHERIT = "inherit"
THOROUGH = "thorough"
CHEAP = "cheap"
PRESETS = (INHERIT, THOROUGH, CHEAP)

# §7: `gate` defaults to thorough. It is the mode that fails a build, so
# spending more per friend is the right default there and nowhere else.
DEFAULT_PRESET_FOR_MODE = {"gate": THOROUGH}

# Best-first. `thorough` walks this and takes the first level an adapter
# actually declares, rather than naming one key and failing on adapters that
# do not have it -- build_argv raises UsageError for an unsupported effort,
# so a fixed key would turn "run thoroughly" into "refuse to run".
_THOROUGH_ORDER = ("max", "xhigh", "high", "medium", "low")
_CHEAP_ORDER = ("low", "medium", "high")


def default_preset(mode: str) -> str:
    return DEFAULT_PRESET_FOR_MODE.get(mode, INHERIT)


def effort_for(preset: str, adapter: Adapter) -> str | None:
    """The effort level `preset` selects for this adapter, or None.

    None means "emit no effort flag", which is both `inherit`'s whole
    behaviour and the honest answer for an adapter with no effort table --
    ollama is a bare model behind an endpoint and has no such concept.
    """
    if preset == INHERIT or not adapter.effort:
        return None
    order = _THOROUGH_ORDER if preset == THOROUGH else _CHEAP_ORDER
    for level in order:
        if level in adapter.effort:
            return level
    return None


def unverifiable_note(preset: str, adapter: Adapter) -> str | None:
    """A note for a friend whose effort cannot be confirmed (§18.8).

    Returned rather than raised: the run should still happen. What must not
    happen is a report that implies `thorough` was honoured everywhere when
    one friend's CLI accepts any variant string without complaint.
    """
    if preset == INHERIT or adapter.effort_kind != "unverified":
        return None
    return (
        f"{adapter.name} reports effort as unverified: its effort flag accepts "
        f"any value silently, so --preset {preset} cannot be confirmed for it. "
        "The level it actually ran at is unknown."
    )


def no_effort_note(preset: str, adapter: Adapter) -> str | None:
    """A note for a friend that has no effort concept at all."""
    if preset == INHERIT or adapter.effort:
        return None
    return f"{adapter.name} has no effort levels, so --preset {preset} changes nothing for it."

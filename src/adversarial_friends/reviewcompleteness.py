"""Bounded presentation of a completed review that received no answers."""

from collections.abc import Iterable, Mapping

from .dispatch import failure_summary
from .ids import FRIEND_NAME_RE

_MAX_REASONS = 3


def _independent(row: Mapping[str, object]) -> bool:
    """Treat old rows as independent unless their advisory role says otherwise."""
    return row.get("independent", True) is True and row.get("host_self_review", False) is not True


def _terminal_status(value: object) -> tuple[bool, str | None] | None:
    """Return whether a recognized terminal status supplied an answer."""
    if not isinstance(value, str):
        return None
    if value == "succeeded" or value == "ok" or value.startswith("ok "):
        return True, None
    for prefix in ("failed: ", "skipped: "):
        if value.startswith(prefix):
            reason = value[len(prefix) :]
            reason = reason.removesuffix(" [orphans suspected]")
            reason = reason.split(" (stderr: ", 1)[0]
            return False, failure_summary(reason) or prefix.removesuffix(": ")
    return None


def from_friends(rows: Iterable[Mapping[str, object]]) -> dict[str, object] | None:
    """Project persisted independent terminal friend rows into zero-answer state.

    The result intentionally contains only short, sanitized status reasons. Detailed
    friend stderr remains in its protected per-round capture rather than escaping into
    terminal output or the status API.
    """
    dispatched = 0
    answered = 0
    reasons: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not _independent(row):
            continue
        name = row.get("name")
        if not isinstance(name, str) or FRIEND_NAME_RE.fullmatch(name) is None:
            continue
        status = _terminal_status(row.get("status"))
        if status is None:
            continue
        dispatched += 1
        did_answer, reason = status
        if did_answer:
            answered += 1
        elif reason is not None:
            reasons.append((name, reason))
    if dispatched == 0 or answered != 0:
        return None

    displayed_reasons = [f"{name}: {reason}" for name, reason in sorted(reasons)[:_MAX_REASONS]]
    message = f"review incomplete: {answered}/{dispatched} friends answered"
    if displayed_reasons:
        message += "; " + "; ".join(displayed_reasons)
    return {
        "state": "incomplete",
        "answered": answered,
        "dispatched": dispatched,
        "reasons": displayed_reasons,
        "message": message,
    }

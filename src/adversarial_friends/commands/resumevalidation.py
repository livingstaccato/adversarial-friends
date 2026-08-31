"""Value-level validation for configuration restored from ``run.json``.

The normal CLI validates these values after constructing an argparse namespace.
Resume treats the file as hostile input, so it must establish the same invariants
before constructing either that namespace or any roster objects.
"""

from typing import Any

from ..errors import UsageError
from ..trust import MODEL_RE

_POSITIVE_SETTINGS = frozenset(
    {
        "timeout",
        "max_friends",
        "max_calls",
        "require_friends",
        "max_rounds",
        "max_wall_clock",
        "max_loop_iterations",
    }
)
_JUDGING_MODES = frozenset({"crossexam", "gate", "loop"})


def _saved_error(name: str, value: object, expected: str) -> UsageError:
    option = name.replace("_", "-")
    return UsageError(f"cannot resume: saved --{option}={value!r}: expected {expected}")


def _validate_saved_path(name: str, value: object, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _saved_error(name, value, "a nonempty path without NUL bytes")


def validate_saved_invocation(saved: dict[str, Any]) -> None:
    """Reject semantic-invalid restored values before object construction."""
    for name in _POSITIVE_SETTINGS:
        value = saved.get(name)
        if value is not None and (type(value) is not int or value <= 0):
            raise _saved_error(name, value, "a positive integer")

    model = saved.get("model")
    if model is not None and (not isinstance(model, str) or MODEL_RE.fullmatch(model) is None):
        raise _saved_error("model", model, f"a value matching {MODEL_RE.pattern!r}")

    _validate_saved_path("artifact", saved.get("artifact"), optional=False)
    if "roster" in saved:
        _validate_saved_path("roster", saved["roster"], optional=True)

    enabled = set(saved.get("enable_provider", []))
    disabled = set(saved.get("disable_provider", []))
    if contradictory := enabled & disabled:
        raise UsageError(
            f"cannot resume: provider(s) {sorted(contradictory)} were passed to both "
            "--enable-provider and --disable-provider"
        )

    mode = saved.get("mode")
    max_rounds = saved.get("max_rounds")
    if mode in _JUDGING_MODES and max_rounds is not None and max_rounds < 2:
        raise UsageError(
            f"cannot resume: --max-rounds={max_rounds} leaves no judging round for --mode {mode}"
        )

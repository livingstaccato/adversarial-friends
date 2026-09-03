"""Built-in, deliberately narrow review profiles."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .cliargs import RUN_MODES
from .errors import UsageError
from .presets import PRESETS


@dataclass(frozen=True)
class ReviewProfile:
    """A named run-mode default with no provider or authority controls."""

    name: str
    mode: str
    settings: Mapping[str, object] = MappingProxyType({})


SAFE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "mode",
        "preset",
        "lenses",
        "max_friends",
        "require_friends",
        "timeout",
        "max_rounds",
        "max_calls",
        "max_wall_clock",
        "max_loop_iterations",
    }
)


_BUILTINS: Final[Mapping[str, ReviewProfile]] = MappingProxyType(
    {
        "quick": ReviewProfile(name="quick", mode="report"),
        "balanced": ReviewProfile(name="balanced", mode="crossexam"),
        "thorough": ReviewProfile(name="thorough", mode="loop"),
    }
)


def builtins() -> Mapping[str, ReviewProfile]:
    """Return the immutable built-in profile registry."""
    return _BUILTINS


def names() -> tuple[str, ...]:
    """Return stable, sorted built-in profile names."""
    return tuple(sorted(_BUILTINS))


def get(name: str) -> ReviewProfile | None:
    """Look up one built-in profile without raising for an unknown name."""
    return _BUILTINS.get(name)


def resolve(name: str, custom: Mapping[str, Mapping[str, object]]) -> ReviewProfile | None:
    """Resolve a safe custom profile through its validated inheritance chain."""
    builtin = get(name)
    if builtin is not None:
        return builtin
    definition = custom.get(name)
    if definition is None:
        return None
    chain: list[Mapping[str, object]] = []
    seen: set[str] = set()
    current_name = name
    while current_name not in _BUILTINS:
        if current_name in seen:
            raise UsageError(f"review profile {name!r} has an inheritance cycle")
        seen.add(current_name)
        current = custom.get(current_name)
        if current is None:
            raise UsageError(f"review profile {name!r} has unknown base {current_name!r}")
        chain.append(current)
        base = current.get("base")
        if not isinstance(base, str):
            raise UsageError(f"review profile {name!r} has an invalid base")
        current_name = base
    base_profile = _BUILTINS[current_name]
    settings: dict[str, object] = dict(base_profile.settings)
    mode = base_profile.mode
    for item in reversed(chain):
        for field, value in item.items():
            if field == "base":
                continue
            if field == "mode":
                mode = str(value)
            else:
                settings[field] = value
    return ReviewProfile(name=name, mode=mode, settings=MappingProxyType(settings))


def validate_safe_setting(field: str, value: object) -> object:
    """Validate one declarative profile setting without exposing authority controls."""
    if field not in SAFE_FIELDS:
        raise UsageError(f"profile has unknown fields: {field!r}")
    if field == "mode":
        if value not in RUN_MODES:
            raise UsageError(f"profile mode must be one of {list(RUN_MODES)}")
        return value
    if field == "preset":
        if value not in PRESETS:
            raise UsageError(f"profile preset must be one of {sorted(PRESETS)}")
        return value
    if field == "lenses":
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise UsageError("profile lenses must be a non-empty list of names")
        return tuple(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UsageError(f"profile {field} must be a positive integer")
    return value

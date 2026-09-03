"""Built-in, deliberately narrow review profiles."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True)
class ReviewProfile:
    """A named run-mode default with no provider or authority controls."""

    name: str
    mode: str


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

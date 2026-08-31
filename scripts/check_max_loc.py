#!/usr/bin/env python3
"""Check that no Python file exceeds the maximum line count."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import sys

MAX_LINES = 777
DIRS = ["src", "tests"]


def find_violations(directories: Iterable[Path]) -> list[tuple[str, int]]:
    """Return sorted Python files whose line counts exceed MAX_LINES."""
    violations: list[tuple[str, int]] = []
    for directory in directories:
        for path in directory.rglob("*.py"):
            count = len(path.read_text().splitlines())
            if count > MAX_LINES:
                violations.append((str(path), count))
    return sorted(violations)


def main() -> int:
    """Return 1 if any file exceeds MAX_LINES."""
    violations = find_violations(Path(name) for name in DIRS)

    if violations:
        print(f"Files exceeding {MAX_LINES} lines:")
        for path, count in violations:
            print(f"  {path}: {count} lines")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

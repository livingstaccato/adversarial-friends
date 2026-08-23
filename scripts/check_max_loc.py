#!/usr/bin/env python3
"""Check that no Python file exceeds the maximum line count."""

from __future__ import annotations

from pathlib import Path
import sys

MAX_LINES = 500
DIRS = ["src", "tests"]


def main() -> int:
    """Return 1 if any file exceeds MAX_LINES."""
    violations: list[tuple[str, int]] = []

    for d in DIRS:
        for f in Path(d).rglob("*.py"):
            lines = len(f.read_text().splitlines())
            if lines > MAX_LINES:
                violations.append((str(f), lines))

    if violations:
        print(f"Files exceeding {MAX_LINES} lines:")
        for path, count in sorted(violations):
            print(f"  {path}: {count} lines")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

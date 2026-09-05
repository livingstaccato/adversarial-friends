#!/usr/bin/env python3
"""Verify VERSION agrees with every plugin manifest's version field.

`VERSION` drives the package's version (via `[tool.setuptools.dynamic]` in
pyproject.toml). The plugin manifests under `plugins/` duplicate that number
as plain JSON because Claude Code's and Codex's plugin loaders read it
directly, without invoking Python packaging. Nothing else keeps those in
sync, so a version bump that only touches VERSION silently ships stale
plugin metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

VERSION_FILE = Path("VERSION")
CODEX_MANIFEST = Path("plugins/afriend/.codex-plugin/plugin.json")
MANIFESTS = [
    Path("plugins/.claude-plugin/marketplace.json"),
    Path("plugins/afriend/.claude-plugin/plugin.json"),
    CODEX_MANIFEST,
]


def _manifest_versions(data: object, path: Path) -> list[tuple[str, str]]:
    """Return (label, version) pairs found in a manifest's top level and any plugins list."""
    found: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return found
    if isinstance(data.get("version"), str):
        found.append((str(path), data["version"]))
    for plugin in data.get("plugins", []) if isinstance(data.get("plugins"), list) else []:
        if isinstance(plugin, dict) and isinstance(plugin.get("version"), str):
            name = plugin.get("name", "?")
            found.append((f"{path} (plugins[].{name})", plugin["version"]))
    return found


def _matches_expected_version(path: Path, version: str, expected: str) -> bool:
    """Accept the documented local Codex cachebuster only in its manifest."""
    if version == expected:
        return True
    if path != CODEX_MANIFEST:
        return False
    return re.fullmatch(rf"{re.escape(expected)}\+codex\.[A-Za-z0-9._-]+", version) is not None


def cli_version() -> str:
    """Read the source CLI version, keeping the checker testable in isolation."""
    sys.path.insert(0, str(Path("src")))
    from afriend import __version__

    return __version__


def main() -> int:
    """Return 1 if any manifest's version disagrees with VERSION, 0 if all match."""
    if not VERSION_FILE.is_file():
        print(f"error: {VERSION_FILE} not found", file=sys.stderr)
        return 1
    expected = VERSION_FILE.read_text().strip()

    mismatches: list[str] = []
    for manifest in MANIFESTS:
        if not manifest.is_file():
            print(f"error: manifest not found: {manifest}", file=sys.stderr)
            return 1
        data = json.loads(manifest.read_text())
        for label, version in _manifest_versions(data, manifest):
            if not _matches_expected_version(manifest, version, expected):
                mismatches.append(f"  {label}: {version} != {expected}")

    # The version the CLI reports. It was hardcoded in
    # src/afriend/__init__.py and drifted two releases behind
    # VERSION before anything noticed -- `afriend --version` printed 0.1.0
    # from a 0.1.2 wheel. It is derived now, and checked here so a future
    # spelling that reintroduces a literal cannot go unnoticed.
    actual_cli_version = cli_version()
    if actual_cli_version != expected:
        mismatches.append(f"  afriend.__version__: {actual_cli_version} != {expected}")

    if not mismatches:
        return 0

    print(f"VERSION ({expected}) disagrees with:", file=sys.stderr)
    for line in mismatches:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

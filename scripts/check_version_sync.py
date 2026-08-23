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
import sys

VERSION_FILE = Path("VERSION")
MANIFESTS = [
    Path("plugins/.claude-plugin/marketplace.json"),
    Path("plugins/adversarial-friends/.claude-plugin/plugin.json"),
    Path("plugins/adversarial-friends/.codex-plugin/plugin.json"),
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
            if version != expected:
                mismatches.append(f"  {label}: {version} != {expected}")

    if not mismatches:
        return 0

    print(f"VERSION ({expected}) disagrees with:", file=sys.stderr)
    for line in mismatches:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

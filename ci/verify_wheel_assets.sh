#!/usr/bin/env bash
# Build a wheel and compare its bundled assets to the source-derived manifest.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

# setuptools reuses build/lib across invocations but does not remove package
# data deleted from the source tree. Start from the repository's ignored,
# generated intermediate directory so the wheel reflects this checkout only.
rm -rf "$repo/build"
uv build --wheel --out-dir "$scratch/dist" "$repo"
wheel=$(find "$scratch/dist" -name '*.whl' -print -quit)

python3 - "$repo" "$wheel" <<'PY'
from __future__ import annotations

from pathlib import Path
import sys
import zipfile

repo = Path(sys.argv[1])
wheel = Path(sys.argv[2])
assets = repo / "src" / "adversarial_friends" / "assets"
expected: set[str] = set()
for directory, pattern in (("adapters", "*.toml"), ("harnesses", "*.md"), ("lenses", "*.md"), ("entrypoints", "*.md")):
    for path in (assets / directory).rglob(pattern):
        expected.add(str(Path("adversarial_friends/assets") / path.relative_to(assets)))

with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        print("error: wheel has duplicate archive members:", *duplicates, sep="\n  ", file=sys.stderr)
        raise SystemExit(1)
    actual = {
        name
        for name in names
        if name.startswith("adversarial_friends/assets/") and name.endswith((".md", ".toml"))
    }

missing = sorted(expected - actual)
unexpected = sorted(actual - expected)
if missing or unexpected:
    if missing:
        print("error: wheel missing expected assets:", *missing, sep="\n  ", file=sys.stderr)
    if unexpected:
        print("error: wheel has unexpected assets:", *unexpected, sep="\n  ", file=sys.stderr)
    raise SystemExit(1)
print(f"ok: wheel contains exactly {len(expected)} source-derived assets")
PY

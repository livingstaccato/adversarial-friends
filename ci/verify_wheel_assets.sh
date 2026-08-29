#!/usr/bin/env bash
# Build the wheel and confirm every bundled asset (adapters, lenses,
# references, SKILL.md) actually made it in. Package-data misconfiguration
# in pyproject.toml is silent at build time -- the wheel builds and installs
# fine, and the failure only shows up later as "adapter directory not found"
# on first run. This makes that failure mode visible in CI instead.
set -euo pipefail

EXPECTED=15  # 5 adapters + 6 lenses + 3 references + SKILL.md

# Built into a scratch directory rather than `dist/`. This used to be
# `rm -rf dist && uv build --wheel`, which is harmless in CI and destructive
# when a release is being cut by hand: run between `uv build` and
# `twine upload dist/*`, it deleted the sdist, and 0.1.7 went to PyPI as a
# wheel with no source distribution -- the only release of this project
# missing one. A fresh build directory gets the same guarantee without
# reaching into the one holding the artifacts about to be uploaded.
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
uv build --wheel --out-dir "$out"

count=$(unzip -l "$out"/*.whl | grep -c 'adversarial_friends/assets/.*\.\(toml\|md\)$')

if [ "$count" -ne "$EXPECTED" ]; then
  echo "error: expected $EXPECTED bundled asset files in the wheel, found $count" >&2
  unzip -l "$out"/*.whl | grep 'adversarial_friends/assets/' >&2 || true
  exit 1
fi

echo "ok: $count bundled asset files found in the wheel"

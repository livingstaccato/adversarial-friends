#!/usr/bin/env bash
# Build the wheel and confirm every bundled asset (adapters, lenses,
# references, SKILL.md) actually made it in. Package-data misconfiguration
# in pyproject.toml is silent at build time -- the wheel builds and installs
# fine, and the failure only shows up later as "adapter directory not found"
# on first run. This makes that failure mode visible in CI instead.
set -euo pipefail

EXPECTED=15  # 5 adapters + 6 lenses + 3 references + SKILL.md

rm -rf dist
uv build --wheel

count=$(unzip -l dist/*.whl | grep -c 'adversarial_friends/assets/.*\.\(toml\|md\)$')

if [ "$count" -ne "$EXPECTED" ]; then
  echo "error: expected $EXPECTED bundled asset files in the wheel, found $count" >&2
  unzip -l dist/*.whl | grep 'adversarial_friends/assets/' >&2 || true
  exit 1
fi

echo "ok: $count bundled asset files found in the wheel"

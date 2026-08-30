#!/usr/bin/env bash
# Install the built wheel and run it. Building a wheel proves it packs;
# installing it proves it works -- and the two are not the same check. The
# version the CLI reports drifted two releases behind VERSION while the
# wheel metadata, the plugin manifests and VERSION itself all agreed,
# because nothing here ever ran the installed console script.
#
# Runs from a directory that is not the checkout, so an asset resolved by
# accident from the source tree cannot pass for one that shipped.
set -euo pipefail

EXPECTED_VERSION="$(cat VERSION)"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# Always build from the current checkout. Reusing dist/*.whl can silently test
# a stale release after VERSION changes.
uv build --wheel --out-dir "$scratch/dist"

venv="$scratch/venv"
uv venv "$venv"
VIRTUAL_ENV="$venv" uv pip install --quiet "$scratch"/dist/*.whl

reported="$(cd /tmp && "$venv/bin/afriend" --version)"
if [ "$reported" != "afriend $EXPECTED_VERSION" ]; then
  echo "error: installed CLI reports '$reported', VERSION says '$EXPECTED_VERSION'" >&2
  exit 1
fi

# Doctor reads the packaged adapters. From /tmp there is no checkout to fall
# back on, so a missing adapter file shows up here rather than at a user's
# first run.
#
# Its exit code is deliberately NOT the assertion: `doctor` returns 3 when
# no agent CLI is installed, which is correct and is exactly what a CI
# runner reports. What proves the package data shipped is that it can name
# every adapter -- those names exist only in the packaged .toml files.
doctor_out="$(cd /tmp && "$venv/bin/afriend" doctor || true)"
for adapter in agy claude codex ollama opencode; do
  if ! grep -q "^$adapter " <<<"$doctor_out"; then
    echo "error: 'afriend doctor' did not report adapter '$adapter' from outside the checkout" >&2
    echo "$doctor_out" >&2
    exit 1
  fi
done

# The no-install entry point, which the README documents separately.
(cd /tmp && "$venv/bin/python" -m adversarial_friends --help >/dev/null)

echo "ok: installed wheel reports $reported and runs outside the checkout"

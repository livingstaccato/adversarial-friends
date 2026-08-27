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

# `ls` rather than `test -f`: the glob can match more than one file, which
# test(1) rejects rather than treating as "present".
ls dist/*.whl >/dev/null 2>&1 || uv build --wheel

venv="$(mktemp -d)/venv"
uv venv "$venv"
VIRTUAL_ENV="$venv" uv pip install --quiet dist/*.whl

reported="$(cd /tmp && "$venv/bin/afriend" --version)"
if [ "$reported" != "afriend $EXPECTED_VERSION" ]; then
  echo "error: installed CLI reports '$reported', VERSION says '$EXPECTED_VERSION'" >&2
  exit 1
fi

# Doctor reads the packaged adapters and lenses. From /tmp there is no
# checkout to fall back on, so this fails if package data did not ship.
if ! (cd /tmp && AF_NO_HTTP_DISCOVERY=1 "$venv/bin/afriend" doctor >/dev/null); then
  echo "error: 'afriend doctor' failed from outside the checkout" >&2
  exit 1
fi

# The no-install entry point, which the README documents separately.
(cd /tmp && "$venv/bin/python" -m adversarial_friends --help >/dev/null)

echo "ok: installed wheel reports $reported and runs outside the checkout"

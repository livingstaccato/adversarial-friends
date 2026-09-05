#!/usr/bin/env bash
# Build every distribution released from this repository and prove that the
# compatibility projects are dependency-only aliases of the real package.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
version="$(tr -d '[:space:]' < "$repo/VERSION")"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
dist="$scratch/dist"
mkdir -p "$dist"

uv build --wheel --sdist --out-dir "$dist" "$repo"
uv build --wheel --sdist --out-dir "$dist" "$repo/compatibility-distributions/adversarial-friends"
uv build --wheel --sdist --out-dir "$dist" "$repo/compatibility-distributions/afriends"

test -f "$dist/afriend-${version}-py3-none-any.whl"
test -f "$dist/afriend-${version}.tar.gz"
test -f "$dist/adversarial_friends-${version}-py3-none-any.whl"
test -f "$dist/adversarial_friends-${version}.tar.gz"
test -f "$dist/afriends-${version}-py3-none-any.whl"
test -f "$dist/afriends-${version}.tar.gz"
count="$(find "$dist" -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' \) | wc -l | tr -d '[:space:]')"
test "$count" -eq 6

uvx --from twine==7.0.0 twine check --strict "$dist"/*

python3 - "$dist" "$version" <<'PY'
from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import sys
import zipfile

dist = Path(sys.argv[1])
version = sys.argv[2]
for normalized_name in ("adversarial_friends", "afriends"):
    wheel = dist / f"{normalized_name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        unexpected = [name for name in names if ".dist-info/" not in name]
        if unexpected:
            print(f"error: {wheel.name} contains runtime members: {unexpected}", file=sys.stderr)
            raise SystemExit(1)
        metadata_name = f"{normalized_name}-{version}.dist-info/METADATA"
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    if metadata.get_all("Requires-Dist") != [f"afriend=={version}"]:
        print(
            f"error: {wheel.name} must require exactly afriend=={version}; "
            f"got {metadata.get_all('Requires-Dist')}",
            file=sys.stderr,
        )
        raise SystemExit(1)
PY

smoke="$scratch/smoke"
mkdir -p "$smoke"
for distribution in afriend adversarial_friends afriends; do
    venv="$smoke/$distribution"
    uv venv "$venv"
    wheel="$dist/${distribution}-${version}-py3-none-any.whl"
    if [ "$distribution" = "afriend" ]; then
        uv pip install --python "$venv/bin/python" "$wheel"
    else
        uv pip install --python "$venv/bin/python" --no-index --find-links "$dist" "$wheel"
    fi
    reported="$(cd /tmp && "$venv/bin/afriend" --version)"
    test "$reported" = "afriend $version"
    (cd /tmp && "$venv/bin/python" - <<'PY'
import importlib.util

import afriend

assert afriend is not None
assert importlib.util.find_spec("afriends") is None
assert importlib.util.find_spec("adversarial_friends") is None
PY
    )
    test ! -e "$venv/bin/afriends"
    test ! -e "$venv/bin/adversarial-friends"
done

echo "ok: verified afriend, adversarial-friends, and afriends"

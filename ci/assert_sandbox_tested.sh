#!/usr/bin/env bash
# Fail if §12.2's real containment tests skipped instead of running.
#
# They are guarded by `skipif no mechanism available`, which is correct for a
# developer machine but wrong for CI: a skipped security test reports the
# same green tick as a passing one. This turns that into a failure.
set -euo pipefail

output=$(uv run pytest tests/test_sandbox.py -q -rs 2>&1)
echo "$output"

if echo "$output" | grep -q "no OS sandbox mechanism"; then
  echo "ERROR: containment tests skipped -- no sandbox mechanism was found." >&2
  echo "ci/install_sandbox.sh should have installed and verified one." >&2
  exit 1
fi
echo "containment tests ran."

#!/usr/bin/env bash
# Install and verify the Linux OS-confinement mechanism (spec §12.2).
#
# Without this, tests/test_sandbox.py's real containment tests skip on every
# Linux runner, and the bwrap path would only ever be exercised by whoever
# happened to have bubblewrap installed. A sandbox nobody tests is not a
# sandbox anyone should trust.
#
# Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor
# (kernel 6.8+), which bwrap needs. The sysctl is relaxed here rather than
# worked around, because the alternative is a silent skip that looks
# identical to a pass.
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq bubblewrap

# Unprivileged user namespaces, which bwrap requires.
if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi

# Prove it works here rather than assuming the install was enough -- and
# prove it using the runner's OWN bind layout, not a hand-written bwrap
# invocation. The first version of this check used its own flags, passed a
# layout the real code does not produce, and failed for a reason the real
# code did not have (`--ro-bind /bin` on a merged-/usr distro, where /bin is
# a symlink). Verifying anything other than the shipped code path is how you
# get a green check for a sandbox that does not work.
echo "verifying bwrap can create a namespace using the runner's own policy..."
uv run python - <<'PY'
import subprocess
import sys
import tempfile
from pathlib import Path

from adversarial_friends import sandbox

workdir = Path(tempfile.mkdtemp())
policy = sandbox.policy_for(workdir, "true", ())
argv = sandbox.wrap(["true"], sandbox.BWRAP, policy)
result = subprocess.run(argv, capture_output=True, text=True)
if result.returncode != 0:
    print(f"bwrap failed: {result.stderr.strip()}", file=sys.stderr)
    print(f"argv was: {argv}", file=sys.stderr)
    sys.exit(1)
print("bwrap works; §12.2 containment tests will run")
PY

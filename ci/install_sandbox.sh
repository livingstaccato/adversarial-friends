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
# working around it, because the alternative is a silent skip that looks
# identical to a pass.
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq bubblewrap

# Unprivileged user namespaces, which bwrap requires.
if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi

# Prove it actually works here rather than assuming the install was enough.
# A bwrap that installs but cannot create a namespace would otherwise turn
# every containment test into a skip.
echo "verifying bwrap can create a namespace..."
if bwrap --ro-bind /usr /usr --ro-bind /bin /bin --dev /dev -- /bin/true; then
  echo "bwrap works; §12.2 containment tests will run"
else
  echo "ERROR: bwrap installed but cannot create a namespace." >&2
  echo "The §12.2 containment tests would silently skip. Failing instead." >&2
  exit 1
fi

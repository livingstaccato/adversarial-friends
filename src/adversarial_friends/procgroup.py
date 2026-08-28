"""Reaping a friend's whole process group.

Agent CLIs spawn descendants -- MCP servers, shells, language servers -- so
killing only the parent leaves them running, making network calls and writing
files after the round has been marked incomplete. Hence process groups:
SIGTERM to the group, a grace period, then SIGKILL to the group.

A descendant that leaves the group entirely (its own `os.setsid()`, see
`test_setsid_escapee_is_not_reaped`) cannot be reached by any of this. That is
a real, accepted limitation rather than a bug: pgid membership is the only
handle these functions have.

Split out of spawn.py, which had grown past this repo's 500-line file cap.
"""

import contextlib
import os
import signal
import subprocess
import time

# Wait windows for group escalation: this long for the group to exit after
# SIGTERM, then (if anything is still alive) this long for it to actually
# disappear after SIGKILL. SIGKILL cannot be blocked or ignored, so anything
# still a *member* of the group cannot survive it.
GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5
_POLL_INTERVAL_S = 0.05


def _signal_group(pgid: int, sig: int) -> bool:
    """Send sig to every process in pgid. False means the group is already
    empty -- nothing to signal, not an error."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


def _group_alive(pgid: int) -> bool:
    """Best-effort membership check via the null signal: it does nothing to
    the target but still requires the kernel to confirm something with that
    pgid exists. A zombie member still counts as "alive" here -- it only
    disappears once its actual parent (or, once orphaned, the OS's reaper)
    calls wait() on it, which is exactly the condition callers are polling
    for."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _reap_after_signal(process: subprocess.Popen[bytes], pgid: int, grace_seconds: float) -> None:
    """Wait up to grace_seconds for the group to empty out.

    Two different things need to happen here, not one. `process` is *our*
    direct child: the kernel keeps it as a zombie until we call wait() on it
    ourselves -- polling `_group_alive` alone would spin for the full grace
    window every time, since the kernel never stops reporting a zombie we
    haven't reaped. Other descendants are not our children (they may even be
    reparented to the OS's reaper after their own parent dies); we cannot
    wait() on those, only poll for them to vanish once whichever process is
    responsible for them reaps them.
    """
    deadline = time.monotonic() + grace_seconds
    remaining = max(0.0, deadline - time.monotonic())
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=remaining)
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(_POLL_INTERVAL_S)


def _terminate_group(process: subprocess.Popen[bytes], pgid: int) -> bool:
    """Escalate SIGTERM -> grace period -> SIGKILL against the whole process
    group. Called both after a timeout and after every ordinary completion
    (see `run_process`): a friend that exits 0 can still leave a descendant
    alive in its group, and that descendant deserves the same cleanup a
    timed-out one gets. When nothing is left in the group, the first
    `_signal_group` call returns False immediately and this is a no-op --
    the common case (a friend with no children) pays for one syscall, not a
    wait.

    Returns True if the group still has a member after the full escalation.
    SIGKILL cannot be blocked or ignored, so this should be False in
    practice every time -- anything actually still a *member* of the group
    at that point cannot survive it. It is kept as a defensive, independent
    signal into run_process's orphans_suspected anyway, in case a future
    platform or edge case breaks that assumption.

    Note what this does *not* catch: a descendant that calls its own
    os.setsid() leaves this group entirely, by definition, the moment it
    does so (see test_setsid_escapee_is_not_reaped) -- pgid membership can
    never observe it, before or after this runs. run_process detects that
    case separately, from whether the stdout/stderr pump threads reach
    natural EOF once this sweep is done.
    """
    if not _signal_group(pgid, signal.SIGTERM):
        return False
    _reap_after_signal(process, pgid, GRACE_SECONDS)
    if not _group_alive(pgid):
        return False
    if not _signal_group(pgid, signal.SIGKILL):
        return False
    _reap_after_signal(process, pgid, KILL_GRACE_SECONDS)
    return _group_alive(pgid)

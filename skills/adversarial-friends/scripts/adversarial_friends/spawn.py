"""Run one friend under a timeout, in its own process group.

Agent CLIs spawn descendants -- MCP servers, shells, language servers -- so
killing only the parent on timeout leaves them running, making network calls
and writing files after the run has been marked incomplete. Hence process
groups: SIGTERM to the whole group, a grace period, then SIGKILL to the whole
group. A friend can also leave a descendant running after exiting 0 (it never
timed out at all), so the same sweep runs after every round, not only after a
timeout -- see `run_process`.

Exit status is not evidence of success either: a friend can exit 0 while
answering an unrelated prompt, or after writing its findings somewhere other
than stdout. A round only counts as succeeded when exit is 0 AND stdout
parsed AND normalize() found at least one claim or an explicit
`{"no_findings": true}` marker. A timeout always takes precedence over
parsing: a killed friend's truncated output never enters the repair path, it
is simply a failed round.

One more interaction between those two hazards is worth calling out because
it is easy to miss: `subprocess.Popen.communicate()` does not return once the
process we spawned has exited -- it returns once the process has exited *and*
both its stdout and stderr pipes have hit EOF. Any subprocess call that does
not explicitly redirect stdio inherits fds 0/1/2 from its parent regardless
of `close_fds` (that only governs fds >= 3), so a descendant the friend
spawned and never waited on can hold our stdout/stderr pipes open long after
the friend itself has finished and printed its answer. Built on
`communicate()`, that would make `run_process` block for the full timeout on
a friend that actually succeeded quickly, then report `timed_out=True` --
turning hazard #2 backwards, misreporting a real success as a failure. This
module avoids `communicate()` for exactly that reason: stdin is written and
stdout/stderr are drained on background threads, while the main flow polls
`process.poll()` directly to learn when *our* child is done, independent of
whether any pipe has reached EOF.
"""
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .normalize import NormalizeResult, normalize

# Wait windows for group escalation: this long for the group to exit after
# SIGTERM, then (if anything is still alive) this long for it to actually
# disappear after SIGKILL. SIGKILL cannot be blocked or ignored, so anything
# still a *member* of the group cannot survive it -- only a descendant that
# has left the group entirely (e.g. via its own os.setsid()) can outlast
# this, and that is a real, accepted limitation, not a bug here.
GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5
_POLL_INTERVAL_S = 0.05
# How long to wait for the output-pumping threads to notice the group is
# dead and finish, once we already know the process itself is done. Purely
# a drain window, not a kill/escalation window, so it can be short.
_DRAIN_JOIN_S = 2.0


@dataclass
class SpawnResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    result: NormalizeResult
    failure_reason: str | None


def _pump_stdin(process: subprocess.Popen, stdin_text: str | None) -> None:
    """Write the prompt (if any) on its own thread and close stdin.

    Writing synchronously on the main thread before we start polling would
    risk the classic pipe deadlock: a prompt larger than the OS pipe buffer
    blocks our write() until the friend reads more, but the friend may
    itself be blocked writing to its own full stdout pipe if nobody is
    draining it concurrently. Running the write here, alongside the stdout
    and stderr pump threads, avoids that.
    """
    try:
        if stdin_text:
            process.stdin.write(stdin_text)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def _pump_output(stream, chunks: list) -> None:
    """Read a text-mode pipe to EOF, one line at a time, appending into
    chunks. Runs on a background thread so a descendant holding this pipe
    open after our own child has exited never blocks the main flow -- see
    the module docstring."""
    try:
        for line in iter(stream.readline, ""):
            chunks.append(line)
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


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


def _reap_after_signal(process: subprocess.Popen, pgid: int, grace_seconds: float) -> None:
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
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        pass
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(_POLL_INTERVAL_S)


def _terminate_group(process: subprocess.Popen, pgid: int) -> None:
    """Escalate SIGTERM -> grace period -> SIGKILL against the whole process
    group. Called both after a timeout and after every ordinary completion
    (see `run_process`): a friend that exits 0 can still leave a descendant
    alive in its group, and that descendant deserves the same cleanup a
    timed-out one gets. When nothing is left in the group, the first
    `_signal_group` call returns False immediately and this is a no-op --
    the common case (a friend with no children) pays for one syscall, not a
    wait.
    """
    if not _signal_group(pgid, signal.SIGTERM):
        return
    _reap_after_signal(process, pgid, GRACE_SECONDS)
    if not _group_alive(pgid):
        return
    if not _signal_group(pgid, signal.SIGKILL):
        return
    _reap_after_signal(process, pgid, KILL_GRACE_SECONDS)
    # Anything still alive past this point has left the group entirely
    # (e.g. by calling os.setsid() itself) -- there is nothing more this
    # function can do about it with process-group signals alone.


def run_process(argv: list[str], stdin_text: str | None, timeout_s: int,
                 cwd: Path) -> SpawnResult:
    started = time.monotonic()
    process = subprocess.Popen(
        argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    # start_new_session=True runs setsid() in the child before exec, which
    # makes it both a new session leader and a new process group leader --
    # its pgid is therefore always its own pid. Capturing that now means
    # later cleanup never has to call os.getpgid() on a pid that may
    # already have been reaped (and, at least in principle, recycled).
    pgid = process.pid

    stdout_chunks: list = []
    stderr_chunks: list = []
    stdin_thread = threading.Thread(target=_pump_stdin, args=(process, stdin_text), daemon=True)
    stdout_thread = threading.Thread(target=_pump_output, args=(process.stdout, stdout_chunks), daemon=True)
    stderr_thread = threading.Thread(target=_pump_output, args=(process.stderr, stderr_chunks), daemon=True)
    stdin_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    deadline = started + timeout_s
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(_POLL_INTERVAL_S)

    # Whether the friend finished on its own or ran long, sweep its process
    # group. On a timeout this is the kill that hazard #1 exists for. On a
    # clean exit it is just as necessary: a friend can exit 0 while a
    # descendant it spawned (and never waited on) is still alive in the same
    # group -- left alone, that descendant would keep making network calls
    # or writing files after this round has already been decided. This is
    # also what unblocks the output-pump threads when a descendant was
    # holding a pipe open: killing the group closes its copy of the fd.
    _terminate_group(process, pgid)

    stdout_thread.join(timeout=_DRAIN_JOIN_S)
    stderr_thread.join(timeout=_DRAIN_JOIN_S)
    stdin_thread.join(timeout=_DRAIN_JOIN_S)
    # A thread that is still alive here is blocked on a descendant that
    # escaped the process group entirely (the setsid case _terminate_group's
    # docstring calls out) and will never see EOF. It is left running as a
    # daemon thread rather than joined forever; whatever it collected before
    # this point is what we use.

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    duration = time.monotonic() - started

    # Timeout wins over parsing: truncated output from a killed process is
    # not a candidate for repair, it is simply a failed round.
    if timed_out:
        return SpawnResult(
            argv, process.returncode, stdout, stderr, duration,
            True, NormalizeResult(None, ["killed on timeout"], False), "timeout",
        )

    result = normalize(stdout)
    failure_reason = None
    if process.returncode != 0:
        failure_reason = f"exit {process.returncode}"
    elif not result.succeeded:
        failure_reason = "; ".join(result.errors) or "unusable output"
    return SpawnResult(
        argv, process.returncode, stdout, stderr, duration,
        False, result, failure_reason,
    )

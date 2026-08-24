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

Two more things worth calling out because they are easy to miss:

`subprocess.Popen.communicate()` does not return once the process we spawned
has exited -- it returns once the process has exited *and* both its stdout
and stderr pipes have hit EOF. Any subprocess call that does not explicitly
redirect stdio inherits fds 0/1/2 from its parent regardless of `close_fds`
(that only governs fds >= 3), so a descendant the friend spawned and never
waited on can hold our stdout/stderr pipes open long after the friend itself
has finished and printed its answer -- built on `communicate()`, that would
misreport a real, fast success as a timeout. This module avoids
`communicate()` for exactly that reason: stdin is written and stdout/stderr
are drained on background threads, while the main flow polls `process.poll()`
directly to learn when *our* child is done, independent of whether any pipe
has reached EOF.

A descendant that leaves the process group entirely (calling its own
`os.setsid()` before this module can intervene -- see `test_setsid_escapee_is_not_reaped`)
can hold a stdout/stderr pipe open forever; nothing this process does can
force it closed. A pump thread blocked in a plain, buffered `readline()`
cannot be reliably unblocked from another thread either -- verified directly
on this runtime: closing the file object from a different thread while a
`readline()` call is blocked inside it does not make that call return.
Reading is therefore done through a non-blocking fd polled with `selectors`,
so a pump thread is never stuck in a syscall it can't get back out of: it is
always back at a `stop_event` check within one `_POLL_INTERVAL_S`.
"""

import codecs
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import IO
import warnings

from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .normalize import Envelope, NormalizeResult, normalize

# Wait windows for group escalation: this long for the group to exit after
# SIGTERM, then (if anything is still alive) this long for it to actually
# disappear after SIGKILL. SIGKILL cannot be blocked or ignored, so anything
# still a *member* of the group cannot survive it -- only a descendant that
# has left the group entirely (e.g. via its own os.setsid()) can outlast
# this, and that is a real, accepted limitation, not a bug here.
GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5
_POLL_INTERVAL_S = 0.05
# How long to give the output-pump threads to finish draining once we
# already know the process is done, before falling back to stop_event to
# force them out. Purely a drain window, not a kill/escalation window.
_DRAIN_JOIN_S = 2.0
_READ_CHUNK = 65536


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
    orphans_suspected: bool


def _pump_stdin(process: subprocess.Popen[bytes], stdin_text: str | None) -> None:
    """Write the prompt (if any) on its own thread and close stdin.

    Writing synchronously on the main thread before we start polling would
    risk the classic pipe deadlock: a prompt larger than the OS pipe buffer
    blocks our write() until the friend reads more, but the friend may
    itself be blocked writing to its own full stdout pipe if nobody is
    draining it concurrently. Running the write here, alongside the stdout
    and stderr pump threads, avoids that.
    """
    # process was always constructed with stdin=subprocess.PIPE (see
    # run_process below) -- .stdin is only ever None for a Popen that never
    # requested a pipe, which never happens on this code path.
    assert process.stdin is not None
    try:
        if stdin_text:
            process.stdin.write(stdin_text.encode("utf-8"))
    except (BrokenPipeError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            process.stdin.close()


def _pump_output(stream: IO[bytes], chunks: list[str], stop_event: threading.Event) -> None:
    """Drain a pipe into chunks (decoded str fragments) until EOF or
    stop_event is set, without ever blocking in an uninterruptible read.

    The fd is switched to non-blocking and polled with a selector so the
    loop is always back at the stop_event check within one
    _POLL_INTERVAL_S -- see the module docstring for why a plain blocking
    readline() loop cannot be relied on to exit once a descendant has
    escaped the process group and is holding this pipe open. stop_event is
    checked only once no more data is immediately available, so a chunk
    that becomes ready in the same instant stop_event is set is still read
    before this returns -- setting stop_event never truncates output that
    was already sitting in the kernel buffer.
    """
    fd = stream.fileno()
    os.set_blocking(fd, False)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    try:
        while True:
            if sel.select(timeout=_POLL_INTERVAL_S):
                try:
                    raw = os.read(fd, _READ_CHUNK)
                except BlockingIOError:
                    raw = None
                except OSError:
                    break
                if raw == b"":
                    break
                if raw:
                    chunks.append(decoder.decode(raw))
                continue
            if stop_event.is_set():
                break
        chunks.append(decoder.decode(b"", final=True))
    finally:
        sel.close()
        with contextlib.suppress(OSError):
            stream.close()


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


def _early_failure(argv: list[str], duration: float, reason: str) -> SpawnResult:
    """Build a SpawnResult for a friend that never actually started (the
    binary is missing or not executable). run_process's signature promises
    a SpawnResult, not an exception: Task 12 calls this inside a thread
    pool, where an escaping FileNotFoundError/PermissionError from Popen()
    would take down the whole dispatch instead of marking one friend
    failed."""
    return SpawnResult(
        argv,
        None,
        "",
        "",
        duration,
        False,
        NormalizeResult(None, [reason], False),
        reason,
        False,
    )


def run_process(
    argv: list[str],
    stdin_text: str | None,
    timeout_s: int,
    cwd: Path,
    abort_event: threading.Event | None = None,
    envelope: Envelope | None = None,
    structured_output: bool = False,
    contract: PayloadContract = CLAIM_CONTRACT,
    env: dict[str, str] | None = None,
) -> SpawnResult:
    """Run one friend; see the module docstring for the process-group and
    pump-thread hazards this guards against.

    `abort_event`, if given, is polled on the same cadence as the timeout
    deadline (every `_POLL_INTERVAL_S`). Setting it from another thread --
    e.g. a signal handler reacting to Ctrl-C or SIGTERM -- terminates this
    process's group through the exact same path a timeout already uses and
    returns promptly with `failure_reason="aborted"`, rather than this call
    blocking for the rest of `timeout_s` regardless of what the caller
    wants. Default `None` means this call behaves exactly as before: no new
    exit condition is checked, so every existing caller and test is
    unaffected.

    `envelope`/`structured_output`/`contract` are passed straight through
    to normalize() -- see its docstring. The defaults (None/False/claims)
    reproduce exactly the prior behavior for every existing caller;
    `contract` is what a cross-examination round overrides so a judge's
    output is read against the verdict schema rather than the claim one.

    Popen() can fail for reasons beyond a missing or non-executable binary:
    `Argument list too long` (E2BIG, when a friend's prompt landed in a
    single argv element and exceeded the kernel's per-argument limit) or
    `Exec format error` (ENOEXEC, a corrupt or non-executable-format file
    that nonetheless has the execute bit set) are both plain OSError, not
    FileNotFoundError/PermissionError. This function's contract is "return a
    SpawnResult naming the cause," full stop -- catching only the two
    specific subclasses let every other OSError escape Popen() and propagate
    out of the worker thread that calls this (see cli.py's dispatch wrapper
    for the second half of that fix: even if something here still slipped
    through, one friend's exception must never end the whole run).
    """
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            # None inherits, which is what an unconfined friend gets. A
            # confined one is handed an allowlisted environment instead --
            # see childenv, and dispatch._dispatch for who gets which.
            env=env,
        )
    except FileNotFoundError:
        return _early_failure(argv, time.monotonic() - started, f"binary not found: {argv[0]}")
    except PermissionError:
        return _early_failure(argv, time.monotonic() - started, f"binary not executable: {argv[0]}")
    except OSError as exc:
        return _early_failure(argv, time.monotonic() - started, f"failed to start: {exc}")
    # start_new_session=True runs setsid() in the child before exec, which
    # makes it both a new session leader and a new process group leader --
    # its pgid is therefore always its own pid. Capturing that now means
    # later cleanup never has to call os.getpgid() on a pid that may
    # already have been reaped (and, at least in principle, recycled).
    pgid = process.pid

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stop_event = threading.Event()
    stdin_thread = threading.Thread(target=_pump_stdin, args=(process, stdin_text), daemon=True)
    stdout_thread = threading.Thread(
        target=_pump_output, args=(process.stdout, stdout_chunks, stop_event), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump_output, args=(process.stderr, stderr_chunks, stop_event), daemon=True
    )
    stdin_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    deadline = started + timeout_s
    timed_out = False
    aborted = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        if abort_event is not None and abort_event.is_set():
            aborted = True
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
    orphans_suspected = _terminate_group(process, pgid)

    stdout_thread.join(timeout=_DRAIN_JOIN_S)
    stderr_thread.join(timeout=_DRAIN_JOIN_S)
    stdin_thread.join(timeout=_DRAIN_JOIN_S)
    # A pump thread still alive here, well after the group sweep above has
    # finished, has nothing left it could still be legitimately waiting
    # for -- every process we can reach is dead. It being blocked anyway is
    # itself the evidence: something still holds this pipe's write end
    # open. That is deliberately used as a second, independent source for
    # orphans_suspected, not just _terminate_group's pgid-membership check
    # above. A descendant that calls os.setsid() (see
    # test_setsid_escapee_is_not_reaped) leaves the *original* process
    # group by definition, so pgid membership can never observe it -- the
    # pipe it forgot to close is the only externally visible trace of it
    # this process has.
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        orphans_suspected = True
        stop_event.set()
        stdout_thread.join(timeout=_DRAIN_JOIN_S)
        stderr_thread.join(timeout=_DRAIN_JOIN_S)
    for name, thread in (
        ("stdin", stdin_thread),
        ("stdout", stdout_thread),
        ("stderr", stderr_thread),
    ):
        if thread.is_alive():
            # Should not happen given the design above (a selector-polled
            # non-blocking read always returns to check stop_event within
            # _POLL_INTERVAL_S) -- recorded rather than left to leak
            # silently if it ever does.
            warnings.warn(
                f"spawn: {name} pump thread for {argv!r} did not exit", RuntimeWarning, stacklevel=2
            )

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    duration = time.monotonic() - started

    # Timeout wins over parsing: truncated output from a killed process is
    # not a candidate for repair, it is simply a failed round.
    if timed_out:
        return SpawnResult(
            argv,
            process.returncode,
            stdout,
            stderr,
            duration,
            True,
            NormalizeResult(None, ["killed on timeout"], False),
            "timeout",
            orphans_suspected,
        )
    # An abort is not a timeout (timed_out stays False -- nothing here
    # timed out on its own) and, like a timeout, its output is never a
    # candidate for repair: the process was killed mid-run by request, not
    # because it finished and produced something unusable.
    if aborted:
        return SpawnResult(
            argv,
            process.returncode,
            stdout,
            stderr,
            duration,
            False,
            NormalizeResult(None, ["aborted"], False),
            "aborted",
            orphans_suspected,
        )

    result = normalize(
        stdout, envelope=envelope, structured_output=structured_output, contract=contract
    )
    failure_reason = None
    if process.returncode != 0:
        failure_reason = f"exit {process.returncode}"
    elif not result.succeeded:
        failure_reason = "; ".join(result.errors) or "unusable output"
    return SpawnResult(
        argv,
        process.returncode,
        stdout,
        stderr,
        duration,
        False,
        result,
        failure_reason,
        orphans_suspected,
    )

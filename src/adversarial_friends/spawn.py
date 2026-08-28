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

from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
import time
import warnings

from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .envelopes import Envelope, answer_is_complete
from .normalize import NormalizeResult, normalize
from .procgroup import _terminate_group
from .procio import (
    _DRAIN_JOIN_S,
    _POLL_INTERVAL_S,
    _buffer_looks_finished,
    _pump_output,
    _pump_stdin,
)

# Wait windows for group escalation: this long for the group to exit after
# SIGTERM, then (if anything is still alive) this long for it to actually
# disappear after SIGKILL. SIGKILL cannot be blocked or ignored, so anything
# still a *member* of the group cannot survive it -- only a descendant that
# has left the group entirely (e.g. via its own os.setsid()) can outlast
# this, and that is a real, accepted limitation, not a bug here.
GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5
# Per-stream ceiling on what one friend may make this process hold. The
# timeout bounds how LONG a friend runs; without this, nothing bounds how
# much memory it costs.
#
# Per STREAM, per FRIEND -- the run-level figure is larger and worth stating
# rather than leaving a reader to multiply: with ceilings.DEFAULT_MAX_CONCURRENCY
# friends in flight and two streams each, the accumulation bound is
# concurrency x 2 x this number. At the defaults that is 512MiB of captured
# text before the joins below. An earlier version of this comment called
# 32MiB "far below anything that threatens the host" while the sentence above
# it noted friends run concurrently, which is the multiplication it skipped.
#
# In practice a real critique is tens of KB and nothing approaches this; the
# ceiling exists for the friend that loops, and one looping friend costs
# 64MiB, not 512.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


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
    # The friend had already written its whole answer and had not exited.
    # Recorded because the run stopped it deliberately, and a reader
    # comparing durations should not have to guess why one friend's wall
    # clock is shorter than the CLI's own report of itself.
    stopped_after_answer: bool = False
    # The friend produced more than MAX_OUTPUT_BYTES on a stream and was cut
    # off. Recorded rather than inferred from the failure text, because a
    # reader comparing a short stdout against a long duration otherwise has
    # no way to tell truncation from a friend that simply said little.
    output_truncated: bool = False


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
    max_output_bytes: int = MAX_OUTPUT_BYTES,
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
        # Popen raises the same error for a missing executable and a missing
        # working directory, and reporting both as "binary not found" sends
        # a reader hunting for a CLI that is installed. A missing agent CLI
        # is this tool's most common setup problem, so the message has to
        # name the right thing.
        missing = (
            f"binary not found: {argv[0]}"
            if cwd is not None and Path(cwd).is_dir()
            else f"working directory not found: {cwd}"
        )
        return _early_failure(argv, time.monotonic() - started, missing)
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
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    stdout_failed = threading.Event()
    stderr_failed = threading.Event()
    stop_event = threading.Event()
    stdin_thread = threading.Thread(
        target=_pump_stdin, args=(process, stdin_text, stop_event), daemon=True
    )
    stdout_thread = threading.Thread(
        target=_pump_output,
        args=(
            process.stdout,
            stdout_chunks,
            stop_event,
            max_output_bytes,
            stdout_overflow,
            stdout_failed,
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_output,
        args=(
            process.stderr,
            stderr_chunks,
            stop_event,
            max_output_bytes,
            stderr_overflow,
            stderr_failed,
        ),
        daemon=True,
    )
    stdin_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    # Hoisted out of the loop. `answer_is_complete` rejects every ndjson
    # envelope unconditionally, so for those adapters the guard below could
    # never succeed -- while `_buffer_looks_finished` is TRUE on almost every
    # poll, since each NDJSON line ends with `}`. The whole buffer was
    # therefore being joined ~20 times a second to answer a question already
    # settled by the envelope kind.
    early_envelope = envelope if envelope is not None and envelope.kind == "json_path" else None

    deadline = started + timeout_s
    timed_out = False
    aborted = False
    answered = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        if abort_event is not None and abort_event.is_set():
            aborted = True
            break
        if stdout_overflow.is_set():
            # Only stdout ends the wait. A friend flooding stderr is noisy,
            # not unanswerable.
            break
        if (
            early_envelope is not None
            and _buffer_looks_finished(stdout_chunks)
            and answer_is_complete("".join(stdout_chunks), early_envelope)
        ):
            answered = True
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
    # The joined string and the chunk list hold the same bytes twice, and the
    # lists are dead from here on. Dropping them halves peak footprint at the
    # exact moment it is highest -- which matters most for the flooding friend
    # this ceiling exists for.
    stdout_chunks.clear()
    stderr_chunks.clear()
    duration = time.monotonic() - started

    # Output that hit the ceiling is truncated, so it is not a candidate for
    # repair -- the same rule a timeout already follows, and for the same
    # reason: a partial answer that happens to parse would be reported as a
    # whole one. Checked before `timed_out` because a flooding friend often
    # trips both, and the ceiling is the more specific diagnosis.
    # Only a STDOUT overflow discards the answer. One event used to be
    # shared by both pumps, so a friend that answered correctly on stdout and
    # merely chattered on stderr had its valid answer thrown away unparsed --
    # nothing reads stderr for content, so its truncation cannot make the
    # answer wrong. A stderr overflow is recorded and the answer still goes
    # through normalize() below.
    # A stdout pipe that failed mid-stream is truncated for a reason nobody
    # chose, which is exactly the condition the ceiling and the timeout both
    # refuse to parse. Checked with overflow, before any parsing.
    if stdout_failed.is_set():
        return SpawnResult(
            argv,
            process.returncode,
            stdout,
            stderr,
            duration,
            timed_out,
            NormalizeResult(None, ["stdout stream failed before it ended"], False),
            "stdout stream failed",
            orphans_suspected,
            stopped_after_answer=answered,
            output_truncated=True,
        )
    if stdout_overflow.is_set():
        return SpawnResult(
            argv,
            process.returncode,
            stdout,
            stderr,
            duration,
            timed_out,
            NormalizeResult(None, ["stdout exceeded the byte ceiling"], False),
            f"stdout exceeded {max_output_bytes} bytes",
            orphans_suspected,
            stopped_after_answer=answered,
            output_truncated=True,
        )
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
            stopped_after_answer=answered,
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
            stopped_after_answer=answered,
        )

    result = normalize(
        stdout, envelope=envelope, structured_output=structured_output, contract=contract
    )
    failure_reason = None
    # A friend we stopped ourselves cannot be judged by its exit code: the
    # code IS our signal. `answered` means the wait loop broke because the
    # answer was already complete, and the group sweep below it then killed
    # a still-running process -- so a negative (signal) code here is this
    # module's own doing, not the friend's verdict. Reporting it as `exit
    # -15` discarded a payload that had already normalized successfully,
    # turning agy's answer-then-hang path from a slow success into a fast
    # failure. Restricted to negative codes on purpose: a friend that
    # exited nonzero ON ITS OWN in the same instant is still a failure, and
    # that is a real exit status rather than a signal we sent.
    killed_after_answering = answered and process.returncode is not None and process.returncode < 0
    if process.returncode != 0 and not killed_after_answering:
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
        stopped_after_answer=answered,
        output_truncated=stderr_overflow.is_set() or stderr_failed.is_set(),
    )

"""Moving bytes to and from a friend without ever blocking on it.

A friend can spawn descendants that inherit its stdio, and one that calls its
own `os.setsid()` leaves the process group entirely -- so a pipe can stay open
long after the friend itself has exited, and nothing this process does can
force it closed. Every read and write here is therefore non-blocking and
polled with `selectors`, so a pump thread is always back at its stop check
within one `_POLL_INTERVAL_S` and can never be stuck in a syscall it cannot
get out of.

Split out of spawn.py, which had grown past the then-current line cap.
"""

import codecs
import contextlib
import os
import selectors
import subprocess
import threading
import time
from typing import IO

_POLL_INTERVAL_S = 0.05
_READ_CHUNK = 65536
# How long a pump keeps draining after stop_event is set, so a chunk already
# sitting in the kernel buffer is still read. Setting the event asks a pump to
# finish, not to truncate.
_DRAIN_JOIN_S = 2.0


def _pump_stdin(
    process: subprocess.Popen[bytes],
    stdin_text: str | None,
    stop_event: threading.Event,
) -> None:
    """Write the prompt (if any) on its own thread and close stdin.

    Writing synchronously on the main thread before polling starts would risk
    the classic pipe deadlock: a prompt larger than the OS pipe buffer blocks
    our write() until the friend reads more, but the friend may itself be
    blocked writing to its own full stdout pipe if nobody is draining it
    concurrently. Running the write here, alongside the output pumps, avoids
    that.

    Non-blocking for the same reason the output pumps are, which this
    function did not originally share. A plain `write()` blocks once the pipe
    buffer fills and nothing drains it -- and a descendant that inherited fd 0
    and never reads it does exactly that, holding the pipe open after the
    friend itself is gone. The thread is a daemon so the process can still
    exit, but within a run it never returns: a runner dispatching many friends
    accumulates one stuck thread per affected friend, each pinning a copy of
    the prompt. Prompts here carry the whole artifact, so the precondition --
    a prompt larger than the pipe buffer -- is ordinary rather than exotic.
    """
    # process was always constructed with stdin=subprocess.PIPE (see
    # run_process) -- .stdin is only ever None for a Popen that never
    # requested a pipe, which never happens on this code path.
    assert process.stdin is not None
    stream = process.stdin
    try:
        if stdin_text:
            payload = stdin_text.encode("utf-8")
            fd = stream.fileno()
            os.set_blocking(fd, False)
            sel = selectors.DefaultSelector()
            sel.register(fd, selectors.EVENT_WRITE)
            stop_deadline: float | None = None
            offset = 0
            try:
                while offset < len(payload):
                    # Checked every iteration, not only when the pipe is
                    # full: a friend reading slowly must not keep this thread
                    # past the point the run has given up on it.
                    if stop_event.is_set():
                        if stop_deadline is None:
                            stop_deadline = time.monotonic() + _DRAIN_JOIN_S
                        elif time.monotonic() >= stop_deadline:
                            break
                    if not sel.select(timeout=_POLL_INTERVAL_S):
                        continue
                    try:
                        offset += os.write(fd, payload[offset : offset + _READ_CHUNK])
                    except BlockingIOError:
                        continue
                    except OSError:
                        # The friend closed its end, or died. Its problem to
                        # report, not this thread's to retry.
                        break
            finally:
                sel.close()
    except (BrokenPipeError, OSError):
        pass
    finally:
        # EOF matters even when nothing was written: a friend whose prompt
        # travels in argv still waits on stdin otherwise.
        with contextlib.suppress(OSError):
            stream.close()


def _pump_output(
    stream: IO[bytes],
    chunks: list[str],
    stop_event: threading.Event,
    limit: int,
    overflow_event: threading.Event,
    failed_event: threading.Event | None = None,
) -> None:
    """Drain a pipe into chunks (decoded str fragments), never blocking in an
    uninterruptible read.

    The fd is switched to non-blocking and polled with a selector so the loop
    is always back at its stop check within one `_POLL_INTERVAL_S` -- see the
    module docstring for why a plain blocking `readline()` cannot be relied on
    to exit once a descendant has escaped the process group and is holding
    this pipe open. `stop_event` is checked on every iteration, and once set
    the pump keeps draining for `_DRAIN_JOIN_S` so a chunk already sitting in
    the kernel buffer is still read: asking a pump to finish never truncates
    what was already there.

    `limit` caps what this pump ACCUMULATES, not what it reads. Past it the
    pump sets `overflow_event` and goes on draining into the void. Returning
    at the ceiling instead -- which is what this did first -- leaves the pipe
    unread, so the friend blocks writing and dies flushing at exit: a
    complete, valid answer came back as `exit 120`. Reading and discarding
    bounds memory just as well and leaves the child able to exit on its own
    terms.

    `failed_event`, when given, is set if the pipe fails in a way that is NOT
    a clean end of stream. Every OSError used to be treated exactly like EOF,
    so a transport failure and an orderly close were indistinguishable and the
    caller happily parsed whatever bytes had arrived before the failure --
    reporting a fraction of a friend's answer as the whole of it. That is the
    same mistake the byte ceiling and the timeout both exist to prevent, one
    layer further down.
    """
    fd = stream.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    total = 0
    stop_deadline: float | None = None
    sel: selectors.BaseSelector | None = None
    try:
        # Inside the try: selector construction and registration can fail
        # under file-descriptor pressure, and doing them outside meant the
        # `finally` below never ran -- leaving the pipe undrained, so the
        # friend blocked on it until its own timeout and the operator saw a
        # timeout rather than the real cause.
        os.set_blocking(fd, False)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        while True:
            if stop_event.is_set():
                if stop_deadline is None:
                    stop_deadline = time.monotonic() + _DRAIN_JOIN_S
                elif time.monotonic() >= stop_deadline:
                    break
            if sel.select(timeout=_POLL_INTERVAL_S):
                try:
                    raw = os.read(fd, _READ_CHUNK)
                except BlockingIOError:
                    raw = None
                except OSError:
                    # A real read failure, not an orderly close. Recorded so
                    # the caller can refuse to parse a buffer that stopped
                    # arriving for a reason nobody chose.
                    if failed_event is not None:
                        failed_event.set()
                    break
                if raw == b"":
                    break
                if raw:
                    total += len(raw)
                    if total > limit:
                        overflow_event.set()
                        # Discarded at full speed, deliberately. Pacing this
                        # read was tried and reverted: throttling to one
                        # chunk per poll caps drain throughput at roughly a
                        # megabyte a second, so a friend flooding faster than
                        # that blocks on its own write and the round's
                        # duration starts depending on how much it chose to
                        # emit -- which made two tests timing-dependent.
                        # The loop is already bounded without it: EOF, or
                        # stop_event plus the drain window, ends it.
                        continue
                    chunks.append(decoder.decode(raw))
                continue
            if stop_event.is_set():
                break
        chunks.append(decoder.decode(b"", final=True))
    except OSError:
        # Setup failed (fd pressure, an already-closed stream). The stream is
        # still closed below, which is the part that matters: an undrained
        # pipe blocks the friend.
        if failed_event is not None:
            failed_event.set()
    finally:
        if sel is not None:
            sel.close()
        with contextlib.suppress(OSError):
            stream.close()


def _buffer_looks_finished(chunks: list[str]) -> bool:
    """Cheap precondition for the early-answer probe.

    `answer_is_complete` needs one string, and joining the whole buffer on
    every poll is O(n) per poll -- quadratic across a run, which only became
    affordable to ignore while output was unbounded. A complete JSON object
    ends with `}`, and the buffer ends with `}` exactly when its last
    non-blank chunk does, so this settles it without joining anything.
    """
    for chunk in reversed(chunks):
        stripped = chunk.rstrip()
        if stripped:
            return stripped.endswith("}")
    return False

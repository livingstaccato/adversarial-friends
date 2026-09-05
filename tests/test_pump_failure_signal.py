"""A pipe that fails mid-stream is not a clean end of stream.

`_pump_output` treated every OSError from `os.read` exactly like EOF: break,
append the final decode, return. A transport failure and an orderly close
were therefore indistinguishable, and the caller parsed whatever bytes had
arrived before the failure -- reporting a fraction of a friend's answer as
the whole of it. That is the mistake the byte ceiling and the timeout both
exist to prevent, one layer further down.

Selector setup had the same shape of problem: it ran before the try/finally,
so a failure there skipped the stream close, leaving the pipe undrained. The
friend then blocked on it until its own timeout, and the operator saw a
timeout rather than the real cause.
"""

import os
import threading

from afriend import procio


def _pump(stream, chunks, *, limit=1 << 20):
    stop, overflow, failed = (threading.Event() for _ in range(3))
    procio._pump_output(stream, chunks, stop, limit, overflow, failed)
    return overflow, failed


def test_a_read_failure_is_reported_not_treated_as_eof(monkeypatch):
    read_fd, write_fd = os.pipe()
    # Bytes are already waiting, so the selector reports the fd readable
    # immediately and the failing read happens on the first attempt. An
    # earlier version failed only on the SECOND read, which never came: with
    # the write end still open and no more data, the pump correctly sat in
    # select forever and the test hung rather than failing.
    os.write(write_fd, b'{"partial": ')
    reader = os.fdopen(read_fd, "rb")
    target_fd = reader.fileno()
    real_read = os.read

    def failing_read(fd, size):
        # Scoped to THIS pipe. Failing every fd takes pytest's own I/O down
        # with it, which is how the first version of this test hung.
        if fd != target_fd:
            return real_read(fd, size)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", failing_read)
    chunks: list[str] = []
    _overflow, failed = _pump(reader, chunks)
    monkeypatch.undo()
    os.close(write_fd)
    assert failed.is_set(), "a mid-stream read failure must not look like EOF"


def test_a_clean_eof_is_not_reported_as_a_failure():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"no_findings": true}')
    os.close(write_fd)
    reader = os.fdopen(read_fd, "rb")
    chunks: list[str] = []
    _overflow, failed = _pump(reader, chunks)
    assert not failed.is_set()
    assert "".join(chunks) == '{"no_findings": true}'


def test_setup_failure_still_closes_the_stream(monkeypatch):
    """The pipe must be closed even when the pump cannot start, or the friend
    blocks on an undrained pipe and the run reports a timeout instead."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")

    def boom():
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(procio.selectors, "DefaultSelector", boom)
    chunks: list[str] = []
    _overflow, failed = _pump(reader, chunks)
    os.close(write_fd)
    assert reader.closed, "setup failure left the pipe open"
    assert failed.is_set()

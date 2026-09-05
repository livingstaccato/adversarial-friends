"""The stdin pump must not block forever on an inherited pipe (c-0010).

The output pumps were built non-blocking precisely because a descendant that
inherits a friend's stdio can hold a pipe open after the friend itself has
gone. `_pump_stdin` used a plain synchronous `write()`, so the same
descendant holding fd 0 open -- while never reading it -- blocks that write
once the pipe buffer fills. The thread is a daemon, so the process can still
exit, but within a run it never returns: a runner dispatching many friends
accumulates one stuck thread per affected friend, each pinning the prompt
buffer.

A prompt larger than the pipe buffer is the precondition, and prompts here
carry the whole artifact, so it is not a small input.
"""

import os
import threading
import time

from afriend import procio


class _FakeProcess:
    """Just enough Popen surface for the pump: a writable stdin."""

    def __init__(self, fd):
        self.stdin = os.fdopen(fd, "wb")


def test_a_reader_that_never_reads_does_not_pin_the_pump_forever():
    read_fd, write_fd = os.pipe()
    process = _FakeProcess(write_fd)
    # Far larger than any pipe buffer, so the write cannot complete while
    # nothing drains the other end.
    prompt = "p" * (8 * 1024 * 1024)
    stop = threading.Event()
    thread = threading.Thread(target=procio._pump_stdin, args=(process, prompt, stop), daemon=True)
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=procio._DRAIN_JOIN_S + 3)
    alive = thread.is_alive()
    os.close(read_fd)
    assert not alive, "stdin pump ignored stop_event while the pipe was full"


def test_a_normal_prompt_is_still_delivered_whole():
    read_fd, write_fd = os.pipe()
    process = _FakeProcess(write_fd)
    prompt = "hello, friend\n" * 100
    stop = threading.Event()
    thread = threading.Thread(target=procio._pump_stdin, args=(process, prompt, stop), daemon=True)
    thread.start()
    reader = os.fdopen(read_fd, "rb")
    got = reader.read().decode()
    thread.join(timeout=5)
    assert got == prompt
    assert not thread.is_alive()


def test_no_prompt_closes_stdin_immediately():
    """A friend whose prompt travels in argv still needs EOF on stdin, or it
    waits for input that will never come."""
    read_fd, write_fd = os.pipe()
    process = _FakeProcess(write_fd)
    stop = threading.Event()
    thread = threading.Thread(target=procio._pump_stdin, args=(process, None, stop), daemon=True)
    thread.start()
    reader = os.fdopen(read_fd, "rb")
    assert reader.read() == b""
    thread.join(timeout=5)
    assert not thread.is_alive()

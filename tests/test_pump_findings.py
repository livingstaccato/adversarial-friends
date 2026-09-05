"""Fixes from the spawn.py cross-examination: c-0005, c-0006, c-0007/c-0012.

Each was a claim about an assumption the code stated and did not hold.
"""

import json
import sys
import threading
import time

from afriend import spawn
from afriend.envelopes import Envelope

JSON_PATH = Envelope(kind="json_path", path="response")
NDJSON = Envelope(kind="ndjson")

ANSWER = json.dumps({"no_findings": True})


def test_a_noisy_stderr_does_not_discard_a_good_stdout_answer(tmp_path):
    """c-0005. One overflow_event was shared by both pumps, so a friend that
    answered correctly and merely chattered on stderr had its valid answer
    thrown away unparsed. Nothing reads stderr for content, so its truncation
    cannot make the answer wrong."""
    script = (
        "import sys\n"
        f"sys.stdout.write({json.dumps(ANSWER)})\nsys.stdout.flush()\n"
        "chunk = 'e' * 65536\n"
        "for _ in range(200):\n"
        "    sys.stderr.write(chunk)\n"
        "    sys.stderr.flush()\n"
    )
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.result.succeeded is True, outcome.failure_reason
    assert outcome.failure_reason is None
    # Still recorded, so a reader can see stderr was cut off.
    assert outcome.output_truncated is True
    assert len(outcome.stderr) < 2 * 256 * 1024


def test_a_flooded_stdout_still_fails(tmp_path):
    """The other half: stdout IS the answer, so its truncation is fatal."""
    script = (
        "import sys\nchunk='x'*65536\nfor _ in range(200):\n"
        "    sys.stdout.write(chunk)\n    sys.stdout.flush()\n"
    )
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 30, tmp_path, max_output_bytes=256 * 1024
    )
    assert outcome.output_truncated is True
    assert outcome.result.succeeded is False
    assert "stdout exceeded" in (outcome.failure_reason or "")


def test_an_ndjson_run_never_joins_the_buffer(tmp_path, monkeypatch):
    """c-0006. `answer_is_complete` rejects every ndjson envelope
    unconditionally, while `_buffer_looks_finished` is true on almost every
    poll because each NDJSON line ends with `}`. The whole buffer was being
    joined ~20 times a second to answer a question the envelope kind had
    already settled."""
    calls = []
    real = spawn._buffer_looks_finished
    monkeypatch.setattr(spawn, "_buffer_looks_finished", lambda c: calls.append(1) or real(c))
    line = json.dumps({"type": "item.completed", "item": {"text": "x"}})
    script = (
        "import sys,time\n"
        f"for _ in range(6):\n    sys.stdout.write({json.dumps(line)} + chr(10))\n"
        "    sys.stdout.flush()\n    time.sleep(0.1)\n"
    )
    spawn.run_process([sys.executable, "-c", script], None, 20, tmp_path, envelope=NDJSON)
    assert calls == [], "ndjson runs must not reach the buffer check at all"


def test_a_json_path_run_still_stops_early(tmp_path):
    """The hoist must not disable the thing it optimizes."""
    payload = json.dumps({"status": "SUCCESS", "response": ANSWER})
    script = (
        f"import sys,time;sys.stdout.write({json.dumps(payload)});sys.stdout.flush();time.sleep(60)"
    )
    started = time.monotonic()
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 45, tmp_path, envelope=JSON_PATH
    )
    assert outcome.stopped_after_answer is True
    assert time.monotonic() - started < 15


def test_a_pump_stops_promptly_even_while_data_keeps_arriving(tmp_path):
    """c-0007/c-0012. The loop `continue`d straight past its stop_event check
    after any successful read, so a writer trickling bytes forever kept the
    check from ever running. The thread then lived until the byte ceiling
    stopped it -- a bound, but not the one the docstring claimed."""
    chunks: list[str] = []
    stop = threading.Event()
    overflow = threading.Event()
    read_fd, write_fd = __import__("os").pipe()
    import os

    reader = os.fdopen(read_fd, "rb")

    def trickle():
        # Never idle: always another byte ready before the selector times out.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                os.write(write_fd, b"x" * 512)
            except OSError:
                return
            time.sleep(0.005)

    writer = threading.Thread(target=trickle, daemon=True)
    writer.start()
    pump = threading.Thread(
        target=spawn._pump_output,
        args=(reader, chunks, stop, 64 * 1024 * 1024, overflow),
        daemon=True,
    )
    pump.start()
    time.sleep(0.5)
    stop.set()
    pump.join(timeout=spawn._DRAIN_JOIN_S + 3)
    alive = pump.is_alive()
    os.close(write_fd)
    assert not alive, "pump ignored stop_event while data kept arriving"

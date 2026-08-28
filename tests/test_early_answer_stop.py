"""Waiting for a friend that has already answered (§11.3).

agy, on its error path, writes its JSON and then does not exit until its own
`--print-timeout` elapses. Measured across seven cross-examinations: eleven
successful runs exited about 2.5 seconds after the work they reported, and
three hangs exited at 906 seconds having reported 163, 372 and 482 -- seven
to twelve minutes of waiting for an answer already sitting in the pipe.
"""

import json
import sys
import time

from adversarial_friends import spawn
from adversarial_friends.normalize import Envelope, answer_is_complete

JSON_PATH = Envelope(kind="json_path", path="response")
NDJSON = Envelope(kind="ndjson")


def test_a_complete_json_object_is_a_finished_answer():
    whole = json.dumps({"status": "ERROR", "response": "", "error": "timeout"})
    assert answer_is_complete(whole, JSON_PATH) is True


def test_a_half_written_object_is_not():
    """The pump delivers whatever the pipe has: a check that fired on a
    partial write would truncate the answer it exists to preserve."""
    whole = json.dumps({"status": "SUCCESS", "response": "a long answer"})
    assert answer_is_complete(whole[: len(whole) // 2], JSON_PATH) is False


def test_an_ndjson_friend_is_never_stopped_early():
    """codex and opencode stream events, and the answer is a LATER line --
    codex emits a schema-shaped progress message before its real findings.
    Stopping at the first complete object would cut off the thing being
    waited for."""
    progress = json.dumps({"type": "item.completed", "item": {"text": "working"}})
    assert answer_is_complete(progress, NDJSON) is False


def test_trailing_prose_after_the_object_is_not_a_finished_answer():
    """A json_path friend's contract is that stdout IS the object. Anything
    after it means this is not that shape, so the run waits as before."""
    assert answer_is_complete('{"a": 1}\nstill going', JSON_PATH) is False


def test_the_run_stops_waiting_for_a_process_that_has_answered_and_hung(tmp_path):
    """The behaviour, against a real process: it writes a complete object,
    then sleeps far past the runner's patience. Before this the run waited
    for the CLI's own deadline -- up to fifteen minutes for an answer it
    already had."""
    script = (
        "import json,sys,time;"
        "sys.stdout.write(json.dumps({'status':'ERROR','response':'',"
        "'error':'timeout waiting for response'}));"
        "sys.stdout.flush();"
        "time.sleep(60)"
    )
    started = time.monotonic()
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 45, tmp_path, envelope=JSON_PATH
    )
    elapsed = time.monotonic() - started

    assert outcome.stopped_after_answer is True
    assert outcome.timed_out is False
    # It answered immediately; anything near the 45s deadline means the loop
    # waited for the process rather than for the answer.
    assert elapsed < 15, elapsed
    assert "timeout waiting for response" in outcome.stdout


def test_a_process_that_never_answers_still_runs_to_the_deadline(tmp_path):
    """The stop is 'it has answered', not 'it has written something'. A
    friend still working must not be cut off because its output happens to
    parse."""
    script = "import sys,time;sys.stdout.write('working');sys.stdout.flush();time.sleep(60)"
    started = time.monotonic()
    outcome = spawn.run_process(
        [sys.executable, "-c", script], None, 3, tmp_path, envelope=JSON_PATH
    )
    assert outcome.stopped_after_answer is False
    assert outcome.timed_out is True
    assert time.monotonic() - started >= 3


def test_a_missing_working_directory_does_not_read_as_a_missing_binary(tmp_path):
    """Popen raises the same error for both, and calling it "binary not
    found" sends a reader hunting for a CLI that is installed."""
    outcome = spawn.run_process([sys.executable, "-c", "pass"], None, 5, tmp_path / "nope")
    assert outcome.failure_reason is not None
    assert "working directory not found" in outcome.failure_reason, outcome.failure_reason

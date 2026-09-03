"""Live progress on stderr.

The feature exists because a crossexam is silent for tens of minutes and a
silent run cannot be told from a hung one. So the tests that matter are the
ones about what happens when nothing is happening -- the heartbeat -- and
the one about the stream it must never touch.
"""

import io
import time

from adversarial_friends import progress
from adversarial_friends.events import read_events
from adversarial_friends.runstore import RunStore


def _lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def _wait_for(stream: io.StringIO, needle: str, limit: float = 5.0) -> None:
    """Poll rather than sleep a fixed amount.

    A fixed sleep either flakes on a loaded machine or pads every run to the
    worst case. The generous ceiling only applies when the thing never
    arrives, which is a failure anyway."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if any(needle in line for line in _lines(stream)):
            return
        time.sleep(0.02)


def test_a_disabled_reporter_says_nothing():
    """`disabled()` exists so call sites stay unconditional. If it emitted
    anything, every one of those call sites would need a guard back."""
    stream = io.StringIO()
    reporter = progress.disabled()
    reporter.stream = stream
    reporter.round_started(1, "critique", ["a", "b"])
    reporter.friend_dispatched("a", 900)
    reporter.friend_finished("a", "answered with 3 claims")
    reporter.round_finished(1, "1/1 friends answered")
    assert _lines(stream) == []


def test_progress_never_writes_to_stdout(capsys):
    """The contract this feature must not break: `afriend run` prints the
    run directory on stdout and nothing else, so `cd "$(afriend run x.md)"`
    works. A progress line on that stream corrupts the one machine-readable
    thing the command produces."""
    reporter = progress.Progress(heartbeat_s=0.01)
    reporter.round_started(1, "critique", ["fake-ok-0"])
    reporter.friend_dispatched("fake-ok-0", 900)
    reporter.friend_finished("fake-ok-0", "answered with 3 claims")
    reporter.round_finished(1, "1/1 friends answered, 3 claims")
    reporter.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fake-ok-0" in captured.err


def test_a_finished_friend_is_named_with_its_outcome_and_duration():
    stream = io.StringIO()
    reporter = progress.Progress(stream=stream)
    reporter.friend_dispatched("codex-security-0", 900)
    reporter.friend_finished("codex-security-0", "answered with 6 claims")
    line = _lines(stream)[-1]
    assert "codex-security-0" in line
    assert "answered with 6 claims" in line


def test_the_heartbeat_names_what_is_still_outstanding():
    """The line that does the actual work. Lifecycle lines fire when
    something happens, and the failure being diagnosed is that nothing is
    happening -- so only this one distinguishes a slow friend from a dead
    one."""
    stream = io.StringIO()
    reporter = progress.Progress(stream=stream, heartbeat_s=0.05)
    reporter.round_started(1, "critique", ["codex-security-0"])
    reporter.friend_dispatched("codex-security-0", 900)
    _wait_for(stream, "still waiting")
    reporter.close()
    waiting = [line for line in _lines(stream) if "still waiting" in line]
    assert waiting, stream.getvalue()
    assert "codex-security-0" in waiting[0]
    # Both numbers, because "6m elapsed" alone does not say whether that is
    # nearly over or barely started.
    assert "elapsed" in waiting[0]
    assert "timeout" in waiting[0]


def test_a_finished_friend_is_not_named_by_the_heartbeat():
    """Otherwise the heartbeat reports a stall that already ended, which is
    worse than silence -- it sends a reader looking for a problem that is
    not there."""
    stream = io.StringIO()
    reporter = progress.Progress(stream=stream, heartbeat_s=0.05)
    reporter.round_started(1, "critique", ["a", "b"])
    reporter.friend_dispatched("a", 900)
    reporter.friend_dispatched("b", 900)
    reporter.friend_finished("a", "answered with 1 claim")
    _wait_for(stream, "still waiting")
    reporter.close()
    waiting = [line for line in _lines(stream) if "still waiting" in line]
    assert waiting, stream.getvalue()
    assert all("b" in line for line in waiting)
    assert not any("still waiting: a " in line for line in waiting)


def test_a_forgotten_friend_is_not_named_either():
    """The deliberate-stop path: an AfError tears the round down around a
    friend that never produced an outcome."""
    stream = io.StringIO()
    reporter = progress.Progress(stream=stream, heartbeat_s=0.05)
    reporter.round_started(1, "critique", ["a"])
    reporter.friend_dispatched("a", 900)
    reporter.friend_forgotten("a")
    time.sleep(0.2)
    reporter.close()
    assert not [line for line in _lines(stream) if "still waiting" in line]


def test_the_heartbeat_thread_stops_with_the_round():
    """A daemon thread that outlived its round would keep narrating friends
    that are no longer running, interleaved with whatever came next."""
    reporter = progress.Progress(heartbeat_s=0.05, stream=io.StringIO())
    reporter.round_started(1, "critique", ["a"])
    beat = reporter._beat
    assert beat is not None and beat.is_alive()
    reporter.round_finished(1, "done")
    assert not beat.is_alive()


def test_a_broken_stream_disables_progress_rather_than_raising():
    """A closed pipe -- `afriend run ... 2>&1 | head` -- must not cost a
    round of review that has already spent several minutes of somebody's
    CLI quota. Progress is the least important thing in the process."""
    stream = io.StringIO()
    stream.close()
    reporter = progress.Progress(stream=stream)
    reporter.note("still fine")
    assert reporter.enabled is False


def test_durations_are_reported_at_the_scale_friends_run_at():
    """357s is the real measurement that motivated this module. Reporting
    it in seconds makes a reader do the division."""
    assert progress.format_duration(9) == "9s"
    assert progress.format_duration(59) == "59s"
    assert progress.format_duration(60) == "1m00s"
    assert progress.format_duration(357.1) == "5m57s"
    assert progress.format_duration(900) == "15m00s"


def test_progress_writes_safe_lifecycle_events_without_touching_human_output(tmp_path):
    stream = io.StringIO()
    store = RunStore(tmp_path / "runs", "run-progress")
    reporter = progress.Progress(stream=stream, event_writer=store.events_writer())
    reporter.run_started("report", "quick")
    reporter.round_started(1, "critique", ["fake-security-0"])
    reporter.friend_dispatched("fake-security-0", 900, provider="fake", lens="security")
    reporter.friend_finished("fake-security-0", "answered with 1 claim", succeeded=True)
    reporter.round_finished(1, "1/1 friends answered", status="completed")
    reporter.run_finished("completed", "inspect_report", duration_s=0.1)
    reporter.close()
    events = read_events(store.events_path(), root=store.root)
    assert [(event.type, event.payload["status"]) for event in events] == [
        ("run_started", "started"),
        ("friend_finished", "succeeded"),
        ("round_finished", "completed"),
        ("run_finished", "completed"),
    ]
    assert events[1].payload["provider"] == "fake"
    assert events[1].payload["lens"] == "configured"
    assert "answered with 1 claim" not in store.events_path().read_text()


def test_lifecycle_events_redact_an_arbitrary_lens_and_are_observational(tmp_path):
    class BrokenWriter:
        def append(self, _event):
            raise RuntimeError("telemetry storage failed")

    store = RunStore(tmp_path / "runs", "run-progress-redaction")
    reporter = progress.Progress(event_writer=store.events_writer())
    reporter.friend_dispatched(
        "fake-secret-0",
        900,
        provider="fake",
        lens="token=super-secret",
    )
    reporter.friend_finished("fake-secret-0", "answered with 1 claim")
    events = read_events(store.events_path(), root=store.root)
    assert events[0].payload["lens"] == "configured"
    assert "token=super-secret" not in store.events_path().read_text()

    reporter.event_writer = BrokenWriter()  # type: ignore[assignment]
    reporter.round_finished(1, "still completes")

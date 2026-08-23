import os
import signal
import sys
from pathlib import Path

import pytest

from adversarial_friends import spawn

FAKE = str(Path(__file__).resolve().parent / "fake_friend.py")


def test_successful_run_is_marked_succeeded():
    result = spawn.run_process([sys.executable, FAKE, "good"], None, 30, Path.cwd())
    assert result.exit_code == 0
    assert result.result.succeeded is True


def test_nonzero_exit_is_a_failure():
    result = spawn.run_process([sys.executable, FAKE, "crash"], None, 30, Path.cwd())
    assert result.exit_code == 1
    assert result.failure_reason


def test_exit_zero_with_offtopic_output_is_a_failure():
    """Verified against agy: exit 0 while answering an entirely different prompt."""
    result = spawn.run_process([sys.executable, FAKE, "offtopic"], None, 30, Path.cwd())
    assert result.exit_code == 0
    assert result.result.succeeded is False
    assert result.failure_reason


def test_empty_findings_without_marker_is_a_failure():
    result = spawn.run_process([sys.executable, FAKE, "empty"], None, 30, Path.cwd())
    assert result.result.succeeded is False


def test_no_findings_marker_is_a_success():
    result = spawn.run_process([sys.executable, FAKE, "no_findings"], None, 30, Path.cwd())
    assert result.result.succeeded is True


def test_timeout_kills_the_whole_process_group(tmp_path):
    """Corrected from the brief: rather than parsing a child pid out of
    stdout captured *after* a SIGKILL (not guaranteed to be flushed/read by
    then), have the fake friend write its child's pid to a file passed via
    argv before it hangs, then assert on that file once the timeout has been
    handled. run_process() only returns once the group has been confirmed
    gone (see spawn._wait_until_gone), so no extra sleep is needed here to
    avoid a race against the kill.
    """
    pidfile = tmp_path / "child.pid"
    result = spawn.run_process(
        [sys.executable, FAKE, "hang", str(pidfile)], None, 2, Path.cwd()
    )
    assert result.timed_out is True
    child_pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGTERM)  # already reaped


def test_timeout_takes_precedence_over_parsing():
    """A killed friend is a failure regardless of what it managed to print."""
    result = spawn.run_process([sys.executable, FAKE, "hang"], None, 2, Path.cwd())
    assert result.failure_reason == "timeout"


# --- Adversarial reaping probes (not in the brief) ---
#
# The brief's two verified hazards are (1) descendants must not survive a
# timeout and (2) exit status is not evidence of success. The task asked for
# an active attempt to defeat the reaping in (1) beyond the single hang/child
# case the required tests cover. Each probe below targets one specific way a
# real agent CLI's process tree could misbehave. Results (which are reaped,
# which escape) are recorded in task-8-report.md; the honest outcome for
# `test_setsid_escapee_is_not_reaped` is that it escapes -- process groups
# cannot reach a descendant that gives itself a new session, and this test
# exists to prove that limitation rather than hide it.


def test_grandchild_is_reaped_through_two_levels(tmp_path):
    """child spawns its own child (a grandchild relative to the runner);
    neither calls setsid, so both stay in the friend's process group and
    killpg must reach both."""
    pidfile_child = tmp_path / "child.pid"
    pidfile_grandchild = tmp_path / "grandchild.pid"
    result = spawn.run_process(
        [sys.executable, FAKE, "grandchild", str(pidfile_child), str(pidfile_grandchild)],
        None, 2, Path.cwd(),
    )
    assert result.timed_out is True
    child_pid = int(pidfile_child.read_text().strip())
    grandchild_pid = int(pidfile_grandchild.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGTERM)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, signal.SIGTERM)


def test_sigterm_ignoring_friend_is_still_killed(tmp_path):
    """The friend itself ignores SIGTERM; SIGKILL cannot be ignored, so
    escalation must still finish it off within the grace windows."""
    pidfile = tmp_path / "self.pid"
    result = spawn.run_process(
        [sys.executable, FAKE, "ignore_sigterm", str(pidfile)], None, 2, Path.cwd(),
    )
    assert result.timed_out is True
    pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def test_closing_stdout_early_does_not_hang_the_runner():
    """A friend that closes stdout and then hangs must not cause the runner
    itself to block waiting for pipe EOF that will never come from that fd;
    the runner still has to notice the process never exits."""
    result = spawn.run_process(
        [sys.executable, FAKE, "close_stdout_then_hang"], None, 2, Path.cwd(),
    )
    assert result.timed_out is True
    assert result.failure_reason == "timeout"


def test_exit0_with_leftover_descendant_is_reaped(tmp_path):
    """The friend prints a valid payload and exits 0 immediately, without
    waiting on a child it spawned. Nothing timed out, so hazard (1)'s
    timeout-triggered cleanup never fires on its own -- the runner must
    still sweep the process group after a clean exit, or the descendant
    (an MCP server, in the real-world case this hazard is modeled on) keeps
    running past the point the round was marked complete."""
    pidfile = tmp_path / "descendant.pid"
    result = spawn.run_process(
        [sys.executable, FAKE, "exit0_leaves_descendant", str(pidfile)], None, 30, Path.cwd(),
    )
    assert result.timed_out is False
    assert result.exit_code == 0
    descendant_pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, signal.SIGTERM)


def test_setsid_escapee_is_not_reaped(tmp_path):
    """Honest negative result: a descendant that calls os.setsid() before
    the runner intervenes leaves the friend's process group entirely and
    forms its own session/group. killpg on the original group can never
    reach it. This is a real, accepted limitation of process-group-based
    reaping (the fix would require OS-level containment: cgroups, a Windows
    job object, or a pid namespace), not a bug in this runner. The test
    proves the limitation and then cleans the process up itself so it
    doesn't leak past the test suite."""
    pidfile = tmp_path / "escapee.pid"
    result = spawn.run_process(
        [sys.executable, FAKE, "escape", str(pidfile)], None, 2, Path.cwd(),
    )
    assert result.timed_out is True
    escapee_pid = int(pidfile.read_text().strip())
    # No ProcessLookupError here: the escapee is still alive. Demonstrate
    # that, then kill it directly (not via the runner) so the test suite
    # doesn't leave a stray sleeping process behind.
    os.kill(escapee_pid, 0)  # does not raise: still alive
    os.kill(escapee_pid, signal.SIGKILL)


def test_nonzero_exit_with_otherwise_valid_output_is_still_a_failure():
    """Diagnosticity gap found while verifying the brief's own
    test_nonzero_exit_is_a_failure: "crash" mode prints nothing to stdout at
    all (only "boom" on stderr), so that test would still pass even if the
    exit-code check in run_process were deleted entirely -- normalize()
    would already report failure on empty/unparseable stdout for an
    unrelated reason, masking the exit-code branch having no effect.
    Confirmed by mutation: deleting the `if process.returncode != 0` branch
    left test_nonzero_exit_is_a_failure green. This test isolates the
    exit-code check specifically by pairing a nonzero exit with output that
    would otherwise parse as a full success."""
    result = spawn.run_process(
        [sys.executable, "-c",
         'import json,sys; print(json.dumps({"no_findings": True})); sys.exit(1)'],
        None, 30, Path.cwd(),
    )
    assert result.exit_code == 1
    assert result.result.succeeded is True  # normalize() alone would call this fine
    assert result.failure_reason == "exit 1"

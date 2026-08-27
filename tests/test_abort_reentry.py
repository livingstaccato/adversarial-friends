"""A second signal during abort must not deadlock the abort."""

from pathlib import Path
import signal
import subprocess
import sys

PROBE = Path(__file__).with_name("abort_reentry_probe.py")


def test_a_second_signal_during_abort_does_not_deadlock():
    """Found as a two-day-old `afriend` process: five nested invocations of
    the abort handler on the main thread, the innermost parked on
    `abort_event`'s lock, held by the invocation beneath it. GNU coreutils
    `timeout` alone delivers SIGTERM twice (once to the pid, once to the
    process group), so one impatient caller is enough. The probe forces the
    exact interleaving; without a re-entrancy guard it never exits.
    """
    proc = subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"handled {int(signal.SIGTERM)} True"

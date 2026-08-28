"""`os.killpg` raising EPERM must not crash the round (c-0011).

`_signal_group` caught only ProcessLookupError, so a PermissionError from
`os.killpg` propagated out of `_terminate_group` and out of `run_process`
itself -- taking down the friend whose answer had, in the reported case,
already been written and parsed.

Observed live before it was understood: a full test run failed with
`PermissionError: [Errno 1] Operation not permitted`, which was initially
put down to interference from a concurrent run. agy raised it independently
in a cross-examination of spawn.py, which is what prompted actually reading
the exception handling.

EPERM and ESRCH mean opposite things here and must not be collapsed:
- ESRCH (ProcessLookupError) -- nothing has that pgid. The group is gone,
  so there is nothing to reap and no orphan to suspect.
- EPERM (PermissionError) -- something DOES have that pgid and the kernel
  refused the signal. The group was not reaped, so an orphan is exactly
  what should be suspected. Reporting that as "gone" would claim a cleanup
  that did not happen.
"""

import os
import signal
import subprocess
import sys

import pytest

from adversarial_friends import procgroup


@pytest.fixture
def dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc


def test_a_denied_signal_does_not_raise(monkeypatch, dead_process):
    def denied(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", denied)
    # Must return, not raise. Before the fix this propagated all the way out
    # of run_process.
    assert procgroup._terminate_group(dead_process, dead_process.pid) is True


def test_a_denied_signal_reports_orphans_suspected(monkeypatch, dead_process):
    """The return value feeds run_process's orphans_suspected. A refused
    signal means the group was NOT reaped."""

    def denied(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", denied)
    assert procgroup._terminate_group(dead_process, dead_process.pid) is True


def test_a_vanished_group_is_not_an_orphan(monkeypatch, dead_process):
    """ESRCH keeps its old meaning: nothing to signal is the common case for
    a friend with no children, and must stay a cheap no-op."""

    def gone(pgid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "killpg", gone)
    assert procgroup._terminate_group(dead_process, dead_process.pid) is False


def test_signal_group_distinguishes_gone_from_denied(monkeypatch, dead_process):
    monkeypatch.setattr(os, "killpg", lambda p, s: None)
    assert procgroup._signal_group(dead_process.pid, signal.SIGTERM) == procgroup.SENT

    def gone(pgid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "killpg", gone)
    assert procgroup._signal_group(dead_process.pid, signal.SIGTERM) == procgroup.GONE

    def denied(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", denied)
    assert procgroup._signal_group(dead_process.pid, signal.SIGTERM) == procgroup.DENIED

"""§7.6's exit precedence, and `--require-friends` (c-0013) within it.

decide_exit had no direct unit coverage before this -- every exit code was
only ever observed through a full end-to-end run. That was enough to catch
a wrong number, but not to pin the PRECEDENCE between two conditions that
can hold at once, which is the part a full run rarely exercises both sides
of in one case.
"""

from adversarial_friends.commands.exits import decide_exit
from adversarial_friends.errors import CeilingError, QuorumError


def test_a_clean_run_exits_zero():
    assert decide_exit(None, True, "report", None, []) == 0


def test_no_friend_succeeding_exits_one():
    assert decide_exit(None, False, "report", None, []) == 1


def test_a_signal_outranks_everything():
    assert decide_exit(15, True, "report", None, []) == 128 + 15


def test_a_ceiling_outranks_quorum():
    """A truncated run has not evaluated anything, including whether it
    met quorum -- 11 says "retry", and reporting 12 instead would tell a
    caller the wrong thing to do about it."""
    got = decide_exit(
        None,
        True,
        "report",
        None,
        [],
        ceiling_hit="budget-exhausted",
        succeeded_friends=1,
        require_friends=5,
    )
    assert got == CeilingError.exit_code


# --- --require-friends (c-0013) ---------------------------------------------


def test_below_quorum_fails_even_though_any_success_is_true():
    """The defect exactly: any_success is True the moment ONE friend
    answers, so a run with 1 of 50 succeeding exited 0 identically to one
    with 50 of 50 -- the report said plainly it was a single opinion, but
    nothing in the exit code carried that."""
    got = decide_exit(None, True, "report", None, [], succeeded_friends=1, require_friends=5)
    assert got == QuorumError.exit_code
    assert QuorumError.exit_code == 12


def test_at_quorum_passes():
    got = decide_exit(None, True, "report", None, [], succeeded_friends=5, require_friends=5)
    assert got == 0


def test_above_quorum_passes():
    got = decide_exit(None, True, "report", None, [], succeeded_friends=8, require_friends=5)
    assert got == 0


def test_unset_require_friends_is_never_enforced():
    """Opt-in. The default None must never trip the check regardless of
    how few friends succeeded."""
    got = decide_exit(None, True, "report", None, [], succeeded_friends=1, require_friends=None)
    assert got == 0


def test_unknown_succeeded_friends_fails_open_not_closed():
    """A resumed `--merge orchestrator` run applies stored merges and goes
    straight to judging -- no fresh critique round exists in that process
    to count. Reporting a quorum failure on a run that may have met quorum
    before the halt would be worse than not checking: it fails a run for a
    number the process never actually saw."""
    got = decide_exit(None, True, "report", None, [], succeeded_friends=None, require_friends=5)
    assert got == 0


def test_quorum_outranks_gate_completeness():
    """A run below the operator's declared quorum has not produced the
    review its exit code would otherwise claim -- whether the few claims
    it DID get are all resolved is beside the point."""
    from adversarial_friends.ledger import Claim

    blocking = [
        Claim(
            id="c-0001@1",
            supersedes=None,
            origin=["fake/ops"],
            lens="ops",
            round=1,
            advisory=False,
            severity="high",
            claim="x",
            location=None,
            evidence="e",
            failure_scenario="f",
            suggested_fix="s",
        )
    ]
    got = decide_exit(
        None,
        True,
        "gate",
        None,
        blocking,
        succeeded_friends=1,
        require_friends=5,
    )
    assert got == QuorumError.exit_code

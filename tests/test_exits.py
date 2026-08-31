"""Console exit glue prints an already-decided RunOutcome; it never decides."""

import pytest

from adversarial_friends.commands.exits import decide_exit
from adversarial_friends.outcomes import terminal_outcome


def _outcome(**facts):
    return terminal_outcome(
        mode=facts.pop("mode", "report"),
        converged=facts.pop("converged", False),
        loop_exhausted=facts.pop("loop_exhausted", False),
        budget_reason=facts.pop("budget_reason", None),
        blocking_ids=facts.pop("blocking_ids", []),
        any_success=facts.pop("any_success", True),
        unresolved=facts.pop("unresolved", False),
        **facts,
    )


@pytest.mark.parametrize(
    ("run_outcome", "expected"),
    [
        (_outcome(), 0),
        (_outcome(any_success=False), 1),
        (_outcome(any_success=False, quorum_failed=True), 1),
        (_outcome(abort_signum=15), 143),
        (_outcome(budget_reason="--max-calls reached"), 11),
        (_outcome(quorum_failed=True), 12),
        (_outcome(mode="gate", blocking_ids=["c-0001@1"]), 1),
    ],
)
def test_decide_exit_returns_the_outcomes_exact_code(run_outcome, expected):
    assert decide_exit(run_outcome) == expected


def test_signal_message_is_derived_from_the_outcome(capsys):
    decide_exit(_outcome(abort_signum=15))
    assert "aborted by signal 15" in capsys.readouterr().err


def test_gate_message_uses_the_outcomes_blocker_ids(capsys):
    decide_exit(_outcome(mode="gate", blocking_ids=["c-0002@1", "c-0001@1"]))
    message = capsys.readouterr().err
    assert "gate blocked" in message
    assert "c-0002@1, c-0001@1" in message


def test_optional_detail_is_output_only(capsys):
    completed = _outcome()
    assert decide_exit(completed, detail="diagnostic only") == 0
    assert "diagnostic only" in capsys.readouterr().err

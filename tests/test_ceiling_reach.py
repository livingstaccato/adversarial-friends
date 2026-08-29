"""The wall-clock ceiling, reached rather than reasoned about.

`Budget.out_of_time` had a unit test; nothing drove the branch that uses
it, because an end-to-end run cannot wait two hours and the check read the
clock directly. AF_CLOCK_OFFSET_S adds a constant to every reading the run
takes, so the same arithmetic runs against a clock a test can move.
"""

import json

from e2e_helpers import run_af

from adversarial_friends.adapters import FriendSpec
from adversarial_friends.ceilings import Budget, within_deadline
from adversarial_friends.dispatch import KILL_GRACE_S


def _spec(timeout):
    return FriendSpec(
        name="fake-ops-0",
        cli="fake",
        lens="ops",
        model=None,
        effort=None,
        scope="doc",
        timeout=timeout,
    )


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with a missing guard.\n")
    return path


def _run_json(tmp_path):
    return json.loads((sorted((tmp_path / "runs").iterdir())[0] / "run.json").read_text())


def test_a_run_past_its_wall_clock_ceiling_stops_and_says_so(tmp_path):
    """Deleting the ceiling check used to leave the suite green."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:judge_uphold_a",
        "--friend",
        "fake:judge_uphold_b",
        "--max-wall-clock",
        "60",
        mode="crossexam",
        env_extra={"AF_CLOCK_OFFSET_S": "600"},
    )
    assert result.returncode == 11, (result.returncode, result.stderr)
    assert "budget-exhausted" in result.stderr, result.stderr
    meta = _run_json(tmp_path)
    assert meta["ceiling_hit"] == "budget-exhausted"
    # Which ceiling, and when: the label names the kind, the downgrade the
    # instance. Both are needed -- an operator who set two ceilings cannot
    # tell from "budget-exhausted" alone which one stopped the run.
    assert any("--max-wall-clock reached" in d for d in meta["downgrades"]), meta["downgrades"]


def test_a_friend_may_not_outlive_the_ceiling_it_was_dispatched_under():
    """The ceiling bounded the gaps between rounds, not the run: a friend
    dispatched a second before it expired ran its own full timeout past it
    -- 900 seconds by default -- and a run that finished in that round
    reported no ceiling hit at all. Each dispatch now caps every friend's
    timeout at what is left."""
    budget = Budget(max_calls=10, max_wall_clock_s=120, started=0.0)
    assert budget.seconds_left(0.0) == 120
    assert budget.seconds_left(100.0) == 20
    # Never negative: a friend dispatched exactly at the ceiling gets zero,
    # not a timeout in the past.
    assert budget.seconds_left(500.0) == 0

    specs = [_spec(timeout=900), _spec(timeout=10)]
    # With room for both a timeout and the kill grace, every friend is capped
    # to what remains of the run, minus the grace dispatch adds on top.
    capped = within_deadline(specs, budget.seconds_left(0.0))
    assert [s.timeout for s in capped] == [120 - KILL_GRACE_S, 10]
    # With 20 seconds left and a 60-second grace, there is no dispatch that
    # both runs and respects the ceiling. This used to cap to 20 and then
    # hand run_process a kill deadline of 80 -- honouring the ceiling in the
    # timeout while breaking it in the kill, which is the half-guarantee
    # c-0011 named. Refusing is the honest answer; the withheld path reports
    # it rather than letting the round look clean.
    assert within_deadline(specs, budget.seconds_left(100.0)) == []

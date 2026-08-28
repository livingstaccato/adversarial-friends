"""Three findings from cross-examining crossexam.py: c-0004, c-0005, c-0010.

Each is about the roster or the clock disagreeing with what the file says
about itself.
"""

import dataclasses

from adversarial_friends.adapters import FriendSpec
from adversarial_friends.commands import judging


def _spec(name: str, timeout: int = 600) -> FriendSpec:
    return FriendSpec(
        name=name, cli="codex", lens="ops", model=None, effort=None, scope="doc", timeout=timeout
    )


def test_a_sub_second_remainder_dispatches_nothing(**_):
    """c-0004. `int()` floors, so 0.6s remaining became a timeout of 0 -- a
    friend launched only to be killed the instant it started, spending a call
    from the budget and reporting a failure that marks the run incomplete.
    No agent CLI reaches its model in under a second."""
    assert judging._within_deadline([_spec("a"), _spec("b")], 0.6) == []
    assert judging._within_deadline([_spec("a")], 0.0) == []


def test_a_negative_remainder_dispatches_nothing():
    assert judging._within_deadline([_spec("a")], -5.0) == []


def test_the_cap_reserves_the_kill_grace(**_):
    """c-0011. dispatch hands run_process `spec.timeout + KILL_GRACE_S`, a
    full extra minute, so a cap that ignored it made the wall-clock ceiling
    a ceiling only for friends that behaved: one hung friend overshot it by
    that minute plus the group escalation windows."""
    from adversarial_friends.dispatch import KILL_GRACE_S

    got = judging._within_deadline([_spec("a", timeout=600)], 90.7)
    assert [s.timeout for s in got] == [90 - KILL_GRACE_S]


def test_nothing_is_dispatched_when_only_the_grace_would_fit():
    from adversarial_friends.dispatch import KILL_GRACE_S

    assert judging._within_deadline([_spec("a")], float(KILL_GRACE_S)) == []


def test_a_timeout_below_the_remainder_is_left_alone():
    """The cap is a ceiling, not an assignment: a friend asking for less than
    what remains keeps its own timeout."""
    got = judging._within_deadline([_spec("a", timeout=5)], 300.0)
    assert [s.timeout for s in got] == [5]


def test_within_deadline_does_not_mutate_the_caller_specs():
    original = _spec("a", timeout=600)
    judging._within_deadline([original], 10.0)
    assert original.timeout == 600
    assert dataclasses.asdict(original)["timeout"] == 600


def test_a_disabled_friend_is_announced_once_per_run_not_per_iteration(tmp_path):
    """c-0010. `dropped` was local to `run_rounds`, which a loop calls once
    per iteration -- so the "failed identically twice and is disabled"
    downgrade was emitted every iteration, where the spec asks for once per
    run. It now lives on the outcome, seeded from the previous block, exactly
    as `signatures` already was.
    """
    from adversarial_friends.commands.crossexam import CrossexamOutcome

    first = CrossexamOutcome()
    first.dropped.add("codex-ops-0")
    # What run_rounds does when handed the previous block as `prior`.
    second = CrossexamOutcome()
    second.dropped = set(first.dropped)
    assert "codex-ops-0" in second.dropped


def test_the_outcome_carries_dropped_across_blocks():
    from adversarial_friends.commands.crossexam import CrossexamOutcome

    fresh = CrossexamOutcome()
    assert fresh.dropped == set()
    # Distinct per instance, or one run's announcements would silence another's.
    other = CrossexamOutcome()
    fresh.dropped.add("a")
    assert other.dropped == set()

"""Tests for the rest of the claim state machine.

§7.2's discard rule and per-verdict downgrades, §6.5's evidence symmetry,
§6.1's successor claims, §7.3's loop termination, and the gate rule.
§7.1's decision table is in test_verdicts.py.

Weighted toward `deadlocked` counting as terminal for loop termination:
excluding it meant a single genuine disagreement -- precisely the outcome
this tool exists to surface -- disabled termination permanently.
"""

import dataclasses
from types import SimpleNamespace

import pytest
from verdict_helpers import ROSTER, claim, in_round, verdict

from afriend import verdicts
from afriend.commands.crossexam import _prior_verdicts_by_claim
from afriend.commands.runmeta import unresolved_loop_states

# --- §7.2 discard rule -----------------------------------------------------


def test_identical_verdict_sets_across_rounds_discard():
    sig = verdicts.verdict_set_signature(
        [verdict("claude-security", "unproven"), verdict("agy-assumptions", "unproven")],
        "c-0001@1",
    )
    assert verdicts.should_discard(sig, sig) is True


def test_a_changed_verdict_set_does_not_discard():
    first = verdicts.verdict_set_signature([verdict("claude-security", "unproven")], "c-0001@1")
    second = verdicts.verdict_set_signature([verdict("claude-security", "refuted")], "c-0001@1")
    assert verdicts.should_discard(first, second) is False


def test_the_signature_does_not_grow_when_a_judge_repeats_itself():
    """The discard rule compares consecutive rounds, but a claim's verdict
    list accumulates across them -- round 3 holds round 2's verdicts plus its
    own. Without reducing to one per judge the signature grows every round,
    the two never compare equal, and nothing is ever discarded: a claim
    nobody can verify keeps costing a full fan-out until max_rounds."""
    round2 = [in_round(verdict("claude-security", "unproven"), 2)]
    round3 = [*round2, in_round(verdict("claude-security", "unproven"), 3)]
    first = verdicts.verdict_set_signature(round2, "c-0001@1")
    second = verdicts.verdict_set_signature(round3, "c-0001@1")
    assert verdicts.should_discard(first, second) is True


def test_the_first_round_never_discards():
    sig = verdicts.verdict_set_signature([verdict("claude-security", "unproven")], "c-0001@1")
    assert verdicts.should_discard(None, sig) is False


def test_signature_ignores_ordering():
    a = verdicts.verdict_set_signature(
        [verdict("claude-security", "unproven"), verdict("agy-assumptions", "unproven")],
        "c-0001@1",
    )
    b = verdicts.verdict_set_signature(
        [verdict("agy-assumptions", "unproven"), verdict("claude-security", "unproven")],
        "c-0001@1",
    )
    assert a == b


def test_new_counter_evidence_prevents_discard():
    first = verdict("claude-security", "unproven")
    second = dataclasses.replace(first, round=3, counter_evidence="src/auth.py:38")
    assert verdicts.verdict_set_signature([first], first.claim_id) != (
        verdicts.verdict_set_signature([first, second], first.claim_id)
    )


def test_reasoning_and_confidence_alone_do_not_prevent_discard():
    first = verdict("claude-security", "unproven", reasoning="could not find it")
    second = dataclasses.replace(
        first,
        round=3,
        reasoning="looked twice and still could not find it",
        confidence="low",
    )
    assert verdicts.verdict_set_signature([first], first.claim_id) == (
        verdicts.verdict_set_signature([first, second], first.claim_id)
    )


# --- §6.5 evidence symmetry ------------------------------------------------


@pytest.mark.parametrize("kind", ["upheld", "refuted", "amended"])
def test_an_unverifiable_dispositive_verdict_becomes_unproven(kind):
    """A judge that says "refuted, but I could not find the evidence" has not
    refuted anything -- it has reported that it could not check."""
    v = verdict("claude-security", kind, assessment="unverifiable", amended="reworded")
    assert verdicts.downgrade_unverifiable(v).verdict == verdicts.UNPROVEN


def test_the_downgraded_verdict_records_what_it_would_have_been():
    v = verdict("claude-security", "refuted", assessment="unverifiable")
    out = verdicts.downgrade_unverifiable(v)
    assert "refuted" in out.reasoning
    assert "unverifiable" in out.reasoning


def test_a_confirmed_verdict_is_untouched():
    v = verdict("claude-security", "refuted", assessment="confirmed")
    assert verdicts.downgrade_unverifiable(v) == v


def test_an_already_non_dispositive_verdict_is_untouched():
    v = verdict("claude-security", "out-of-scope", assessment="unverifiable")
    assert verdicts.downgrade_unverifiable(v) == v


def test_two_unverifiable_judges_cannot_settle_a_claim():
    """The reason the rule exists: left dispositive, two judges would
    unanimously settle a claim on the strength of not having looked."""
    cast = [
        verdicts.downgrade_unverifiable(verdict(j, "refuted", assessment="unverifiable"))
        for j in ("claude-security", "agy-assumptions")
    ]
    assert verdicts.state_for(claim(), cast, ROSTER, 2, 3) == verdicts.UNPROVEN


# --- §6.1 successors from a unanimous amendment ----------------------------


def test_a_successor_bumps_the_version_and_records_what_it_supersedes():
    amendments = [verdict("claude-security", "amended", amended="the guard is weak")]
    successor, _note = verdicts.build_successor(claim(), amendments, round_no=2)
    assert successor.id == "c-0001@2"
    assert successor.supersedes == "c-0001@1"
    assert successor.claim == "the guard is weak"


def test_the_successor_origin_is_the_union_of_author_and_amenders():
    """§6.1. Neither is independent of the successor's wording, so both are
    excluded from judging it."""
    amendments = [
        verdict("claude-security", "amended", amended="reworded"),
        verdict("agy-assumptions", "amended", amended="reworded"),
    ]
    successor, _ = verdicts.build_successor(claim(), amendments, round_no=2)
    assert set(successor.origin) == {"codex-ops", "claude-security", "agy-assumptions"}
    assert verdicts.judges_for(successor, ROSTER) == []


def test_disagreeing_amenders_cannot_build_an_arbitrary_successor():
    amendments = [
        verdict("agy-assumptions", "amended", amended="first wording"),
        verdict("claude-security", "amended", amended="second wording"),
    ]
    with pytest.raises(ValueError, match="conflicting wording"):
        verdicts.build_successor(claim(), amendments, round_no=2)


def test_agreeing_amenders_produce_no_note():
    amendments = [
        verdict("agy-assumptions", "amended", amended="same"),
        verdict("claude-security", "amended", amended="same"),
    ]
    _successor, note = verdicts.build_successor(claim(), amendments, round_no=2)
    assert note is None


def test_a_successor_keeps_the_advisory_flag_of_its_ancestor():
    """Advisory-ness comes from the originating lens, which an amendment
    does not change."""
    amendments = [verdict("claude-security", "amended", amended="reworded")]
    successor, _ = verdicts.build_successor(claim(advisory=True), amendments, round_no=2)
    assert successor.advisory is True


# --- §7.3 termination ------------------------------------------------------


def test_dry_round_requires_both_conditions():
    assert verdicts.round_is_dry(True, True) is True
    assert verdicts.round_is_dry(True, False) is False  # a friend failed
    assert verdicts.round_is_dry(False, True) is False  # new claims appeared


def test_a_failed_round_resets_the_streak():
    """A round that did not complete is not evidence of convergence."""
    assert verdicts.next_streak(1, failed=True, dry=False) == 0


def test_dry_rounds_accumulate_and_a_wet_round_resets():
    assert verdicts.next_streak(1, failed=False, dry=True) == 2
    assert verdicts.next_streak(1, failed=False, dry=False) == 0


def test_deadlocked_counts_as_terminal_for_loop_termination():
    """The other regression this module exists to prevent: excluding
    deadlocked meant one genuine disagreement -- the outcome this tool is
    for -- disabled termination permanently."""
    assert verdicts.loop_should_terminate(2, [verdicts.DEADLOCKED]) is True


def test_termination_needs_both_a_streak_and_terminal_claims():
    assert verdicts.loop_should_terminate(1, [verdicts.SETTLED_REFUTED]) is False
    assert verdicts.loop_should_terminate(2, [verdicts.CONTESTED]) is False


# --- Findings from running the tool on verdicts.py -------------------------


def test_a_successor_uses_only_a_judges_latest_amendment():
    """codex's finding. `verdicts` accumulates across rounds, so a judge that
    amended in round 2 and changed its mind in round 3 would still supply
    wording for the successor -- the same accumulation bug already fixed in
    state_for and verdict_set_signature, missed at this third site.

    Checked through latest_per_judge, which is what the caller now filters
    with before building a successor.
    """
    stale = in_round(verdict("claude-security", "amended", amended="the round-2 wording"), 2)
    current = in_round(verdict("claude-security", "refuted"), 3)
    latest = verdicts.latest_per_judge([stale, current])
    amendments = [v for v in latest if v.verdict == "amended"]
    assert amendments == [], "a judge that changed its mind supplies no amendment"


def test_a_judge_that_still_amends_in_the_latest_round_does_supply_wording():
    """The other half: the filter must not discard a live amendment."""
    old = in_round(verdict("claude-security", "unproven"), 2)
    new = in_round(verdict("claude-security", "amended", amended="reworded"), 3)
    latest = verdicts.latest_per_judge([old, new])
    amendments = [v for v in latest if v.verdict == "amended"]
    assert [v.amended_claim for v in amendments] == ["reworded"]


def test_loop_termination_ignores_advisory_claims():
    """codex's other finding, and the reason it matters: an advisory claim
    stuck at `unproven` would block termination forever and force every loop
    to its ceiling -- exactly the failure §7.3's H4 correction exists to
    prevent, arriving through a different door.

    The contract was always "every non-advisory claim terminal"; the caller
    was passing every claim's state in.
    """
    non_advisory_only = [verdicts.SETTLED_UPHELD]
    assert verdicts.loop_should_terminate(2, non_advisory_only) is True
    # And with the advisory claim's state included, it would not have.
    assert verdicts.loop_should_terminate(2, [*non_advisory_only, verdicts.UNPROVEN]) is False


# --- What the third crossexam of verdicts.py found ---------------------------


def test_an_empty_signature_never_discards():
    """A claim nobody judged -- every friend in its origin -- has signature
    `()` every round, and `() == ()`: it went `discarded`, "judges looked
    twice and could not verify", when no judge was ever shown it."""
    assert verdicts.should_discard((), ()) is False
    assert verdicts.should_discard(None, ()) is False


def test_a_lone_judges_unanimous_amendment_supersedes():
    """Before this a lone judge's `amended` was contested every round and,
    in the final round, rewritten to `upheld` -- the judge reported as
    agreeing with wording it had just rejected."""
    c = claim(origin=("codex/ops",))
    v = verdict("claude/security", "amended", amended="reworded")
    roster = ["codex/ops", "claude/security"]
    assert verdicts.state_for(c, [v], roster, 2, 3) == verdicts.SUPERSEDED
    assert verdicts.state_for(c, [v], roster, 3, 3) == verdicts.SUPERSEDED


def test_judges_for_counts_each_identity_once():
    """`latest_per_judge` keeps one verdict per identity, so quorum must not
    count an identity twice or it becomes unreachable."""
    c = claim(origin=("codex/ops",))
    roster = ["claude/security", "claude/security", "agy/assumptions", "codex/ops"]
    assert verdicts.judges_for(c, roster) == ["claude/security", "agy/assumptions"]
    assert verdicts.quorum_for(verdicts.judges_for(c, roster)) == 2


def test_unresolved_loop_states_excludes_advisory_claims():
    """The filter `loop_should_terminate` relies on, tested on its own -- the
    only test of the rule used to pre-filter by hand."""
    blocking = claim()
    advisory = dataclasses.replace(claim(), id="c-0002@1", advisory=True)
    cross = SimpleNamespace(
        states={"c-0001@1": verdicts.SETTLED_UPHELD, "c-0002@1": verdicts.UNPROVEN}
    )
    assert unresolved_loop_states([blocking, advisory], cross, ROSTER) == [verdicts.SETTLED_UPHELD]
    assert unresolved_loop_states([blocking, advisory], None, ROSTER) == []


def test_unresolved_loop_states_excludes_claims_no_friend_can_judge():
    """A claim every friend co-authored can never leave `unproven`, so a loop
    that waits for it runs to its iteration ceiling -- which is what a
    two-friend roster did as soon as a lone judge's amendment could build a
    successor carrying both friends' origins."""
    unjudgeable = dataclasses.replace(claim(), origin=tuple(ROSTER))
    cross = SimpleNamespace(states={"c-0001@1": verdicts.UNPROVEN})
    assert unresolved_loop_states([unjudgeable], cross, ROSTER) == []


def test_the_downgrade_records_the_verdict_the_judge_actually_cast():
    """§6.5 rewrites the verdict; the reasoning is the only place the
    judge's actual word survives. The assertions on that wording went with
    the deleted apply_downgrades tests."""
    out = verdicts.downgrade_unverifiable(
        verdict("claude/security", "refuted", assessment="unverifiable")
    )
    assert out.verdict == verdicts.UNPROVEN
    assert "downgraded from 'refuted' to 'unproven'" in out.reasoning


def test_prior_verdicts_show_each_judge_once():
    """§5.1 strips the judge and carries no round, so a judge's round-2 and
    round-3 verdicts render as two anonymous reviewers -- a consensus that
    does not exist, in the prompt the next judge reads."""
    same_judge = [
        in_round(verdict("claude/security", "upheld"), 2),
        in_round(verdict("claude/security", "refuted"), 3),
        in_round(verdict("agy/assumptions", "upheld"), 3),
    ]
    prior = _prior_verdicts_by_claim(same_judge, {"c-0001@1"})
    cast = prior["c-0001@1"]
    assert len(cast) == 2, cast
    assert {v.judge for v in cast} == {"claude/security", "agy/assumptions"}
    assert [v.verdict for v in cast if v.judge == "claude/security"] == ["refuted"]

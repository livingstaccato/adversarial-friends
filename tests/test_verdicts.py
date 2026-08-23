"""Tests for the claim state machine (spec §7.1, §7.2, §7.3).

Weighted toward the two rules the design got wrong before, because those
are the ones a plausible-looking rewrite would reintroduce:

* the originator must not be dispositive, or `settled-refuted` becomes
  unreachable for every claim in every roster;
* `deadlocked` must count as terminal for loop termination, or a single
  genuine disagreement disables termination permanently.
"""

import pytest

from adversarial_friends import verdicts
from adversarial_friends.ledger import Claim, Verdict

ROSTER = ["codex-ops", "claude-security", "agy-assumptions"]


def claim(origin=("codex-ops",), cid="c-0001@1", advisory=False):
    return Claim(
        id=cid,
        supersedes=None,
        origin=list(origin),
        lens="ops",
        round=1,
        advisory=advisory,
        severity="high",
        claim="the guard is missing",
        location="src/auth.py:42",
        evidence="src/auth.py:38",
        failure_scenario="expired token reaches the handler",
        suggested_fix="check exp before dispatch",
    )


def verdict(judge, kind, cid="c-0001@1", amended=None, reasoning="because", assessment="confirmed"):
    return Verdict(
        claim_id=cid,
        judge=judge,
        round=2,
        verdict=kind,
        confidence="high",
        evidence_assessment=assessment,
        reasoning=reasoning,
        counter_evidence=None,
        amended_claim=amended,
    )


# --- §7.1 judges and quorum ------------------------------------------------


def test_originator_is_excluded_from_the_judges():
    assert verdicts.judges_for(claim(), ROSTER) == ["claude-security", "agy-assumptions"]


def test_every_origin_is_excluded_not_just_the_first():
    """origin is a list precisely because an amended or corroborated claim
    carries more than one friend."""
    c = claim(origin=("codex-ops", "claude-security"))
    assert verdicts.judges_for(c, ROSTER) == ["agy-assumptions"]


def test_quorum_is_two_when_enough_judges_exist():
    assert verdicts.quorum_for(["a", "b", "c"]) == 2


def test_quorum_falls_back_to_one_judge():
    assert verdicts.quorum_for(["a"]) == 1


# --- The H1 regression: settled-refuted must be reachable ------------------


def test_settled_refuted_is_reachable(caplog):
    """The whole point of finding H1. If the originator were dispositive
    with a standing `upheld`, unanimity among judges could never be refuted
    and this state would be unreachable in every roster."""
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "refuted"), verdict("agy-assumptions", "refuted")],
        ROSTER,
        round_no=2,
        max_rounds=3,
    )
    assert state == verdicts.SETTLED_REFUTED


def test_originator_verdict_is_ignored_even_if_one_is_cast():
    """Nothing stops a caller passing the originator's own verdict in. It
    must not count toward quorum or unanimity."""
    state = verdicts.state_for(
        claim(),
        [
            verdict("codex-ops", "upheld"),  # the originator -- must be ignored
            verdict("claude-security", "refuted"),
            verdict("agy-assumptions", "refuted"),
        ],
        ROSTER,
        round_no=2,
        max_rounds=3,
    )
    assert state == verdicts.SETTLED_REFUTED


def test_settled_upheld_when_judges_unanimously_uphold():
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "upheld"), verdict("agy-assumptions", "upheld")],
        ROSTER,
        round_no=2,
        max_rounds=3,
    )
    assert state == verdicts.SETTLED_UPHELD


def test_disagreement_is_contested_while_rounds_remain():
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "upheld"), verdict("agy-assumptions", "refuted")],
        ROSTER,
        round_no=2,
        max_rounds=3,
    )
    assert state == verdicts.CONTESTED


def test_disagreement_at_max_rounds_is_deadlocked():
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "upheld"), verdict("agy-assumptions", "refuted")],
        ROSTER,
        round_no=3,
        max_rounds=3,
    )
    assert state == verdicts.DEADLOCKED


def test_unanimous_amended_supersedes():
    state = verdicts.state_for(
        claim(),
        [
            verdict("claude-security", "amended", amended="the guard is weak"),
            verdict("agy-assumptions", "amended", amended="the guard is weak"),
        ],
        ROSTER,
        round_no=2,
        max_rounds=3,
    )
    assert state == verdicts.SUPERSEDED


# --- Below quorum ----------------------------------------------------------


def test_below_quorum_is_unproven():
    state = verdicts.state_for(claim(), [verdict("claude-security", "unproven")], ROSTER, 2, 3)
    assert state == verdicts.UNPROVEN


def test_non_dispositive_verdicts_never_reach_quorum():
    """Two judges both saying "I could not verify this" is information, not
    a decision -- it must not settle anything."""
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "unproven"), verdict("agy-assumptions", "out-of-scope")],
        ROSTER,
        2,
        3,
    )
    assert state == verdicts.UNPROVEN


def test_below_quorum_with_a_missing_required_friend_is_incomplete():
    """Distinct from unproven: judges declining to decide is a different
    problem from a judge never reporting at all (§7.2's M12 rule)."""
    state = verdicts.state_for(
        claim(),
        [verdict("claude-security", "upheld")],
        ROSTER,
        2,
        3,
        required_missing=True,
    )
    assert state == verdicts.INCOMPLETE


# --- The one-judge branch --------------------------------------------------


def test_single_judge_agreeing_with_the_author_settles():
    two_friend_roster = ["codex-ops", "claude-security"]
    state = verdicts.state_for(
        claim(), [verdict("claude-security", "upheld")], two_friend_roster, 2, 3
    )
    assert state == verdicts.SETTLED_UPHELD


def test_single_judge_disagreeing_cannot_outvote_the_author():
    """With one judge there is no way to distinguish a wrong author from a
    wrong judge, so disagreement is a deadlock rather than a settlement --
    which is what lets a two-friend roster genuinely deadlock."""
    two_friend_roster = ["codex-ops", "claude-security"]
    state = verdicts.state_for(
        claim(), [verdict("claude-security", "refuted")], two_friend_roster, 3, 3
    )
    assert state == verdicts.DEADLOCKED


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


# --- §7.2 late amendments --------------------------------------------------


def test_late_amendment_is_downgraded_to_upheld():
    """A successor produced in the final round has no round left to judge
    it, which would leave both versions non-terminal forever."""
    v = verdict("claude-security", "amended", amended="the guard is weak")
    out = verdicts.downgrade_late_amendment(v, round_no=3, max_rounds=3)
    assert out.verdict == "upheld"
    assert "the guard is weak" in out.reasoning  # the proposal is preserved
    assert "late amendment" in out.reasoning


def test_amendment_before_the_final_round_is_untouched():
    v = verdict("claude-security", "amended", amended="the guard is weak")
    assert verdicts.downgrade_late_amendment(v, round_no=2, max_rounds=3) == v


def test_a_non_amendment_is_never_rewritten():
    v = verdict("claude-security", "refuted")
    assert verdicts.downgrade_late_amendment(v, round_no=3, max_rounds=3) == v


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


def test_both_rules_firing_at_once_still_ends_unproven():
    """A final-round `amended` whose evidence was unverifiable triggers both
    rewrites. Whichever runs first, a judge that could not verify the
    evidence must not end up casting a dispositive vote."""
    v = verdict("claude-security", "amended", amended="reworded", assessment="unverifiable")
    assert verdicts.apply_downgrades(v, round_no=3, max_rounds=3).verdict == verdicts.UNPROVEN


def test_the_note_names_the_verdict_the_judge_actually_cast():
    """This is what the rule order buys, and the only observable difference
    between the two orders: running the evidence rule first means the
    recorded reasoning says the judge cast `amended`, not the `upheld` that
    an internal rewrite would otherwise have substituted for it first."""
    v = verdict(
        "claude-security", "amended", amended="reworded", assessment="unverifiable", reasoning=""
    )
    note = verdicts.apply_downgrades(v, round_no=3, max_rounds=3).reasoning
    assert "'amended' to 'unproven'" in note


def test_apply_downgrades_still_performs_the_late_amendment_rewrite():
    v = verdict("claude-security", "amended", amended="reworded", assessment="confirmed")
    assert verdicts.apply_downgrades(v, round_no=3, max_rounds=3).verdict == "upheld"


def test_apply_downgrades_leaves_an_ordinary_verdict_alone():
    v = verdict("claude-security", "refuted", assessment="confirmed")
    assert verdicts.apply_downgrades(v, round_no=2, max_rounds=3) == v


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


# --- Gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,blocks",
    [
        (verdicts.SETTLED_REFUTED, False),
        (verdicts.SUPERSEDED, False),
        (verdicts.DISCARDED, False),
        (verdicts.SETTLED_UPHELD, True),
        (verdicts.CONTESTED, True),
        (verdicts.DEADLOCKED, True),
        (verdicts.UNPROVEN, True),
        (verdicts.INCOMPLETE, True),
    ],
)
def test_gate_blocking_per_state(state, blocks):
    """settled-upheld blocks: the judges agreed the defect is real, which
    needs a Resolution rather than a pass."""
    assert verdicts.gate_blocked([state]) is blocks

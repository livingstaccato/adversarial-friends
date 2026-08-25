"""Tests for the rest of the claim state machine.

§7.2's discard rule and per-verdict downgrades, §6.5's evidence symmetry,
§6.1's successor claims, §7.3's loop termination, and the gate rule.
§7.1's decision table is in test_verdicts.py.

Weighted toward `deadlocked` counting as terminal for loop termination:
excluding it meant a single genuine disagreement -- precisely the outcome
this tool exists to surface -- disabled termination permanently.
"""

import pytest
from verdict_helpers import ROSTER, claim, in_round, verdict

from adversarial_friends import verdicts

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


def test_disagreeing_amenders_produce_a_note_naming_what_was_not_adopted():
    """Judges can agree on `amended` without agreeing on a rewrite, and the
    successor can only carry one. A discarded proposal is exactly the kind
    of thing this tool exists to surface, so it must not vanish."""
    amendments = [
        verdict("agy-assumptions", "amended", amended="first wording"),
        verdict("claude-security", "amended", amended="second wording"),
    ]
    successor, note = verdicts.build_successor(claim(), amendments, round_no=2)
    # Sorted judge order, so a replay of the same ledger picks the same one.
    assert successor.claim == "first wording"
    assert note is not None
    assert "second wording" in note


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

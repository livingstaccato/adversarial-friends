"""Tests for §7.1's decision table: who judges, quorum, and what settles.

Weighted toward the rule the design got wrong before, because it is the one
a plausible-looking rewrite would reintroduce: the originator must not be
dispositive, or `settled-refuted` becomes unreachable for every claim in
every roster.

The rest of the state machine -- §7.2's discard rule and downgrades, §6.1
successors, §7.3 termination -- lives in test_verdicts_lifecycle.py.
"""

from verdict_helpers import ROSTER, claim, in_round, verdict

from adversarial_friends import verdicts
from adversarial_friends.merge import canonical_claims
from test_merge import chained_alias_records


def test_originator_is_excluded_from_the_judges():
    assert verdicts.judges_for(claim(), ROSTER) == ["claude-security", "agy-assumptions"]


def test_every_origin_is_excluded_not_just_the_first():
    """origin is a list precisely because an amended or corroborated claim
    carries more than one friend."""
    c = claim(origin=("codex-ops", "claude-security"))
    assert verdicts.judges_for(c, ROSTER) == ["agy-assumptions"]


def test_reconstructed_transitive_origin_cannot_judge():
    rebuilt = canonical_claims(chained_alias_records())[0]
    roster = ["friend-a", "friend-b", "friend-c", "friend-d"]
    assert verdicts.judges_for(rebuilt, roster) == ["friend-d"]


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


# --- One judge, one vote, however many rounds ------------------------------


def test_one_judge_speaking_twice_cannot_reach_quorum_alone():
    """Found in a real ollama run, not by inspection. A friend failed its
    judging round, leaving the claim below quorum and so non-terminal; the
    next round asked the surviving judge again, and its second identical
    verdict settled the claim as though two independent judges had agreed.

    The ledger showed `ollama/ops` twice on the same claim id, and the claim
    reported `settled-refuted` on one judge's opinion."""
    twice = [
        in_round(verdict("claude-security", "refuted"), 2),
        in_round(verdict("claude-security", "refuted"), 3),
    ]
    state = verdicts.state_for(claim(), twice, ROSTER, round_no=3, max_rounds=3)
    assert state != verdicts.SETTLED_REFUTED
    assert state == verdicts.UNPROVEN


def test_a_judge_that_changed_its_mind_is_counted_once_at_its_newest():
    """Round 3 shows a judge the other side's reasoning, so it may vote
    differently. The newer verdict replaces the older rather than joining
    it -- two judges genuinely disagreeing must still register."""
    cast = [
        in_round(verdict("claude-security", "upheld"), 2),
        in_round(verdict("claude-security", "refuted"), 3),
        in_round(verdict("agy-assumptions", "refuted"), 3),
    ]
    state = verdicts.state_for(claim(), cast, ROSTER, round_no=3, max_rounds=3)
    assert state == verdicts.SETTLED_REFUTED


def test_two_distinct_judges_still_reach_quorum():
    """The guard must not break the normal case it is protecting."""
    cast = [
        in_round(verdict("claude-security", "refuted"), 2),
        in_round(verdict("agy-assumptions", "refuted"), 2),
    ]
    assert verdicts.state_for(claim(), cast, ROSTER, 2, 3) == verdicts.SETTLED_REFUTED


def test_latest_per_judge_keeps_the_newest_round():
    kept = verdicts.latest_per_judge(
        [
            in_round(verdict("claude-security", "upheld"), 2),
            in_round(verdict("claude-security", "refuted"), 3),
        ]
    )
    assert len(kept) == 1
    assert kept[0].verdict == "refuted"


# --- Nobody independent enough to judge -----------------------------------


def test_a_claim_every_friend_wrote_is_unproven_not_contested():
    """Found by smoke-testing an orchestrator merge, which unions two
    friends' origins onto the surviving claim and can leave it with no
    judges at all.

    Without this the zero-judge case falls through to the disagreement
    branch -- quorum is 0 so "below quorum" is false, and no verdicts means
    not unanimous -- and reports `contested`, asserting that judges
    disagreed when there were none."""
    everyone = claim(origin=tuple(ROSTER))
    assert verdicts.judges_for(everyone, ROSTER) == []
    assert verdicts.state_for(everyone, [], ROSTER, 2, 3) == verdicts.UNPROVEN


def test_a_claim_with_no_judges_does_not_deadlock_at_max_rounds():
    """`deadlocked` is worse than `contested` here: it is terminal, so it
    would end the run reporting a disagreement that never happened."""
    everyone = claim(origin=tuple(ROSTER))
    assert verdicts.state_for(everyone, [], ROSTER, 3, 3) == verdicts.UNPROVEN


def test_a_missing_required_friend_still_reads_as_incomplete():
    """The two reasons for "below quorum" stay distinguishable even when the
    judge set is empty."""
    everyone = claim(origin=tuple(ROSTER))
    state = verdicts.state_for(everyone, [], ROSTER, 2, 3, required_missing=True)
    assert state == verdicts.INCOMPLETE

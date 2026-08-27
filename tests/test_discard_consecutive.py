"""§7.2's discard rule fires on two CONSECUTIVE identical non-dispositive
rounds. This drives `_settle_round` through UNPROVEN -> CONTESTED -> UNPROVEN
to check that an intervening contested round resets the comparison.

Raised by codex during a self-review of verdicts.py; reachability was not
established by the claim, so this test establishes it one way or the other.
"""

import dataclasses

from verdict_helpers import claim, verdict

from adversarial_friends import verdicts as vd
from adversarial_friends.adapters import FriendSpec
from adversarial_friends.commands.crossexam import CrossexamOutcome, _settle_round


def _spec(cli, lens):
    return FriendSpec(
        name=f"{cli}-{lens}-0", cli=cli, lens=lens, model=None, effort=None, scope="doc", timeout=9
    )


SPECS = [_spec("codex", "ops"), _spec("claude", "security"), _spec("agy", "assumptions")]
JUDGES = ("claude/security", "agy/assumptions")


def _cast(outcome, round_no, kinds):
    for judge, kind in zip(JUDGES, kinds, strict=True):
        outcome.verdicts.append(
            dataclasses.replace(verdict(judge, kind, assessment="unverifiable"), round=round_no)
        )


def _settle(outcome, round_no, kinds, max_rounds=6):
    # Discard signatures live on the outcome, so a loop's next block goes on
    # comparing against the last round that actually happened.
    _cast(outcome, round_no, kinds)
    contested = [claim(origin=("codex/ops",))]
    _settle_round(outcome, contested, SPECS, None, round_no, max_rounds, {}, True)
    return outcome.states["c-0001@1"]


def test_two_consecutive_identical_unproven_rounds_discard():
    """The rule as intended: same non-dispositive set twice in a row."""
    outcome = CrossexamOutcome()
    assert _settle(outcome, 2, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN
    assert _settle(outcome, 3, ("out-of-scope", "out-of-scope")) == vd.DISCARDED


def test_a_contested_round_in_between_resets_the_comparison():
    """UNPROVEN in round 2, CONTESTED in round 3 (judges engaged and split),
    UNPROVEN again in round 4 with the same set as round 2.

    Rounds 2 and 4 are not consecutive; round 3 shows judges COULD decide
    this claim. Discarding it here closes a claim with live disagreement on
    the record as though nobody had ever been able to look.
    """
    outcome = CrossexamOutcome()
    assert _settle(outcome, 2, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN
    assert _settle(outcome, 3, ("upheld", "refuted")) == vd.CONTESTED
    assert _settle(outcome, 4, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN


def test_a_claim_nobody_can_judge_is_never_discarded():
    """Every friend in its origin: no slice contains it, so nothing is
    `missing`, its signature is `()` every round, and until `should_discard`
    learned that an empty signature is not evidence, `() == ()` made it
    `discarded` on the second round."""
    outcome = CrossexamOutcome()
    everyone = [claim(origin=("codex/ops", "claude/security", "agy/assumptions"))]
    for round_no in (2, 3, 4):
        _settle_round(outcome, everyone, SPECS, None, round_no, 6, {}, True)
        assert outcome.states["c-0001@1"] == vd.UNPROVEN, round_no

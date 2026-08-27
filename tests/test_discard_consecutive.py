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


def _settle(outcome, signatures, round_no, kinds, max_rounds=6):
    _cast(outcome, round_no, kinds)
    contested = [claim(origin=("codex/ops",))]
    _settle_round(outcome, contested, signatures, SPECS, None, round_no, max_rounds, False)
    return outcome.states["c-0001@1"]


def test_two_consecutive_identical_unproven_rounds_discard():
    """The rule as intended: same non-dispositive set twice in a row."""
    outcome, signatures = CrossexamOutcome(), {}
    assert _settle(outcome, signatures, 2, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN
    assert _settle(outcome, signatures, 3, ("out-of-scope", "out-of-scope")) == vd.DISCARDED


def test_a_contested_round_in_between_resets_the_comparison():
    """UNPROVEN in round 2, CONTESTED in round 3 (judges engaged and split),
    UNPROVEN again in round 4 with the same set as round 2.

    Rounds 2 and 4 are not consecutive; round 3 shows judges COULD decide
    this claim. Discarding it here closes a claim with live disagreement on
    the record as though nobody had ever been able to look.
    """
    outcome, signatures = CrossexamOutcome(), {}
    assert _settle(outcome, signatures, 2, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN
    assert _settle(outcome, signatures, 3, ("upheld", "refuted")) == vd.CONTESTED
    assert _settle(outcome, signatures, 4, ("out-of-scope", "out-of-scope")) == vd.UNPROVEN

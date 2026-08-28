"""A judge must never be shown its own prior verdict (§5.1).

`_prior_verdicts_by_claim` de-duplicates each judge across rounds and the
same result was handed to every judge in the round. §5.1 strips the judge's
name, so from the first round that carries prior verdicts a judge weighing
"what did the others conclude" read its OWN earlier opinion back as an
independent anonymous reviewer.

That is worse than a leak of identity. It manufactures corroboration in the
direction each judge already leans, which is exactly the consensus the blind
presentation exists to prevent. The function's docstring describes the
sibling bug it was written to fix -- two verdicts from one judge rendering as
two reviewers -- and never considered the recipient.
"""

from adversarial_friends.commands import judging
from adversarial_friends.ledger import Verdict


def _verdict(claim_id: str, judge: str, round_no: int, text: str) -> Verdict:
    return Verdict(
        claim_id=claim_id,
        judge=judge,
        round=round_no,
        verdict="upheld",
        confidence="high",
        evidence_assessment="confirmed",
        reasoning=text,
        counter_evidence=None,
        amended_claim=None,
    )


ALL = [
    _verdict("c-1", "codex/security", 2, "codex said so"),
    _verdict("c-1", "agy/ops", 2, "agy said so"),
    _verdict("c-1", "claude/assumptions", 2, "claude said so"),
]


def test_a_judge_is_not_shown_its_own_prior_verdict():
    prior = judging._prior_verdicts_by_claim(ALL, {"c-1"}, exclude_judge="agy/ops")
    judges = {v.judge for v in prior["c-1"]}
    assert "agy/ops" not in judges
    assert judges == {"codex/security", "claude/assumptions"}


def test_the_other_judges_are_still_shown():
    """Excluding the recipient must not empty the slice: the whole point of
    carrying prior verdicts is telling a judge what the OTHERS concluded."""
    prior = judging._prior_verdicts_by_claim(ALL, {"c-1"}, exclude_judge="codex/security")
    assert len(prior["c-1"]) == 2


def test_excluding_nobody_keeps_every_verdict():
    prior = judging._prior_verdicts_by_claim(ALL, {"c-1"})
    assert len(prior["c-1"]) == 3


def test_a_claim_whose_only_prior_verdict_is_the_recipients_drops_out():
    """Not an empty list left behind: a judge with nothing to be told about a
    claim should carry no prior block for it at all, rather than an empty one
    that reads as "the others said nothing"."""
    only_mine = [_verdict("c-2", "agy/ops", 2, "mine alone")]
    prior = judging._prior_verdicts_by_claim(only_mine, {"c-2"}, exclude_judge="agy/ops")
    assert "c-2" not in prior

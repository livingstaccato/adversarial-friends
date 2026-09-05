"""A re-amended claim must not mint a successor id that already exists.

`bump_claim_id` derives an id purely from (number, version + 1) and knows
nothing about the ledger. That holds while each claim is amended once. A loop
breaks it: when the artifact changes mid-run, `run_rounds` is handed
`prior=None` so claims settled against the old text are judged against the
new one -- deliberately -- while the accumulated claim list, successors
included, is kept. An already-superseded claim is therefore re-seeded
contested, can be amended again, and produces the same successor id a second
time. The ledger then holds two different claims under one id and every later
reference to it is ambiguous.

Fixed where the id is chosen rather than by asking the caller to preserve
state it discards on purpose.
"""

from afriend import verdicts as vd
from afriend.ledger import Claim, Verdict


def _claim(cid: str, text: str = "the original") -> Claim:
    return Claim(
        id=cid,
        severity="high",
        claim=text,
        location="x.py:1",
        evidence="e",
        failure_scenario="f",
        suggested_fix="s",
        lens="ops",
        origin=["codex/ops"],
        round=1,
        supersedes=None,
        advisory=False,
    )


def _amend(cid: str, judge: str, wording: str) -> Verdict:
    return Verdict(
        claim_id=cid,
        judge=judge,
        round=2,
        verdict="amended",
        confidence="high",
        evidence_assessment="confirmed",
        reasoning="needs rewording",
        counter_evidence=None,
        amended_claim=wording,
    )


AMENDMENTS = [
    _amend("c-0002@1", "agy/ops", "the rewrite"),
    _amend("c-0002@1", "claude/x", "the rewrite"),
]


def test_a_successor_id_skips_one_already_taken():
    successor, _note = vd.build_successor(
        _claim("c-0002@1"), AMENDMENTS, 3, taken={"c-0002@1", "c-0002@2"}
    )
    assert successor.id == "c-0002@3"


def test_it_skips_a_whole_run_of_taken_ids():
    successor, _note = vd.build_successor(
        _claim("c-0002@1"), AMENDMENTS, 3, taken={"c-0002@1", "c-0002@2", "c-0002@3", "c-0002@4"}
    )
    assert successor.id == "c-0002@5"


def test_the_ordinary_case_is_unchanged():
    """Nothing taken, so the successor is the plain next version -- the
    behaviour every existing test and ledger already depends on."""
    successor, _note = vd.build_successor(_claim("c-0002@1"), AMENDMENTS, 3)
    assert successor.id == "c-0002@2"
    successor, _note = vd.build_successor(_claim("c-0002@1"), AMENDMENTS, 3, taken=set())
    assert successor.id == "c-0002@2"


def test_a_collision_would_otherwise_have_happened():
    """Guards the guard: without the taken set the same id is minted twice,
    which is the defect."""
    first, _ = vd.build_successor(_claim("c-0002@1"), AMENDMENTS, 2)
    second, _ = vd.build_successor(_claim("c-0002@1"), AMENDMENTS, 3)
    assert first.id == second.id == "c-0002@2"
    # And with the ledger's contents supplied, it does not.
    third, _ = vd.build_successor(_claim("c-0002@1"), AMENDMENTS, 3, taken={first.id})
    assert third.id != first.id

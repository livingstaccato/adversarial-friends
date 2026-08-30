import dataclasses

import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import Alias, Claim, Resolution, Verdict
from adversarial_friends.merge import canonical_claims
from adversarial_friends.reviewstate import ReviewState


def claim(cid: str, *, text: str = "claim", supersedes: str | None = None) -> Claim:
    return Claim(
        id=cid,
        supersedes=supersedes,
        origin=[f"friend-{cid}"],
        lens="generated",
        round=1,
        advisory=False,
        severity="medium",
        claim=text,
        location="src/example.py:1",
        evidence="src/example.py:1",
        failure_scenario="failure",
        suggested_fix="fix",
    )


def chained_review_records():
    first = dataclasses.replace(claim("c-0001@1"), origin=["friend-a"])
    second = dataclasses.replace(claim("c-0002@1"), origin=["friend-b"])
    third = dataclasses.replace(claim("c-0003@1"), origin=["friend-c"])
    return [
        first,
        second,
        Alias("c-0001@1", "c-0002@1", 1, "exact", "same"),
        third,
        Alias("c-0003@1", "c-0001@1", 2, "orchestrator", "same"),
    ]


def test_replay_equals_incremental_apply():
    records = chained_review_records()
    replayed = ReviewState.replay(records)
    incremental = ReviewState()
    for record in records:
        incremental.apply(record)
    assert incremental == replayed


def test_alias_chain_preserves_every_origin():
    state = ReviewState.replay(chained_review_records())
    assert state.claims[0].origin == ["friend-c", "friend-a", "friend-b"]


def test_duplicate_claim_id_with_different_content_is_rejected():
    state = ReviewState()
    state.apply(claim("c-0001@1", text="first"))
    with pytest.raises(UsageError, match="duplicate claim id"):
        state.apply(claim("c-0001@1", text="different"))


def test_dangling_alias_is_recorded_as_a_compatibility_warning():
    state = ReviewState()
    duplicate = claim("c-0002@1")
    state.apply(duplicate)
    alias = Alias("c-0001@1", "c-0002@1", 1, "exact", "same")
    state.apply(alias)
    assert state.aliases == [alias]
    assert state.claims == []
    assert state.transition_warnings == [
        "alias 'c-0002@1' -> 'c-0001@1' has a missing endpoint"
    ]

    downgrades: list[str] = []
    state.copy_transition_warnings(downgrades)
    state.copy_transition_warnings(downgrades)
    assert downgrades == [
        "ledger compatibility warning: alias 'c-0002@1' -> 'c-0001@1' "
        "has a missing endpoint"
    ]


def test_successor_cycle_is_rejected_even_for_a_preloaded_invalid_graph():
    first = claim("c-0001@1", supersedes="c-0002@1")
    state = ReviewState(claims_by_id={first.id: first})
    with pytest.raises(UsageError, match="successor cycle"):
        state.apply(claim("c-0002@1", supersedes="c-0001@1"))


def test_verdict_and_resolution_for_unknown_claims_are_rejected():
    state = ReviewState()
    verdict = Verdict(
        claim_id="c-0001@1",
        judge="judge",
        round=2,
        verdict="unproven",
        confidence="medium",
        evidence_assessment="unverifiable",
        reasoning="unknown",
        counter_evidence=None,
        amended_claim=None,
    )
    resolution = Resolution(
        claim_id="c-0001@1",
        disposition="accepted-risk",
        author="operator",
        evidence="src/example.py:1",
        round=2,
        verified="unverifiable",
    )
    with pytest.raises(UsageError, match="unknown claim"):
        state.apply(verdict)
    with pytest.raises(UsageError, match="unknown claim"):
        state.apply(resolution)


def test_latest_verdicts_are_reduced_per_judge():
    item = claim("c-0001@1")
    first = Verdict(
        item.id,
        "judge",
        2,
        "unproven",
        "low",
        "unverifiable",
        "first",
        None,
        None,
    )
    second = dataclasses.replace(first, round=3, reasoning="second")
    state = ReviewState.replay([item, first, second])
    assert state.latest_verdicts_for(item.id) == [second]


def test_reducer_matches_legacy_observables():
    item = claim("c-0004@1")
    duplicate = claim("c-0005@1")
    alias = Alias(item.id, duplicate.id, 1, "exact", "same")
    verdict = Verdict(
        item.id,
        "judge",
        2,
        "upheld",
        "high",
        "verified",
        "confirmed",
        None,
        None,
    )
    resolution = Resolution(
        item.id,
        "accepted-risk",
        "operator",
        "src/example.py:1",
        2,
        "changed",
    )
    records = [item, duplicate, alias, verdict, resolution]

    state = ReviewState.replay(records)

    assert state.claims == canonical_claims(records)
    assert state.verdicts == [verdict]
    assert state.aliases == [alias]
    assert state.resolutions == [resolution]

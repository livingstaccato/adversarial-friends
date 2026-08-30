import dataclasses
import random

from adversarial_friends.ledger import Alias, Claim, Record, Resolution, Verdict
from adversarial_friends.reviewstate import ReviewState


def make_generated_claim(number: int, origin: list[str]) -> Claim:
    return Claim(
        id=f"c-{number:04d}@1",
        supersedes=None,
        origin=origin,
        lens="generated",
        round=1,
        advisory=False,
        severity="medium",
        claim=f"generated claim {number}",
        location=f"src/generated_{number}.py:1",
        evidence=f"src/generated_{number}.py:1",
        failure_scenario=f"generated failure {number}",
        suggested_fix=f"generated fix {number}",
    )


def generated_valid_records(rng: random.Random) -> list[Record]:
    count = rng.randint(1, 8)
    claims = [
        make_generated_claim(index + 1, [f"friend-{index + 1}"])
        for index in range(count)
    ]
    records: list[Record] = list(claims)
    active = [item.id for item in claims]
    while len(active) > 1 and rng.random() < 0.8:
        duplicate_index = rng.randrange(len(active) - 1)
        duplicate = active[duplicate_index]
        canonical = active[duplicate_index + 1]
        records.append(Alias(canonical, duplicate, 1, "generated", "generated alias"))
        active.pop(duplicate_index)
    by_id = {item.id: item for item in claims}
    for index, claim_id in enumerate(list(active)):
        if rng.random() < 0.4:
            successor_id = f"{claim_id.rsplit('@', 1)[0]}@2"
            successor = dataclasses.replace(
                by_id[claim_id],
                id=successor_id,
                supersedes=claim_id,
                round=2,
                claim=f"amended {by_id[claim_id].claim}",
            )
            records.append(successor)
            by_id[successor_id] = successor
            active[index] = successor_id
    for claim_id in active:
        if rng.random() < 0.7:
            records.append(
                Verdict(
                    claim_id=claim_id,
                    judge="generated-judge",
                    round=2,
                    verdict="unproven",
                    confidence="medium",
                    evidence_assessment="unverifiable",
                    reasoning="generated reasoning",
                    counter_evidence=None,
                    amended_claim=None,
                )
            )
        if rng.random() < 0.4:
            records.append(
                Resolution(
                    claim_id=claim_id,
                    disposition="accepted-risk",
                    author="generated-operator",
                    evidence="src/generated.py:1",
                    round=2,
                    verified="unverifiable",
                )
            )
    return records


def test_generated_valid_sequences_replay_every_prefix():
    rng = random.Random(0)
    for _case in range(200):
        records = generated_valid_records(rng)
        incremental = ReviewState()
        prefix: list[Record] = []
        for record in records:
            prefix.append(record)
            incremental.apply(record)
            assert incremental == ReviewState.replay(prefix)

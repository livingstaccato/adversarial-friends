"""Shared fixtures for the claim state machine tests.

Split out when test_verdicts.py crossed this repo's 500-line-per-test-file
cap; the two halves both need the same claim/verdict builders.
"""

import dataclasses

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


def in_round(v, round_no):
    """The same verdict, cast in a different round."""
    return dataclasses.replace(v, round=round_no)

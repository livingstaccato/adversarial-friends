import json

import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import (
    Alias,
    Claim,
    Ledger,
    Resolution,
    Verdict,
    record_from_dict,
    record_to_dict,
)


def make_claim(**over):
    base = dict(
        id="c-0001@1",
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="high",
        claim="the guard is missing",
        location="src/auth.py:42",
        evidence="src/auth.py:38",
        failure_scenario="expired token reaches the handler",
        suggested_fix="check exp before dispatch",
    )
    base.update(over)
    return Claim(**base)


def test_claim_roundtrips_through_dict():
    claim = make_claim()
    assert record_from_dict(record_to_dict(claim)) == claim


def test_record_to_dict_tags_the_type():
    assert record_to_dict(make_claim())["type"] == "claim"


def test_ledger_appends_and_reads_back_in_order(tmp_path):
    ledger = Ledger(tmp_path / "claims.jsonl")
    claim = make_claim()
    verdict = Verdict(
        claim_id="c-0001@1",
        judge="claude/security",
        round=2,
        verdict="refuted",
        confidence="high",
        evidence_assessment="disputed",
        reasoning="line 38 already guards it",
        counter_evidence="src/auth.py:38",
        amended_claim=None,
    )
    ledger.append(claim)
    ledger.append(verdict)
    assert list(ledger.records()) == [claim, verdict]
    assert ledger.claims() == [claim]
    assert ledger.verdicts_for("c-0001@1") == [verdict]


def test_verdicts_for_is_version_exact(tmp_path):
    """A verdict on a superseded version must not leak into the successor's tally."""
    ledger = Ledger(tmp_path / "claims.jsonl")
    ledger.append(
        Verdict(
            claim_id="c-0001@1",
            judge="codex/ops",
            round=2,
            verdict="upheld",
            confidence="high",
            evidence_assessment="confirmed",
            reasoning="stands",
            counter_evidence=None,
            amended_claim=None,
        )
    )
    assert len(ledger.verdicts_for("c-0001@1")) == 1
    assert ledger.verdicts_for("c-0001@2") == []


def test_aliases_are_readable(tmp_path):
    ledger = Ledger(tmp_path / "claims.jsonl")
    alias = Alias(
        canonical="c-0001@1",
        duplicate="c-0004@1",
        round=1,
        source="exact",
        rationale="identical claim text and location",
    )
    ledger.append(alias)
    assert ledger.aliases() == [alias]


def test_resolution_roundtrips(tmp_path):
    resolution = Resolution(
        claim_id="c-0001@1",
        disposition="fixed",
        author="tim",
        evidence="src/auth.py:38",
        round=3,
        verified="location-changed",
    )
    assert record_from_dict(record_to_dict(resolution)) == resolution


def test_unknown_record_type_is_rejected():
    with pytest.raises(UsageError):
        record_from_dict({"type": "nonsense"})


def test_amended_claim_roundtrips_with_multiple_origins():
    """origin is a list precisely so an amendment can carry author + amender."""
    claim = make_claim(
        id="c-0007@2", supersedes="c-0007@1", origin=["codex/ops", "claude/security"]
    )
    restored = record_from_dict(record_to_dict(claim))
    assert restored == claim
    assert restored.origin == ["codex/ops", "claude/security"]
    assert restored.supersedes == "c-0007@1"


def test_malformed_line_is_surfaced_not_skipped(tmp_path):
    path = tmp_path / "claims.jsonl"
    ledger = Ledger(path)
    ledger.append(make_claim())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")
    with pytest.raises(json.JSONDecodeError):
        list(ledger.records())

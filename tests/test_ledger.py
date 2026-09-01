import json
import os

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


def test_record_schema_rejects_extra_keys():
    payload = record_to_dict(make_claim())
    payload["future_surprise"] = "must not be ignored"
    with pytest.raises(UsageError, match="unexpected keys"):
        record_from_dict(payload)


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
    with pytest.raises(UsageError, match=r"claims\.jsonl:2: malformed JSON"):
        list(ledger.records())


def test_append_synchronizes_the_record(monkeypatch, tmp_path):
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    Ledger(tmp_path / "claims.jsonl").append(make_claim())
    assert len(synced) >= 1


def test_fsync_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    def fail(_fd):
        raise OSError("disk refused sync")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk refused sync"):
        Ledger(tmp_path / "claims.jsonl").append(make_claim())


def test_append_retries_a_short_write(monkeypatch, tmp_path):
    real_write = os.write
    calls = []

    def short_write(fd, data):
        calls.append(len(data))
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "write", short_write)
    expected = make_claim()
    ledger = Ledger(tmp_path / "claims.jsonl")
    ledger.append(expected)
    assert len(calls) > 1
    assert list(ledger.records()) == [expected]


def test_corrupt_middle_record_names_its_line(tmp_path):
    path = tmp_path / "claims.jsonl"
    valid = json.dumps(record_to_dict(make_claim()))
    path.write_text(f"{valid}\n{{broken\n{valid}\n")
    with pytest.raises(UsageError, match=r"claims\.jsonl:2: malformed JSON"):
        list(Ledger(path).records())


def test_malformed_record_names_its_line(tmp_path):
    path = tmp_path / "claims.jsonl"
    path.write_text('{"type": "claim", "id": "c-0001@1"}\n')
    with pytest.raises(UsageError, match=r"claims\.jsonl:1: malformed 'claim' record"):
        list(Ledger(path).records())


def test_ledger_never_follows_a_symlink_for_read_or_append(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    link = tmp_path / "claims.jsonl"
    link.symlink_to(outside)
    ledger = Ledger(link)

    with pytest.raises(OSError):
        ledger.append(make_claim())
    with pytest.raises(UsageError, match="cannot read ledger"):
        list(ledger.records())

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_ledger_refuses_an_oversized_line_before_json_decode(tmp_path):
    path = tmp_path / "claims.jsonl"
    path.write_bytes(b'"' + b"x" * (8 * 1024 * 1024) + b'"\n')
    with pytest.raises(UsageError, match="line 1 exceeds"):
        list(Ledger(path).records())


def test_ledger_refuses_an_oversized_sparse_file(tmp_path):
    path = tmp_path / "claims.jsonl"
    with path.open("wb") as handle:
        handle.truncate(129 * 1024 * 1024)
    with pytest.raises(UsageError, match="file exceeds"):
        list(Ledger(path).records())


def test_ledger_applies_shared_string_and_depth_bounds(tmp_path):
    path = tmp_path / "claims.jsonl"
    huge = record_to_dict(make_claim(claim="x" * (4 * 1024 * 1024 + 1)))
    path.write_text(json.dumps(huge) + "\n", encoding="utf-8")
    with pytest.raises(UsageError, match="JSON bounds"):
        list(Ledger(path).records())

    nested: object = "leaf"
    for _ in range(70):
        nested = [nested]
    payload = record_to_dict(make_claim())
    payload["nested"] = nested
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(UsageError, match="JSON bounds"):
        list(Ledger(path).records())

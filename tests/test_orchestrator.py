"""Tests for the halt/resume handshake (spec §4.2).

Weighted toward validating the response, because that file arrives from
another process and its ids become permanent ledger records. Every rejection
here corresponds to a way the alias graph could be corrupted such that nobody
notices until they are reading a report and wondering where a finding went.
"""

import json

import pytest

from adversarial_friends import orchestrator
from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import Claim


def claim(cid, text="the guard is missing", origin=("codex/ops",)):
    return Claim(
        id=cid,
        supersedes=None,
        origin=list(origin),
        lens="ops",
        round=1,
        advisory=False,
        severity="high",
        claim=text,
        location="src/auth.py:42",
        evidence="src/auth.py:38",
        failure_scenario="expired token reaches the handler",
        suggested_fix="check exp before dispatch",
    )


def write_response(tmp_path, merges, version=orchestrator.SCHEMA_VERSION):
    payload = {"version": version, "merges": merges}
    orchestrator.response_path(tmp_path).write_text(json.dumps(payload))
    return tmp_path


# --- The request -----------------------------------------------------------


def test_the_request_carries_the_fields_dedup_needs(tmp_path):
    orchestrator.write_request(tmp_path, "run-1", 1, [claim("c-0001@1")])
    data = json.loads(orchestrator.request_path(tmp_path).read_text())
    entry = data["claims"][0]
    assert set(entry) == {"id", "severity", "claim", "location", "evidence"}


def test_the_request_omits_origin(tmp_path):
    """Knowing two friends raised a claim says nothing about whether two
    texts mean the same thing, and including it invites merging by author
    rather than by content."""
    orchestrator.write_request(tmp_path, "run-1", 1, [claim("c-0001@1")])
    assert "codex/ops" not in orchestrator.request_path(tmp_path).read_text()


def test_the_request_is_a_fillable_template(tmp_path):
    """It ships an empty `merges` array so the orchestrator edits one file
    rather than composing a second from scratch."""
    orchestrator.write_request(tmp_path, "run-1", 1, [claim("c-0001@1")])
    data = json.loads(orchestrator.request_path(tmp_path).read_text())
    assert data["merges"] == []
    assert data["question"] == orchestrator.QUESTION_MERGE
    assert data["instructions"]


# --- Reading the response --------------------------------------------------


def test_a_missing_response_says_what_to_do(tmp_path):
    with pytest.raises(UsageError, match="--resume"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


def test_malformed_json_is_a_usage_error(tmp_path):
    orchestrator.response_path(tmp_path).write_text("{not json")
    with pytest.raises(UsageError, match="not valid JSON"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


def test_an_empty_merge_list_is_valid(tmp_path):
    """ "I looked and none of these are duplicates" is a real answer."""
    write_response(tmp_path, [])
    assert orchestrator.read_response(tmp_path, {"c-0001@1"}) == []


def test_a_well_formed_merge_is_accepted(tmp_path):
    write_response(
        tmp_path, [{"canonical": "c-0001@1", "duplicate": "c-0002@1", "rationale": "same defect"}]
    )
    decisions = orchestrator.read_response(tmp_path, {"c-0001@1", "c-0002@1"})
    assert decisions == [orchestrator.MergeDecision("c-0001@1", "c-0002@1", "same defect")]


def test_an_unknown_id_is_rejected(tmp_path):
    """It would produce an Alias pointing at nothing."""
    write_response(tmp_path, [{"canonical": "c-0001@1", "duplicate": "c-9999@1"}])
    with pytest.raises(UsageError, match="not a claim in this run"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


def test_merging_a_claim_into_itself_is_rejected(tmp_path):
    write_response(tmp_path, [{"canonical": "c-0001@1", "duplicate": "c-0001@1"}])
    with pytest.raises(UsageError, match="into itself"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


def test_a_chain_is_rejected_rather_than_resolved(tmp_path):
    """A->B and B->C leaves A pointing at a claim that is itself gone.
    Resolving it silently would pick a canonical the orchestrator never
    chose."""
    write_response(
        tmp_path,
        [
            {"canonical": "c-0002@1", "duplicate": "c-0001@1"},
            {"canonical": "c-0003@1", "duplicate": "c-0002@1"},
        ],
    )
    with pytest.raises(UsageError, match="chain"):
        orchestrator.read_response(tmp_path, {"c-0001@1", "c-0002@1", "c-0003@1"})


def test_the_same_duplicate_twice_is_rejected(tmp_path):
    """It would record two different fates for one claim."""
    write_response(
        tmp_path,
        [
            {"canonical": "c-0001@1", "duplicate": "c-0003@1"},
            {"canonical": "c-0002@1", "duplicate": "c-0003@1"},
        ],
    )
    with pytest.raises(UsageError, match="twice"):
        orchestrator.read_response(tmp_path, {"c-0001@1", "c-0002@1", "c-0003@1"})


def test_two_claims_merging_into_one_canonical_is_fine(tmp_path):
    """The legitimate fan-in: three friends, three wordings, one defect."""
    write_response(
        tmp_path,
        [
            {"canonical": "c-0001@1", "duplicate": "c-0002@1"},
            {"canonical": "c-0001@1", "duplicate": "c-0003@1"},
        ],
    )
    decisions = orchestrator.read_response(tmp_path, {"c-0001@1", "c-0002@1", "c-0003@1"})
    assert len(decisions) == 2


def test_a_future_schema_version_is_refused(tmp_path):
    write_response(tmp_path, [], version=99)
    with pytest.raises(UsageError, match="unsupported version"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


def test_a_non_array_merges_field_is_refused(tmp_path):
    orchestrator.response_path(tmp_path).write_text(
        json.dumps({"version": orchestrator.SCHEMA_VERSION, "merges": "none"})
    )
    with pytest.raises(UsageError, match="must be an array"):
        orchestrator.read_response(tmp_path, {"c-0001@1"})


# --- Applying the decisions ------------------------------------------------


def test_the_duplicate_is_removed_from_the_claim_list():
    claims = [claim("c-0001@1"), claim("c-0002@1", text="the guard is absent")]
    kept, _aliases = orchestrator.apply_merges(
        claims, [orchestrator.MergeDecision("c-0001@1", "c-0002@1", "same")], round_no=1
    )
    assert [c.id for c in kept] == ["c-0001@1"]


def test_corroboration_survives_an_adjudicated_merge():
    """The whole reason this path exists. These are merges of DIFFERENTLY
    worded claims -- exactly where independent agreement is the strongest
    evidence, and exactly where losing it would be worst."""
    claims = [
        claim("c-0001@1", origin=("codex/ops",)),
        claim("c-0002@1", text="the guard is absent", origin=("claude/security",)),
    ]
    kept, _ = orchestrator.apply_merges(
        claims, [orchestrator.MergeDecision("c-0001@1", "c-0002@1", "same")], round_no=1
    )
    assert kept[0].origin == ["codex/ops", "claude/security"]


def test_the_alias_records_who_decided():
    """`source` distinguishes an adjudicated merge from an exact one in the
    ledger, so a reader can tell which merges were judgment calls."""
    claims = [claim("c-0001@1"), claim("c-0002@1", text="other")]
    _kept, aliases = orchestrator.apply_merges(
        claims, [orchestrator.MergeDecision("c-0001@1", "c-0002@1", "same defect")], round_no=2
    )
    assert aliases[0].source == "orchestrator"
    assert aliases[0].rationale == "same defect"
    assert aliases[0].round == 2


def test_a_missing_rationale_still_records_something():
    claims = [claim("c-0001@1"), claim("c-0002@1", text="other")]
    _kept, aliases = orchestrator.apply_merges(
        claims, [orchestrator.MergeDecision("c-0001@1", "c-0002@1", "")], round_no=1
    )
    assert aliases[0].rationale


def test_applying_nothing_changes_nothing():
    claims = [claim("c-0001@1"), claim("c-0002@1", text="other")]
    kept, aliases = orchestrator.apply_merges(claims, [], round_no=1)
    assert kept == claims
    assert aliases == []

import argparse
import dataclasses
import json
import math

import pytest

from adversarial_friends.authority import DENY_ALL
from adversarial_friends.commands.runmeta import _base_meta, _checkpoint_themes
from adversarial_friends.errors import UsageError
from adversarial_friends.ledger import Claim
from adversarial_friends.outcomes import MAX_JSON_NODES
from adversarial_friends.snapshots import SnapshotIdentity
import adversarial_friends.themes as themes_module
from adversarial_friends.themes import (
    THEME_THRESHOLD,
    ThemeProposal,
    classify_novel,
    compare_theme,
    normalized_anchor,
    similarity,
)


def claim(
    cid: str,
    *,
    location: str | None = "src/auth.py:42",
    claim_text: str = "expiry guard is missing",
    failure_scenario: str = "expired token passes",
) -> Claim:
    return Claim(
        id=cid,
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="high",
        claim=claim_text,
        location=location,
        evidence="src/auth.py:38",
        failure_scenario=failure_scenario,
        suggested_fix="check expiration before dispatch",
    )


def _expanded_json_nodes(value):
    if type(value) is dict:
        return 1 + sum(_expanded_json_nodes(item) for item in value.values())
    if type(value) in {list, tuple}:
        return 1 + sum(_expanded_json_nodes(item) for item in value)
    return 1


def test_obvious_wording_variant_at_same_anchor_is_same_theme():
    first = claim("c-0001@1", location="src/auth.py:42", claim_text="expiry guard is missing")
    second = claim("c-0002@1", location="src/auth.py:42", claim_text="missing expiration guard")
    proposal = compare_theme(first, second)
    assert proposal is not None and proposal.score >= THEME_THRESHOLD


def test_different_failure_mechanisms_at_same_anchor_remain_novel():
    auth = claim(
        "c-0001@1",
        location="src/auth.py:42",
        claim_text="expiry guard missing",
        failure_scenario="expired token passes",
    )
    race = claim(
        "c-0002@1",
        location="src/auth.py:42",
        claim_text="refresh update unsafe",
        failure_scenario="concurrent refresh loses update",
    )
    assert compare_theme(auth, race) is None


def test_claims_without_shared_anchor_fall_back_to_exact_identity():
    first = claim(
        "c-0001@1",
        location=None,
        claim_text="expiry guard missing",
        failure_scenario="expired token passes",
    )
    second = claim(
        "c-0002@1",
        location=None,
        claim_text="missing expiration guard",
        failure_scenario="expired tokens are accepted",
    )
    assert compare_theme(first, second) is None
    novel, proposals = classify_novel([first], [second])
    assert novel == {second.id}
    assert proposals == []


def test_exact_identity_is_the_only_fallback_without_an_anchor():
    first = claim("c-0001@1", location=None, claim_text="expiry guard missing")
    duplicate = claim("c-0002@1", location="", claim_text="  EXPIRY guard   missing ")

    novel, proposals = classify_novel([first], [duplicate])

    assert novel == set()
    assert proposals == []


def test_anchor_normalization_preserves_punctuation_and_line_identity():
    assert normalized_anchor("  SRC/Auth.py:42\t") == "src/auth.py:42"
    assert normalized_anchor("src/auth.py:42") != normalized_anchor("src/auth.py:43")
    assert normalized_anchor("  \t\n") is None


def test_similarity_normalizes_only_pinned_synonyms_case_and_punctuation():
    assert similarity("Expiry guard: ABSENT!", "missing expiration guard") == 1.0
    assert similarity("expired token passes", "expiration token passes") < 1.0


def test_classify_novel_prefers_existing_then_first_incoming_canonical():
    existing = claim("c-0001@1")
    first_novel = claim(
        "c-0002@1",
        location="src/cache.py:7",
        claim_text="expiry guard is missing",
    )
    same_batch_duplicate = claim(
        "c-0003@1",
        location=" SRC/cache.py:7 ",
        claim_text="missing expiration guard",
    )
    existing_duplicate = claim("c-0004@1", claim_text="missing expiration guard")

    novel, proposals = classify_novel(
        [existing], [first_novel, same_batch_duplicate, existing_duplicate]
    )

    assert novel == {"c-0002@1"}
    assert [(p.canonical, p.duplicate) for p in proposals] == [
        ("c-0002@1", "c-0003@1"),
        ("c-0001@1", "c-0004@1"),
    ]


def test_classification_bounds_same_anchor_comparisons_and_overflow_is_novel(monkeypatch):
    comparison_limit = 512
    existing = [claim(f"c-{index + 1:04d}@1") for index in range(comparison_limit + 8)]
    candidate = claim("c-9999@1", claim_text="a distinct candidate")
    comparisons = 0

    def no_match(_canonical, _candidate):
        nonlocal comparisons
        comparisons += 1
        return None

    monkeypatch.setattr(themes_module, "compare_theme", no_match)

    novel, proposals = classify_novel(existing, [candidate])

    assert comparisons == comparison_limit
    assert novel == {candidate.id}
    assert proposals == []


def test_classification_indexes_distinct_anchors_without_fuzzy_comparison(monkeypatch):
    existing = [
        claim(f"c-{index + 1:04d}@1", location=f"src/file_{index}.py:1") for index in range(600)
    ]
    candidate = claim("c-9999@1", location="src/new.py:1")
    comparisons = 0
    real_compare = themes_module.compare_theme

    def counted_compare(canonical, incoming):
        nonlocal comparisons
        comparisons += 1
        return real_compare(canonical, incoming)

    monkeypatch.setattr(themes_module, "compare_theme", counted_compare)

    novel, proposals = classify_novel(existing, [candidate])

    assert comparisons == 0
    assert novel == {candidate.id}
    assert proposals == []


def test_oversized_theme_text_is_conservatively_not_fuzzy_matched():
    long_failure = "expired token passes repeatedly " * 100
    first = claim("c-0001@1", failure_scenario=long_failure)
    second = claim("c-0002@1", failure_scenario=long_failure)

    assert compare_theme(first, second) is None


def test_theme_proposal_is_frozen_finite_and_json_safe():
    proposal = ThemeProposal("c-0001@1", "c-0002@1", 0.9876, "src/auth.py:42")
    assert json.loads(json.dumps(dataclasses.asdict(proposal))) == {
        "canonical": "c-0001@1",
        "duplicate": "c-0002@1",
        "score": 0.9876,
        "anchor": "src/auth.py:42",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.score = 0.5
    with pytest.raises(ValueError, match="finite"):
        ThemeProposal("c-0001@1", "c-0002@1", math.nan, "src/auth.py:42")


def test_hostile_restored_claim_types_raise_contextual_usage_error():
    hostile = claim("c-0002@1")
    object.__setattr__(hostile, "failure_scenario", ["not", "text"])

    with pytest.raises(UsageError, match=r"candidate claim.*failure_scenario"):
        compare_theme(claim("c-0001@1"), hostile)


def test_restored_theme_proposals_obey_expanded_json_node_bound():
    # 1,638 proposals consume exactly 8,192 nodes only when incorrectly
    # counted apart from their enclosing metadata. The root object and
    # produced_new_themes value push the actual checkpoint over the bound.
    count = (MAX_JSON_NODES - 1) // 5
    proposals = [
        {
            "canonical": f"c-{index * 2 + 1:04d}@1",
            "duplicate": f"c-{index * 2 + 2:04d}@1",
            "score": 1.0,
            "anchor": f"src/a{index}.py:1",
        }
        for index in range(count)
    ]

    with pytest.raises(UsageError, match="metadata bound"):
        _checkpoint_themes({"theme_proposals": proposals, "produced_new_themes": False})


def test_fresh_metadata_truncates_oversized_self_produced_theme_proposals(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("spec", encoding="utf-8")
    digest = "sha256:" + "1" * 64
    snapshot = SnapshotIdentity(None, None, None, artifact.name, digest)
    proposals = [
        ThemeProposal(
            f"c-{index * 2 + 1:04d}@1",
            f"c-{index * 2 + 2:04d}@1",
            1.0,
            f"src/a{index}.py:1",
        )
        for index in range(1_700)
    ]
    source_ids = {id(proposal) for proposal in proposals}
    source_conversions = 0
    real_to_dict = ThemeProposal.to_dict

    def counted_to_dict(proposal):
        nonlocal source_conversions
        if id(proposal) in source_ids:
            source_conversions += 1
        return real_to_dict(proposal)

    monkeypatch.setattr(ThemeProposal, "to_dict", counted_to_dict)

    meta = _base_meta(
        argparse.Namespace(mode="loop", merge="exact", friend=[]),
        artifact,
        digest,
        [],
        [],
        [],
        snapshot,
        [snapshot],
        DENY_ALL,
        theme_proposals=proposals,
    )

    assert _expanded_json_nodes(meta) <= MAX_JSON_NODES
    assert 0 < len(meta["theme_proposals"]) < len(proposals)
    assert meta["theme_proposals"][0]["canonical"] == proposals[0].canonical
    assert source_conversions < len(proposals)

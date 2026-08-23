"""Tests for the judge output contract (spec §6.2, §6.5).

Weighted toward the conditional requirements, because those are the ones a
judge can satisfy the *shape* of while saying nothing useful: a refutation
with no counter-evidence, an amendment with no successor wording, and a
dispositive verdict that never says whether the evidence was checked.
"""

import pytest

from adversarial_friends import verdictschema


def entry(**overrides):
    base = {
        "claim_id": "c-0001@1",
        "verdict": "upheld",
        "confidence": "high",
        "evidence_assessment": "confirmed",
        "reasoning": "the guard really is absent at that line",
        "counter_evidence": None,
        "amended_claim": None,
    }
    base.update(overrides)
    return base


def payload(*entries):
    return {"verdicts": list(entries)}


def test_a_well_formed_verdict_validates():
    assert verdictschema.validate_payload(payload(entry())) == []


def test_missing_verdicts_array_is_rejected():
    assert verdictschema.validate_payload({}) == ["payload has no 'verdicts' array"]


@pytest.mark.parametrize("field", ["claim_id", "verdict", "confidence", "reasoning"])
def test_each_required_field_is_required(field):
    errors = verdictschema.validate_payload(payload(entry(**{field: ""})))
    assert any(field in e and "missing or empty" in e for e in errors)


def test_a_mangled_claim_id_is_rejected():
    """A judge that invents an id has judged nothing this run can attribute;
    downstream it would look like a claim that simply drew no verdicts."""
    errors = verdictschema.validate_payload(payload(entry(claim_id="claim 7")))
    assert any("not a claim id" in e for e in errors)


def test_an_unknown_verdict_word_is_rejected():
    errors = verdictschema.validate_payload(payload(entry(verdict="probably-fine")))
    assert any("not in" in e and "probably-fine" in e for e in errors)


# --- §6.5 evidence symmetry ------------------------------------------------


@pytest.mark.parametrize("kind", sorted(verdictschema.DISPOSITIVE))
def test_a_dispositive_verdict_must_say_whether_the_evidence_checked_out(kind):
    extra = {"amended_claim": "reworded"} if kind == "amended" else {}
    errors = verdictschema.validate_payload(
        payload(entry(verdict=kind, evidence_assessment=None, **extra))
    )
    assert any("evidence_assessment is required" in e for e in errors)


def test_a_non_dispositive_verdict_needs_no_assessment():
    """Declaring a claim out of scope is not a statement about the evidence."""
    assert (
        verdictschema.validate_payload(
            payload(entry(verdict="out-of-scope", evidence_assessment=None))
        )
        == []
    )


def test_disputed_requires_counter_evidence():
    """ "The cited evidence does not support the claim" is a factual assertion
    about a location. Without naming what is actually there, the report has
    nothing to quote when it prints both sides of a deadlock."""
    errors = verdictschema.validate_payload(
        payload(entry(verdict="refuted", evidence_assessment="disputed", counter_evidence=None))
    )
    assert any("counter_evidence is required" in e for e in errors)


def test_disputed_with_counter_evidence_validates():
    assert (
        verdictschema.validate_payload(
            payload(
                entry(
                    verdict="refuted",
                    evidence_assessment="disputed",
                    counter_evidence="src/auth.py:38 already guards this",
                )
            )
        )
        == []
    )


def test_unverifiable_is_accepted_by_the_schema():
    """The consequence of `unverifiable` is a downgrade applied by the state
    machine (verdicts.downgrade_unverifiable), not a rejection here -- the
    judge must be able to say it honestly."""
    assert (
        verdictschema.validate_payload(
            payload(entry(verdict="refuted", evidence_assessment="unverifiable"))
        )
        == []
    )


# --- §6.2 amendments -------------------------------------------------------


def test_amended_requires_the_successor_wording():
    errors = verdictschema.validate_payload(payload(entry(verdict="amended", amended_claim=None)))
    assert any("amended_claim is required" in e for e in errors)


def test_amended_with_wording_validates():
    assert (
        verdictschema.validate_payload(
            payload(entry(verdict="amended", amended_claim="the guard is weak, not missing"))
        )
        == []
    )


# --- Quorum integrity ------------------------------------------------------


def test_one_judge_cannot_vote_twice_on_one_claim():
    """Both copies would count toward unanimity, letting a single judge
    manufacture a quorum by repeating itself."""
    errors = verdictschema.validate_payload(payload(entry(), entry()))
    assert any("duplicate verdict" in e for e in errors)


def test_verdicts_on_distinct_claims_are_fine():
    assert verdictschema.validate_payload(payload(entry(), entry(claim_id="c-0002@1"))) == []


# --- Success and tiering ---------------------------------------------------


def test_an_empty_verdicts_array_is_not_success():
    """Unlike a critique round there is no honest empty result: a judge is
    only dispatched when it has at least one claim to judge."""
    assert verdictschema.is_successful_payload({"verdicts": []}) is False


def test_a_real_verdict_is_success():
    assert verdictschema.is_successful_payload(payload(entry())) is True


def test_a_substantive_but_broken_payload_outranks_a_well_formed_scrap():
    """Same asymmetry as claimschema.claim_tier: a false failure costs a
    re-run, a false success silently drops a judge out of the tally."""
    broken = verdictschema.verdict_tier({"verdicts": [{"claim_id": "c-0001@1"}]}, ["some error"])
    scrap = verdictschema.verdict_tier({"unrelated": True}, [])
    assert broken < scrap


def test_a_clean_payload_with_verdicts_is_tier_zero():
    assert verdictschema.verdict_tier(payload(entry()), []) == 0

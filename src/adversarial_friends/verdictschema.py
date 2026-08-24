"""The contract a judge's output must satisfy -- spec §6.2 and §6.5.

The claim-side twin of claimschema. A judge is handed a slice of claims and
must return one verdict per claim, naming the exact claim id it judged.

Two conditional requirements carry real weight and are enforced here rather
than left to the prompt:

**A `disputed` assessment requires `counter_evidence`** (§6.5). "I looked at
the cited evidence and it does not support the claim" is a factual assertion
about a specific location; without naming what is actually there it is
indistinguishable from disagreement, and the report has nothing to quote when
it prints both sides of a deadlock verbatim.

**An `amended` verdict requires `amended_claim`** (§6.2). Amending creates a
successor claim (`c-0007@2`); a successor with no wording cannot be judged in
the next round, and would leave both versions non-terminal forever.

`evidence_assessment` is required only for the dispositive verdicts, which is
exactly what §6.5 says carries it: a judge declaring a claim out-of-scope is
not making a claim about the evidence at all.
"""

import json
from pathlib import Path
from typing import Any

from .contracts import PayloadContract
from .ids import CLAIM_ID_RE
from .verdicts import DISPOSITIVE, VALID_VERDICTS

CONFIDENCES = ("high", "medium", "low")

# §6.5. `unverifiable` is not a third opinion -- it downgrades whatever
# dispositive verdict it accompanies to `unproven` (see
# verdicts.downgrade_unverifiable). It is accepted here so the judge can say
# it honestly; the consequence is applied by the state machine, not by
# rejecting the output.
EVIDENCE_ASSESSMENTS = ("confirmed", "disputed", "unverifiable")

REQUIRED_FIELDS = ("claim_id", "verdict", "confidence", "reasoning")

# See claimschema's note on strict mode: every object needs
# `additionalProperties: false` and a `required` naming every property, or
# codex rejects the schema at the API before the model sees anything.
VERDICT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    *REQUIRED_FIELDS,
                    "evidence_assessment",
                    "counter_evidence",
                    "amended_claim",
                ],
                "properties": {
                    "claim_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": sorted(VALID_VERDICTS)},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    # Nullable, but `null` is deliberately NOT in the enum.
                    # A schema-enforcing CLI rejects an enum containing null
                    # outright: agy returns "Agent execution terminated due
                    # to error" and produces nothing, which made every
                    # judging round fail for it. Verified by bisecting three
                    # schema variants against the real CLI -- a nullable
                    # type is accepted, an enum with null is not.
                    "evidence_assessment": {
                        "type": ["string", "null"],
                        "enum": list(EVIDENCE_ASSESSMENTS),
                    },
                    "reasoning": {"type": "string"},
                    "counter_evidence": {"type": ["string", "null"]},
                    "amended_claim": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def schema_path(directory: Path) -> Path:
    """Materialize the schema so adapters with a native schema flag can use it."""
    path = Path(directory) / "verdict-output.schema.json"
    path.write_text(json.dumps(VERDICT_OUTPUT_SCHEMA, indent=2), encoding="utf-8")
    return path


def _validate_one(index: int, entry: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return [f"verdicts[{index}] is not an object"]

    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"verdicts[{index}].{field} missing or empty")

    claim_id = entry.get("claim_id")
    if isinstance(claim_id, str) and claim_id.strip() and CLAIM_ID_RE.fullmatch(claim_id) is None:
        # A judge that invents or mangles an id has judged nothing this run
        # can attribute. Caught here rather than downstream, where it would
        # look like a claim that simply drew no verdicts.
        errors.append(f"verdicts[{index}].claim_id {claim_id!r} is not a claim id (e.g. c-0007@1)")

    verdict = entry.get("verdict")
    if isinstance(verdict, str) and verdict not in VALID_VERDICTS:
        errors.append(f"verdicts[{index}].verdict {verdict!r} not in {sorted(VALID_VERDICTS)}")

    confidence = entry.get("confidence")
    if isinstance(confidence, str) and confidence not in CONFIDENCES:
        errors.append(f"verdicts[{index}].confidence {confidence!r} not in {CONFIDENCES}")

    assessment = entry.get("evidence_assessment")
    if assessment is not None and (
        not isinstance(assessment, str) or assessment not in EVIDENCE_ASSESSMENTS
    ):
        errors.append(
            f"verdicts[{index}].evidence_assessment {assessment!r} not in {EVIDENCE_ASSESSMENTS}"
        )
    if verdict in DISPOSITIVE and not isinstance(assessment, str):
        # §6.5: every dispositive verdict carries one. Without it there is no
        # way to tell a judge that checked the evidence from one that did not.
        errors.append(
            f"verdicts[{index}].evidence_assessment is required for a "
            f"{verdict!r} verdict (one of {EVIDENCE_ASSESSMENTS})"
        )

    counter = entry.get("counter_evidence")
    if assessment == "disputed" and not (isinstance(counter, str) and counter.strip()):
        errors.append(
            f"verdicts[{index}].counter_evidence is required when evidence_assessment is 'disputed'"
        )
    elif counter is not None and not isinstance(counter, str):
        errors.append(f"verdicts[{index}].counter_evidence must be a string or null")

    amended = entry.get("amended_claim")
    if verdict == "amended" and not (isinstance(amended, str) and amended.strip()):
        errors.append(
            f"verdicts[{index}].amended_claim is required for an 'amended' verdict: "
            "the successor claim needs wording of its own to be judged next round"
        )
    elif amended is not None and not isinstance(amended, str):
        errors.append(f"verdicts[{index}].amended_claim must be a string or null")

    return errors


def validate_payload(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    entries = payload.get("verdicts")
    if not isinstance(entries, list):
        return ["payload has no 'verdicts' array"]

    errors: list[str] = []
    for index, entry in enumerate(entries):
        errors.extend(_validate_one(index, entry))

    # One judge, one verdict per claim. Two verdicts on the same id from the
    # same judge would both count toward unanimity, letting a single judge
    # manufacture a quorum by repeating itself.
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        claim_id = entry.get("claim_id")
        if isinstance(claim_id, str):
            if claim_id in seen:
                errors.append(f"duplicate verdict for claim {claim_id!r} in one judge's output")
            seen.add(claim_id)
    return errors


def is_successful_payload(payload: dict[str, Any]) -> bool:
    """A judge with nothing to say has failed, not succeeded.

    Unlike a critique round, there is no honest empty result here: a judge is
    only dispatched when it has at least one claim to judge (it is never
    handed a slice made entirely of its own claims -- see verdicts.judges_for),
    so an empty `verdicts` array means it did not do the work.
    """
    if validate_payload(payload):
        return False
    return bool(payload.get("verdicts"))


def verdict_tier(parsed: dict[str, Any], errors: list[str]) -> int:
    """Rank a parsed candidate; lower wins. Mirrors claimschema.claim_tier's
    reasoning: a substantive-but-broken judgement must outrank a trivially
    well-formed scrap, because a false failure costs a re-run while a false
    success silently drops a judge's opinion out of the tally.

    There is no "nothing to report" tier here -- see is_successful_payload.
    """
    entries = parsed.get("verdicts")
    if not errors and isinstance(entries, list) and entries:
        return 0
    if parsed.get("verdicts") is not None:
        return 1
    return 2


VERDICT_CONTRACT = PayloadContract(
    name="verdicts",
    validate=validate_payload,
    is_successful=is_successful_payload,
    tier=verdict_tier,
    container_key="verdicts",
    empty_message="no verdicts returned; a judge with claims to judge must return one each",
)

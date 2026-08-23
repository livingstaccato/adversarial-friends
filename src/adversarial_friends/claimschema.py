"""The contract a friend's output must satisfy.

Claude, codex, and agy can enforce this natively via a schema flag. gemini and
opencode cannot, so the same shape is stated in the prompt and validated here.
Validation is hand-rolled because the runtime is stdlib-only.
"""

import json
from pathlib import Path
from typing import Any

from .contracts import PayloadContract

SEVERITIES = ("high", "medium", "low")
REQUIRED_FIELDS = (
    "severity",
    "claim",
    "evidence",
    "failure_scenario",
    "suggested_fix",
)

CLAIM_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "no_findings": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "claim": {"type": "string"},
                    "location": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "failure_scenario": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": list(REQUIRED_FIELDS),
            },
        },
    },
}


def schema_path(directory: Path) -> Path:
    """Materialize the schema so adapters with a native schema flag can use it."""
    path = Path(directory) / "claim-output.schema.json"
    path.write_text(json.dumps(CLAIM_OUTPUT_SCHEMA, indent=2), encoding="utf-8")
    return path


def validate_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    if payload.get("no_findings") is True:
        # When no_findings is True, findings must be absent or empty
        findings = payload.get("findings")
        if findings is not None and (not isinstance(findings, list) or findings):
            errors.append(
                "payload asserts no_findings but also carries findings; "
                "contradictory output indicates confused friend"
            )
        return errors
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return ["payload has neither 'findings' array nor no_findings marker"]
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] is not an object")
            continue
        for field in REQUIRED_FIELDS:
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"findings[{index}].{field} missing or empty")
        severity = finding.get("severity")
        if isinstance(severity, str) and severity not in SEVERITIES:
            errors.append(f"findings[{index}].severity {severity!r} not in {SEVERITIES}")
        # Validate location field - must be string or null
        location = finding.get("location")
        if location is not None and not isinstance(location, str):
            errors.append(
                f"findings[{index}].location must be string or null, not {type(location).__name__}"
            )
    return errors


def is_successful_payload(payload: dict[str, Any]) -> bool:
    """Distinguish 'looked and found nothing' from 'produced nothing'."""
    if validate_payload(payload):
        return False
    if payload.get("no_findings") is True:
        return True
    return bool(payload.get("findings"))


def claim_tier(parsed: dict[str, Any], errors: list[str]) -> int:
    """Rank a parsed candidate. Lower is preferred; ties keep first-seen.

    A false failure costs a re-run; a false "clean" hides whatever the friend
    actually found. Those costs are not symmetric, so a substantive-but-broken
    critique must always outrank a trivially clean one -- otherwise a real
    finding sitting next to an unrelated, well-formed '{"no_findings": true}'
    fragment (or any other trivially-valid scrap) would be silently discarded
    in favor of the scrap. Tiers, most preferred first:

      0. Validates cleanly AND has at least one real finding -- a
         substantive, well-formed critique. Nothing beats this.
      1. Has a "findings" key at all, even if it's empty or fails
         validation -- a substantive *attempt* whose validation errors are
         still worth surfacing to the caller as an honest, specific failure.
      2. Validates cleanly as an explicit "nothing to report" marker.
      3. Anything else that merely parsed as a JSON object.
    """
    findings = parsed.get("findings")
    if not errors and isinstance(findings, list) and findings:
        return 0
    if "findings" in parsed:
        return 1
    if not errors and parsed.get("no_findings") is True:
        return 2
    return 3


CLAIM_CONTRACT = PayloadContract(
    name="claims",
    validate=validate_payload,
    is_successful=is_successful_payload,
    tier=claim_tier,
    container_key="findings",
    empty_message="no findings and no explicit no_findings marker",
)

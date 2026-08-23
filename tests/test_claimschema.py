import json

from adversarial_friends import claimschema


def test_schema_file_is_written_and_is_valid_json(tmp_path):
    path = claimschema.schema_path(tmp_path)
    assert json.loads(path.read_text())["type"] == "object"


def test_valid_payload_has_no_errors():
    payload = {"findings": [{
        "severity": "high", "claim": "the guard is missing",
        "location": "src/auth.py:42", "evidence": "src/auth.py:38",
        "failure_scenario": "expired token reaches the handler",
        "suggested_fix": "check exp before dispatch",
    }]}
    assert claimschema.validate_payload(payload) == []


def test_missing_required_field_is_reported():
    payload = {"findings": [{"severity": "high", "claim": "x"}]}
    errors = claimschema.validate_payload(payload)
    assert any("failure_scenario" in e for e in errors)


def test_bad_severity_is_reported():
    payload = {"findings": [{
        "severity": "catastrophic", "claim": "x", "location": None,
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    errors = claimschema.validate_payload(payload)
    assert any("severity" in e for e in errors)


def test_no_findings_marker_is_successful():
    assert claimschema.is_successful_payload({"no_findings": True}) is True


def test_empty_findings_without_marker_is_not_successful():
    """Silence must be distinguishable from breakage."""
    assert claimschema.is_successful_payload({"findings": []}) is False


def test_findings_present_is_successful():
    payload = {"findings": [{
        "severity": "low", "claim": "x", "location": None, "evidence": "e",
        "failure_scenario": "f", "suggested_fix": "s",
    }]}
    assert claimschema.is_successful_payload(payload) is True

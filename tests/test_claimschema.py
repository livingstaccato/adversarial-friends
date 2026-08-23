import json

import pytest

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


@pytest.mark.parametrize("payload", [
    ["not", "a", "dict"],
    "a bare string",
    None,
    42,
])
def test_non_dict_payload_is_reported_not_raised(payload):
    """Friend output is untrusted text; validation must never raise."""
    errors = claimschema.validate_payload(payload)
    assert errors and isinstance(errors, list)


def test_findings_not_a_list_is_reported():
    errors = claimschema.validate_payload({"findings": "garbage"})
    assert any("findings" in e for e in errors)


@pytest.mark.parametrize("finding", ["a string", 42, None, ["nested"]])
def test_non_dict_finding_entry_is_reported(finding):
    errors = claimschema.validate_payload({"findings": [finding]})
    assert any("findings[0]" in e for e in errors)


def test_whitespace_only_required_field_is_reported():
    payload = {"findings": [{
        "severity": "high", "claim": "   ", "location": None,
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    assert any("claim" in e for e in claimschema.validate_payload(payload))


def test_no_findings_marker_with_findings_string_is_contradictory():
    """no_findings: True with a findings value means confused friend."""
    errors = claimschema.validate_payload({"no_findings": True, "findings": "garbage"})
    assert errors


def test_no_findings_marker_with_invalid_findings_is_contradictory():
    """no_findings: True with invalid findings means confused friend."""
    errors = claimschema.validate_payload({
        "no_findings": True,
        "findings": [{"severity": "bogus"}]
    })
    assert errors


def test_no_findings_marker_with_empty_findings_list_is_valid():
    """no_findings: True with empty findings is consistent."""
    errors = claimschema.validate_payload({"no_findings": True, "findings": []})
    assert not errors


def test_location_as_string_is_valid():
    payload = {"findings": [{
        "severity": "high", "claim": "x", "location": "src/auth.py:42",
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    assert claimschema.validate_payload(payload) == []


def test_location_as_null_is_valid():
    payload = {"findings": [{
        "severity": "high", "claim": "x", "location": None,
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    assert claimschema.validate_payload(payload) == []


def test_location_as_int_is_reported():
    payload = {"findings": [{
        "severity": "high", "claim": "x", "location": 42,
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    errors = claimschema.validate_payload(payload)
    assert any("location" in e for e in errors)


def test_location_as_list_is_reported():
    payload = {"findings": [{
        "severity": "high", "claim": "x", "location": ["nested"],
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    errors = claimschema.validate_payload(payload)
    assert any("location" in e for e in errors)

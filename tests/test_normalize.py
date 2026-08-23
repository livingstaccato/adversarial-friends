import json

from adversarial_friends import normalize
from adversarial_friends.normalize import Envelope, EnvelopeRule, parse_envelope

GOOD = '{"findings": [{"severity": "low", "claim": "c", "location": null, ' \
       '"evidence": "e", "failure_scenario": "f", "suggested_fix": "s"}]}'


def test_plain_json_parses():
    result = normalize.normalize(GOOD)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_ansi_interleaved_json_is_recovered():
    """ollama run writes cursor/spinner codes into the middle of its payload."""
    noisy = '\x1b[?25l\x1b[?2026h{"\x1b[?25lfind\x1b[?25hings": []}\x1b[?25h'
    assert normalize.strip_ansi(noisy) == '{"findings": []}'


def test_fenced_json_is_extracted():
    fenced = "Here is my review:\n```json\n" + GOOD + "\n```\nHope that helps!"
    result = normalize.normalize(fenced)
    assert result.succeeded is True


def test_prose_wrapped_json_is_extracted():
    wrapped = "Sure! " + GOOD + " Let me know if you want more."
    result = normalize.normalize(wrapped)
    assert result.succeeded is True


def test_trailing_comma_is_repaired():
    result = normalize.normalize('{"no_findings": true,}')
    assert result.succeeded is True


def test_no_findings_marker_succeeds():
    assert normalize.normalize('{"no_findings": true}').succeeded is True


def test_empty_findings_without_marker_fails():
    result = normalize.normalize('{"findings": []}')
    assert result.succeeded is False


def test_unparseable_output_fails_with_errors():
    result = normalize.normalize("I could not complete this task.")
    assert result.succeeded is False
    assert result.errors


def test_off_topic_prose_fails():
    """agy answered the literal prompt '--mode' and exited 0."""
    result = normalize.normalize(
        "It looks like you just typed `--mode`. Could you clarify?"
    )
    assert result.succeeded is False


# --- Regression tests added after adversarial testing (not in the brief) ---
#
# Both fixtures below were verified against a reconstruction of the brief's
# own Step-3 code before being added here: the recursion fixture raised an
# uncaught RecursionError, and the decoy fixture returned succeeded=False
# with the wrong (decoy) payload picked up. See task-7-report.md for the
# side-by-side evidence.


def test_deeply_nested_json_does_not_raise():
    """normalize() must never raise, even on input crafted to blow the
    interpreter's recursion limit inside json.loads. A friend's output is
    untrusted text; this must surface as a failed parse, not a crash."""
    deeply_nested = '{"findings": ' + "[" * 3000 + "]" * 3000 + '}'
    result = normalize.normalize(deeply_nested)  # must not raise
    assert result.succeeded is False
    assert result.errors


def test_decoy_object_before_real_fenced_answer_is_not_picked():
    """A friend's prose can contain an earlier, incidental '{...}' (e.g. an
    illustrative example of the expected format) before its real, fenced
    answer. The earlier decoy must not be mistaken for the real payload when
    the real one validates and the decoy does not.

    Note: under the tiered candidate preference added in round 2 (see
    _candidate_tier), this specific fixture no longer requires fence-priority
    to pass -- exhaustive whole-text scanning plus tier comparison recovers
    the real answer either way. It's kept as a general regression check, not
    as proof fence-priority is load-bearing; see
    test_stray_unmatched_brace_outside_fence_does_not_hide_real_answer below
    for the fixture that actually requires it (verified by disabling
    fence-priority and confirming that one alone fails)."""
    raw = (
        'e.g. {"findings": []}. Real answer:\n'
        "```json\n"
        "Here it is: " + GOOD + "\n"
        "```"
    )
    result = normalize.normalize(raw)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


# --- Regression tests added after round-2 coordinator review ---
#
# Round 2 found: (1) the round-1 "prefer first schema-valid+successful
# candidate" rule could discard a real, substantive finding in favor of a
# trivially-clean but irrelevant '{"no_findings": true}' fragment elsewhere
# in the same output; (2) the trailing-comma repair looped to a fixed point,
# which is quadratic on a long run of consecutive commas. Both are fixed in
# this file; see task-7-report.md for the coordinator's ruling and the
# before/after evidence.


def test_sql_injection_finding_beats_stray_no_findings_marker():
    """Exact reviewer scenario: a real finding with one schema defect
    (severity out of enum), followed anywhere later by an unrelated, clean
    '{"no_findings": true}' fragment. The substantive-but-broken finding must
    win -- surfaced as an honest failure with its actual error -- rather than
    the trivially-clean marker silently discarding it as a false clean."""
    raw = (
        '{"findings": [{"severity": "critical", "claim": "SQL injection via '
        'unsanitized query param", "location": "src/db.py:88", '
        '"evidence": "src/db.py:88 concatenates request.args into SQL", '
        '"failure_scenario": "attacker-controlled input reaches the query '
        'unescaped", "suggested_fix": "use parameterized queries"}]}\n'
        'Anyway, just to note the format: {"no_findings": true}'
    )
    result = normalize.normalize(raw)
    assert result.succeeded is False
    assert result.payload is not None and "findings" in result.payload
    assert any("severity" in e for e in result.errors)
    assert result.payload.get("no_findings") is not True


def test_schema_invalid_finding_is_reported_not_just_absent():
    """normalize() must wire extract_json's winning candidate through
    validate_payload end-to-end: a payload that parses as JSON but violates
    the claim schema (severity out of enum) must fail with that specific
    schema error, not the generic 'no findings' message. No mutation of the
    9 required tests exercises this branch (see task-7-report.md)."""
    raw = (
        '{"findings": [{"severity": "critical", "claim": "c", "location": null, '
        '"evidence": "e", "failure_scenario": "f", "suggested_fix": "s"}]}'
    )
    result = normalize.normalize(raw)
    assert result.succeeded is False
    assert any("severity" in e for e in result.errors)


def test_stray_unmatched_brace_outside_fence_does_not_hide_real_answer():
    """A single unmatched '{' anywhere earlier in a friend's prose (outside
    any fence) permanently perturbs single-pass whole-text brace-depth
    tracking -- depth never returns to zero again, so a whole-text-only scan
    can miss a perfectly well-formed answer later in the same text. Scanning
    each fenced block's content in isolation (fence-priority) resets depth at
    the fence boundary and recovers it. Verified this fixture specifically
    (not the one above) fails without fence-priority by disabling the fence
    loop and rerunning against the live module."""
    raw = (
        "Note the syntax uses a stray { without a match. Anyway here's my "
        "real answer:\n```json\n" + GOOD + "\n```"
    )
    result = normalize.normalize(raw)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


# --- C1: envelope unwrapping (whole-branch review) -------------------------
#
# Every shipped adapter asks its CLI for structured output (--output-format
# json / --json / --format json), which wraps the model's actual answer in
# an envelope of the CLI's own. The findings JSON then sits inside a quoted
# STRING value in that envelope -- structurally unreachable by
# _iter_balanced_objects, which correctly never descends into string
# content. Envelope/unwrap_envelope/parse_envelope below let normalize()
# unwrap first, declaratively, before falling back to exactly the scan that
# existed before any of this. Real per-adapter fixtures (captured agy/
# opencode shapes, an unverified bare-object case for claude/codex) live in
# tests/fixtures/ and are exercised end to end through adapters.load_adapters
# in test_envelope_fixtures.py; the tests below are unit-level, against
# synthetic data, for the mechanism itself.


def test_parse_envelope_json_path():
    env = parse_envelope({"kind": "json_path", "path": "response"})
    assert env == Envelope(kind="json_path", path="response")


def test_parse_envelope_ndjson_with_rules():
    env = parse_envelope({
        "kind": "ndjson", "match_field": "type",
        "rules": [{"type": "error", "field": "error.data.message"}],
    })
    assert env.kind == "ndjson"
    assert env.match_field == "type"
    assert env.rules == (EnvelopeRule(match_value="error", field="error.data.message"),)


def test_parse_envelope_none_or_empty_is_none():
    assert parse_envelope(None) is None
    assert parse_envelope({}) is None


def test_parse_envelope_unknown_kind_is_none():
    """A typo'd or future `kind` degrades to 'no envelope' rather than
    raising -- adapter config is trusted, but there's no reason a config
    mistake here should be fatal when 'don't unwrap' is always safe."""
    assert parse_envelope({"kind": "xml_path", "path": "response"}) is None


def test_parse_envelope_json_path_without_a_path_is_none():
    assert parse_envelope({"kind": "json_path"}) is None


def test_unwrap_json_path_extracts_the_field():
    envelope = Envelope(kind="json_path", path="response")
    raw = json.dumps({"status": "SUCCESS", "response": GOOD})
    assert normalize.unwrap_envelope(raw, envelope) == GOOD


def test_unwrap_json_path_supports_dotted_paths():
    envelope = Envelope(kind="json_path", path="result.text")
    raw = json.dumps({"result": {"text": GOOD}})
    assert normalize.unwrap_envelope(raw, envelope) == GOOD


def test_unwrap_json_path_missing_key_returns_none():
    envelope = Envelope(kind="json_path", path="response")
    raw = json.dumps({"status": "ERROR", "response": ""})
    assert normalize.unwrap_envelope(raw, envelope) is None


def test_unwrap_json_path_non_json_raw_returns_none():
    envelope = Envelope(kind="json_path", path="response")
    assert normalize.unwrap_envelope("not json at all", envelope) is None


def test_unwrap_ndjson_extracts_matching_lines():
    envelope = Envelope(kind="ndjson", match_field="type",
                        rules=(EnvelopeRule(match_value="error", field="error.message"),))
    raw = "\n".join([
        json.dumps({"type": "step_finish", "tokens": {"total": 10}}),
        json.dumps({"type": "error", "error": {"message": "auth failed"}}),
    ])
    assert normalize.unwrap_envelope(raw, envelope) == "auth failed"


def test_unwrap_ndjson_skips_malformed_lines():
    envelope = Envelope(kind="ndjson", rules=(EnvelopeRule(match_value="error", field="message"),))
    raw = "not json\n" + json.dumps({"type": "error", "message": "boom"}) + "\nalso not json"
    assert normalize.unwrap_envelope(raw, envelope) == "boom"


def test_unwrap_ndjson_no_matching_line_returns_none():
    envelope = Envelope(kind="ndjson", rules=(EnvelopeRule(match_value="error", field="message"),))
    raw = json.dumps({"type": "step_finish"})
    assert normalize.unwrap_envelope(raw, envelope) is None


def test_normalize_unwraps_envelope_before_scanning():
    envelope = Envelope(kind="json_path", path="response")
    raw = json.dumps({"status": "SUCCESS", "response": GOOD})
    result = normalize.normalize(raw, envelope=envelope)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_normalize_unwraps_a_findings_json_string_nested_inside_the_envelope():
    """The exact structural bug C1 describes: the findings object is inside
    a quoted STRING value, unreachable by a plain brace-scan of the whole
    envelope. Asserts both directions: this succeeds WITH envelope
    unwrapping, and (negative control, same raw text) fails without it --
    proving the envelope is what makes the difference, not some incidental
    property of this particular fixture."""
    envelope = Envelope(kind="json_path", path="response")
    raw = json.dumps({"status": "SUCCESS", "response": GOOD})
    assert normalize.normalize(raw, envelope=envelope).succeeded is True
    assert normalize.normalize(raw).succeeded is False  # negative control, no envelope


def test_envelope_fallback_a_bare_findings_object_still_succeeds():
    """Required regression: an adapter with an envelope DECLARED, given
    output that is already a bare findings object (not wrapped at all --
    e.g. because this run's CLI version changed, or the envelope config is
    simply wrong for this particular output), must still succeed. Envelope
    unwrapping finding nothing (the declared "response" key doesn't exist on
    this bare object) is exactly the signal to fall back to the scan that
    worked before envelopes existed at all -- unwrapping must never make a
    currently-working case fail."""
    envelope = Envelope(kind="json_path", path="response")
    result = normalize.normalize(GOOD, envelope=envelope)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_envelope_fallback_also_works_for_ndjson_envelopes():
    envelope = Envelope(kind="ndjson", rules=(EnvelopeRule(match_value="error", field="message"),))
    result = normalize.normalize(GOOD, envelope=envelope)
    assert result.succeeded is True


def test_every_existing_bare_object_case_is_unaffected_by_envelope_none():
    """Sanity check on the default: normalize(raw) with no envelope argument
    at all must be byte-for-byte the prior behavior. Covered implicitly by
    every test above this section (none of them pass `envelope=`), asserted
    explicitly here as a named regression guard."""
    assert normalize.normalize(GOOD).succeeded is True
    assert normalize.normalize('{"no_findings": true}').succeeded is True
    assert normalize.normalize("not json").succeeded is False


# --- C1 item 3: legible failure for a genuinely unknown envelope shape ----


def test_structured_output_hint_appears_when_a_structured_wrapper_has_no_findings():
    """claude/codex's real envelope shape is unknown (no envelope declared
    -- capturing it costs a metered call), so this uses a synthetic,
    illustrative wrapper shape, NOT any real captured CLI output. The point
    is the mechanism: a CLI that was explicitly asked for structured output
    (structured_output=True) but whose parsed JSON has no findings/
    no_findings key at all gets an actionable hint, not a bare 'no
    findings' that reads as a friend's own broken output."""
    synthetic_wrapper = json.dumps({"type": "result", "session_id": "abc123"})
    result = normalize.normalize(synthetic_wrapper, structured_output=True)
    assert result.succeeded is False
    assert any("envelope path" in e for e in result.errors)


def test_structured_output_hint_is_suppressed_when_the_adapter_did_not_ask_for_it():
    synthetic_wrapper = json.dumps({"type": "result", "session_id": "abc123"})
    result = normalize.normalize(synthetic_wrapper, structured_output=False)
    assert result.succeeded is False
    assert not any("envelope path" in e for e in result.errors)


def test_structured_output_hint_can_surface_via_the_raw_retry_after_a_failed_unwrap():
    """Post-wave regression fix: when the DIRECT unwrap attempt fails (the
    extracted text parses as JSON but has no findings key), normalize() no
    longer commits to that failure exclusively -- it retries the scan
    against the untouched raw text, with the real structured_output value
    (not forced False), so the hint can still surface there. Suppressing it
    unconditionally once ANY text had been extracted (the previous, buggy
    behavior this test used to assert) would silence the one message that
    explains why the "obvious" unwrap didn't pan out, exactly when a reader
    needs it most -- see test_envelope_fixtures.py's regression tests for
    the real captured shapes this was reproduced against end to end."""
    envelope = Envelope(kind="json_path", path="response")
    wrapper_with_no_findings_answer = json.dumps({
        "status": "SUCCESS", "response": json.dumps({"type": "result"}),
    })
    result = normalize.normalize(wrapper_with_no_findings_answer, envelope=envelope,
                                 structured_output=True)
    assert result.succeeded is False
    assert any("envelope path" in e for e in result.errors)


def test_structured_output_hint_is_suppressed_when_the_raw_retry_also_forces_it_false():
    """The suppress-on-direct-unwrap rule is still real -- it just isn't the
    ONLY rule anymore. Passing structured_output=False end to end (the
    caller's real signal that this adapter never asked for structured
    output at all) must still produce no hint anywhere, on the direct
    attempt OR the raw retry."""
    envelope = Envelope(kind="json_path", path="response")
    wrapper_with_no_findings_answer = json.dumps({
        "status": "SUCCESS", "response": json.dumps({"type": "result"}),
    })
    result = normalize.normalize(wrapper_with_no_findings_answer, envelope=envelope,
                                 structured_output=False)
    assert result.succeeded is False
    assert not any("envelope path" in e for e in result.errors)


def test_envelope_retry_recovers_findings_the_direct_unwrap_alone_would_have_lost():
    """Unit-level version of the exact regression reproduced end to end in
    test_envelope_fixtures.py: unwrapping matches something (a field that
    isn't the real answer), that direct attempt fails, and the real
    findings are recovered only because normalize() retries against the
    untouched raw text instead of committing to the failed direct attempt."""
    envelope = Envelope(kind="json_path", path="response")
    raw = json.dumps({
        "response": "unrelated prose, not the answer",
        "findings": [{"severity": "low", "claim": "c", "location": None,
                     "evidence": "e", "failure_scenario": "f", "suggested_fix": "s"}],
    })
    result = normalize.normalize(raw, envelope=envelope)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_ndjson_envelope_retry_recovers_findings_past_a_matched_error_line():
    envelope = Envelope(kind="ndjson", rules=(EnvelopeRule(match_value="error", field="message"),))
    raw = "\n".join([
        json.dumps({"type": "error", "message": "rate limited, retrying"}),
        GOOD,
    ])
    result = normalize.normalize(raw, envelope=envelope)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_structured_output_hint_does_not_fire_for_ordinary_schema_errors():
    """The hint is specifically for "no findings key at all" -- a payload
    that DOES have a findings key but fails schema validation (e.g. a bad
    severity enum) is the friend's own fault, not an envelope problem, and
    must keep its specific, existing error message unpolluted."""
    raw = ('{"findings": [{"severity": "critical", "claim": "c", "location": null, '
          '"evidence": "e", "failure_scenario": "f", "suggested_fix": "s"}]}')
    result = normalize.normalize(raw, structured_output=True)
    assert result.succeeded is False
    assert any("severity" in e for e in result.errors)
    assert not any("envelope path" in e for e in result.errors)

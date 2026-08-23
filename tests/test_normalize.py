from adversarial_friends import normalize

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

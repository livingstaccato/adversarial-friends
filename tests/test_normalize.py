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
    the real one validates and the decoy does not."""
    raw = (
        'e.g. {"findings": []}. Real answer:\n'
        "```json\n"
        "Here it is: " + GOOD + "\n"
        "```"
    )
    result = normalize.normalize(raw)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"

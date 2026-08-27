"""C1 fixture item 4: one real captured-stdout fixture per shipped adapter,
asserted end to end through the REAL adapter TOML (adapters.load_adapters)
and the REAL unwrapping path in normalize.normalize -- not hand-built
Envelope objects (those are covered separately, at the mechanism level, in
test_normalize.py).

agy and opencode: the JSON below is byte-for-byte what was captured from
those exact CLIs (see adapters/agy.toml and adapters/opencode.toml, and the
whole-branch review that produced this file). Only agy's "findings" fixture
substitutes real findings JSON into the `response` field in place of the
captured "ok\\n" -- the captured success example never actually exercised a
real critique, so a second fixture proves the field, once isolated, is
handed back through the SAME extract/validate logic every other adapter
uses, not something bespoke to agy.

claude and codex: captured 2026-08-23 by running each CLI once on its own
subscription plan (not a metered API key) and saving stdout verbatim.
claude 2.1.240 emits a single JSON object with type="result" and the answer
in `result` as a JSON-escaped string. codex 0.149.0 emits an NDJSON event
stream -- thread.started, turn.started, item.completed, turn.completed --
with the answer in the item.completed event whose item.type is
"agent_message", at item.text. codex additionally writes a non-JSON line
("Reading additional input from stdin...") to stdout, so its fixture also
exercises the NDJSON reader's requirement to skip unparseable lines rather
than fail on them.

opencode_error_then_findings.ndjson / agy_response_prose_with_top_level_findings.json:
added for the whole-branch re-review's Regression 1 -- an envelope rule
matching something OTHER than the real answer (a benign "rate limited,
retrying" error event; a `response` field full of unrelated prose) must not
make normalize() commit to that match exclusively when real findings are
sitting elsewhere in the same output (a second NDJSON line; the envelope
object's own top-level `findings` key). Both still use the real, declared
`[envelope]` for their adapter -- only the surrounding content is
constructed to reproduce the regression, not the envelope shape itself.
"""

from pathlib import Path

from adversarial_friends import adapters, normalize

REPO = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO / "src" / "adversarial_friends" / "assets" / "adapters"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _registry():
    return adapters.load_adapters(ADAPTER_DIR)


def _normalize_fixture(cli_name: str, fixture_name: str) -> normalize.NormalizeResult:
    adapter = _registry()[cli_name]
    raw = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    return normalize.normalize(
        raw, envelope=adapter.envelope, structured_output=adapter.structured_output
    )


# --- agy: captured json_path envelope ("response") -------------------------


def test_agy_success_findings_fixture_unwraps_to_the_real_finding():
    result = _normalize_fixture("agy", "agy_success_findings.json")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "the guard is missing"
    assert result.payload["findings"][0]["severity"] == "high"


def test_agy_success_response_ok_fixture_is_a_legible_non_json_failure():
    """The literal captured example from the review has response="ok\\n" --
    a trivial, non-JSON answer (not a real critique). Once unwrapped, "ok\\n"
    correctly fails to parse as JSON on its own -- but that failure is not
    the end of it: normalize() retries the scan against the untouched raw
    envelope (post-wave regression fix -- see
    test_ndjson_stream_with_a_benign_error_event_and_real_findings_still_succeeds
    for why this retry exists at all), and the raw envelope itself DOES
    parse as JSON (it's the whole wrapper object), just with no findings
    key -- so the final result carries the structured_output hint, the same
    outcome as the error fixture below, not a bare 'no parseable JSON'."""
    result = _normalize_fixture("agy", "agy_success_response_ok.json")
    assert result.succeeded is False
    assert result.payload is not None  # the raw envelope itself parsed as JSON
    assert any("envelope path" in e for e in result.errors)


def test_agy_error_fixture_falls_back_and_reports_legibly():
    """Captured verbatim: status=ERROR, response="" (empty -- unwrap finds
    nothing to extract), error=<a real agy CLI error message>. Unwrapping
    reports "found nothing" (an empty string is not a usable answer), so
    normalize() falls back to scanning the raw envelope object directly --
    which DOES parse as JSON (it's the whole wrapper) but has no findings
    key, so the structured_output hint applies."""
    result = _normalize_fixture("agy", "agy_error.json")
    assert result.succeeded is False
    assert result.payload is not None  # the raw envelope itself parsed as JSON
    assert any("envelope path" in e for e in result.errors)


def test_agy_response_prose_with_top_level_findings_still_succeeds():
    """Regression (post-wave re-review): a captured-shape envelope whose
    `response` field holds unrelated prose (agy answering conversationally
    instead of with JSON) while the ENVELOPE OBJECT ITSELF -- one level up
    -- also happens to carry a valid top-level `findings` array. Before the
    fix, committing to the unwrapped `response` text exclusively (it fails
    to parse, since it's plain prose) discarded this real, schema-valid
    `findings` array sitting right next to it -- an outcome that used to
    succeed via the plain raw-text scan, before this adapter had an
    envelope at all."""
    result = _normalize_fixture("agy", "agy_response_prose_with_top_level_findings.json")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "missing rate limit on login endpoint"


# --- opencode: captured ndjson envelope (the "error" event only) ----------


def test_opencode_error_ndjson_fixture_unwraps_the_real_error_message():
    adapter = _registry()["opencode"]
    raw = (FIXTURES / "opencode_error.ndjson").read_text(encoding="utf-8")
    unwrapped = normalize.unwrap_envelope(raw, adapter.envelope)
    assert unwrapped == (
        "CLOUDFLARE_GATEWAY_ID missing. Set with: export CLOUDFLARE_GATEWAY_ID=<value>"
    )
    result = _normalize_fixture("opencode", "opencode_error.ndjson")
    assert result.succeeded is False
    # The unwrapped text (a plain error string, not JSON) fails on its own,
    # so normalize() retries against the raw NDJSON text -- which DOES
    # contain a parseable JSON object (the error event itself, as its own
    # bare top-level object), just with no findings key. Same outcome as
    # the step_finish-only fixture below, reached via the other code path
    # (there, unwrapping finds nothing at all; here, it finds text that
    # itself fails to parse) -- see
    # test_ndjson_stream_with_a_benign_error_event_and_real_findings_still_succeeds
    # for the case where the retry actually recovers real findings instead.
    assert result.payload is not None
    assert "payload has neither" in result.errors[0]
    assert any("envelope path" in e for e in result.errors)


def test_opencode_step_finish_only_fixture_falls_back_and_reports_legibly():
    """No "error" event in this stream, so ndjson unwrapping finds nothing
    to extract (the only declared rule is for type="error" -- see
    adapters/opencode.toml's comment on why no success-event rule is
    declared) and normalize() falls back to scanning the raw NDJSON text.
    That scan finds the step_finish event object itself (a bare JSON object
    with no findings key), so the structured_output hint applies -- same
    mechanism as the agy error fixture above, exercised on the OTHER known
    real shape."""
    result = _normalize_fixture("opencode", "opencode_step_finish_only.ndjson")
    assert result.succeeded is False
    assert result.payload is not None
    assert any("envelope path" in e for e in result.errors)


def test_ndjson_stream_with_a_benign_error_event_and_real_findings_still_succeeds():
    """The exact regression reproduced by the re-review: an opencode-shaped
    NDJSON stream whose FIRST line matches the declared "error" rule with a
    benign, non-fatal message ("rate limited, retrying" -- opencode's own
    protocol overloads the "error" event type for transient conditions, not
    only fatal ones), followed by a second line that is a clean, schema-valid
    findings object. Before the fix, matching the error rule at all meant
    the unwrapped (error-message) text was used EXCLUSIVELY -- that text
    fails to parse, so the real findings sitting one line later were
    silently discarded and the run reported a status that was actively
    false ("no parseable JSON object" when the stdout plainly contained
    one). opencode's only declared rule is the error rule (see
    adapters/opencode.toml), so before this fix ANY error event anywhere in
    a stream disabled findings extraction entirely, for every real
    opencode run that hit one, however transient."""
    result = _normalize_fixture("opencode", "opencode_error_then_findings.ndjson")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "the guard is missing"


# --- claude / codex: real captured stdout through declared envelopes ------


def test_claude_pre_schema_stdout_fails_legibly():
    """Real claude 2.1.240 stdout, captured verbatim on 2026-08-23 WITHOUT
    --json-schema: the answer lives in `result` as a JSON-escaped string and
    there is no `structured_output` key at all.

    This used to assert success, when the envelope targeted `result`. The
    adapter always passes a schema now, and the envelope targets the object
    claude validated (`structured_output`, see the fixture test above), so
    this shape can only mean the CLI ignored the schema -- and the right
    outcome for that is the legible envelope-path failure, not a silent
    success on a copy that nothing validated. Kept so that fallback stays
    legible rather than turning into "no parseable JSON"."""
    result = _normalize_fixture("claude", "claude_stdout_captured.json")
    assert result.succeeded is False
    assert result.payload is not None  # the wrapper itself parsed as JSON
    assert any("envelope path" in e for e in result.errors)


def test_codex_captured_stdout_normalizes_successfully():
    """Real codex 0.149.0 stdout, captured verbatim, including the non-JSON
    "Reading additional input from stdin..." line it writes before the event
    stream. Passing requires both skipping that line and selecting the
    item.completed event rather than any other."""
    result = _normalize_fixture("codex", "codex_stdout_captured.jsonl")
    assert result.succeeded is True
    assert result.payload == {"no_findings": True}


# --- claude under --json-schema: captured 2026-08-26 ------------------------


def test_claude_structured_output_fixture_unwraps_to_the_real_findings():
    """Captured verbatim from `afriend run --friend claude:security` after
    the schema fix. Under --json-schema claude puts the validated object in
    `structured_output` (an OBJECT) and a serialized copy in `result` (a
    string). The envelope now targets the object, which needed
    _unwrap_json_path to accept a dict target -- it accepted only strings,
    so the first run after the schema fix still failed with "structured JSON
    but contained no findings". This fixture pins the whole path through
    the real adapter TOML."""
    result = _normalize_fixture("claude", "claude_structured_output_captured.json")
    assert result.succeeded is True, result.errors
    findings = result.payload["findings"]
    assert len(findings) >= 3
    assert findings[0]["severity"] == "high"
    assert "Logout" in findings[0]["claim"]

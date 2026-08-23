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

claude and codex: their real envelope shape has not been captured (doing so
costs a metered call against a live API, which this suite must never make --
see the repo's test constraints). No [envelope] is declared for either
adapter (see adapters/claude.toml, adapters/codex.toml), so normalize() never
attempts to unwrap their output at all -- it falls straight to the same
bare-object scan every adapter used before envelopes existed. The two
`*_UNVERIFIED.json` fixtures below are a synthetic bare-findings-object
case, included ONLY to prove that scan still works unchanged now that
structured_output=True is set for these two adapters (see
test_normalize.py's structured_output tests for the mechanism this enables).
They are explicitly NOT a claim about what claude/codex's real stdout looks
like -- nobody should mistake them for captured output.
"""
from pathlib import Path

from adversarial_friends import adapters, normalize

REPO = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO / "skills" / "adversarial-friends" / "adapters"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _registry():
    return adapters.load_adapters(ADAPTER_DIR)


def _normalize_fixture(cli_name: str, fixture_name: str) -> normalize.NormalizeResult:
    adapter = _registry()[cli_name]
    raw = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    return normalize.normalize(raw, envelope=adapter.envelope,
                               structured_output=adapter.structured_output)


# --- agy: captured json_path envelope ("response") -------------------------


def test_agy_success_findings_fixture_unwraps_to_the_real_finding():
    result = _normalize_fixture("agy", "agy_success_findings.json")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "the guard is missing"
    assert result.payload["findings"][0]["severity"] == "high"


def test_agy_success_response_ok_fixture_is_a_legible_non_json_failure():
    """The literal captured example from the review has response="ok\\n" --
    a trivial, non-JSON answer (not a real critique). Once unwrapped, "ok\\n"
    correctly fails to parse as JSON; this is NOT the "structured wrapper
    with no findings" case (that requires the WRAPPER itself, post-fallback,
    to be the thing that parsed as JSON) -- see the error fixture below for
    that one."""
    result = _normalize_fixture("agy", "agy_success_response_ok.json")
    assert result.succeeded is False
    assert result.payload is None
    assert "no parseable JSON" in result.errors[0]


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


# --- opencode: captured ndjson envelope (the "error" event only) ----------


def test_opencode_error_ndjson_fixture_unwraps_the_real_error_message():
    adapter = _registry()["opencode"]
    raw = (FIXTURES / "opencode_error.ndjson").read_text(encoding="utf-8")
    unwrapped = normalize.unwrap_envelope(raw, adapter.envelope)
    assert unwrapped == ("CLOUDFLARE_GATEWAY_ID missing. Set with: "
                         "export CLOUDFLARE_GATEWAY_ID=<value>")
    result = _normalize_fixture("opencode", "opencode_error.ndjson")
    assert result.succeeded is False
    # The unwrapped text is a plain error string, not JSON -- correctly a
    # "no parseable JSON" failure, not a confusing scan across the whole
    # NDJSON stream (which would otherwise pick up some other event object
    # as a false candidate; see the step_finish-only fixture below for that
    # scan instead firing on the fallback path, which is a REAL success for
    # the mechanism doing its job on the shape we do know).
    assert "no parseable JSON" in result.errors[0]


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


# --- claude / codex: no envelope declared; bare-object case unaffected ----


def test_claude_bare_object_fixture_still_normalizes_successfully():
    """UNVERIFIED fixture (see this module's docstring): claude's real
    envelope shape is unknown, so no [envelope] is declared, so this must
    hit the exact same bare-object scan path as before envelopes existed.
    structured_output=True is set for claude, but the enrichment only fires
    when there's no findings key at all -- this payload has one and
    validates cleanly, so it must succeed unchanged."""
    result = _normalize_fixture("claude", "claude_stdout_bare_UNVERIFIED.json")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "missing input validation"


def test_codex_bare_object_fixture_still_normalizes_successfully():
    result = _normalize_fixture("codex", "codex_stdout_bare_UNVERIFIED.json")
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "log line leaks a token"

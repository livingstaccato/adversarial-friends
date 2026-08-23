"""Turn whatever a friend printed into a validated payload, or fail honestly.

Repair here is a pure transformation. Re-prompting a friend to fix its own
malformed output cannot work when rounds are stateless: the "repair prompt"
reaches a brand new process that never emitted the broken output, so it simply
redoes the entire critique at full cost and produces different claims.

Repair is limited to: stripping terminal control codes, pulling JSON out of a
fenced block or surrounding prose, balancing braces, and dropping trailing
commas. Nothing else. Friend output is untrusted text, so every function here
must return a result describing failure rather than raise.

Envelopes: several CLIs wrap the model's actual answer in structured output of
their own (`claude --print --output-format json`, `codex exec --json`,
`opencode run --format json`, `agy --output-format json`). The wrapping is
itself well-formed JSON, so the *inner* answer -- which is what actually
carries the findings -- sits inside a quoted string value. `_iter_balanced_objects`
correctly treats string content as opaque and never descends into it, which
means a friend's real findings object is structurally unreachable by the plain
brace-scan below unless it is unwrapped first. `Envelope`/`unwrap_envelope`
below are a small, declarative description of "where the answer lives" that
`normalize()` applies before falling back to the scan that already existed --
see `normalize()`'s docstring for the exact fallback order.
"""
import json
import re
from dataclasses import dataclass
from typing import Iterator

from .claimschema import is_successful_payload, validate_payload

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
FENCE_RE = re.compile(r"```[ \t]*[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", re.DOTALL)

# A run of one-or-more ",<whitespace>" immediately before a closing bracket
# collapses in a single substitution pass -- the lookahead means the run
# isn't consumed until it's confirmed trailing, so "[1,,,]" and
# '{"a":1, , }' are both fixed without looping. (An earlier version of this
# looped a simpler regex to a fixed point; that was O(n) per pass and O(k)
# passes for k trailing commas, which is quadratic on a long run of commas --
# exactly what a repetition-looping local model can emit. Verified linear
# with this pattern: see task-7-report.md timings.)
TRAILING_COMMA_RE = re.compile(r"(?:,\s*)+(?=[}\]])")

# json.loads recurses per nesting level; a maliciously (or accidentally) deep
# structure can blow the interpreter's recursion limit. Anything from a friend
# is untrusted text, so that must surface as a failed parse, not a crash.
_JSON_ERRORS = (json.JSONDecodeError, ValueError, RecursionError)


@dataclass(frozen=True)
class NormalizeResult:
    payload: dict | None
    errors: list[str]
    succeeded: bool


@dataclass(frozen=True)
class EnvelopeRule:
    """One NDJSON matching rule: when `match_field` on a parsed line equals
    `match_value`, extract `field` (a dotted path) as one segment of the
    unwrapped answer text."""
    match_value: str
    field: str


@dataclass(frozen=True)
class Envelope:
    """Declarative description of where a friend's real answer lives inside
    a CLI's structured-output wrapper. Two kinds, matching the two shapes
    verified against real CLI output (see the module docstring):

    - "json_path": the whole stdout is a single JSON object; `path` is a
      dotted key path to the string field holding the answer (agy: the
      top-level `response` field).
    - "ndjson": stdout is one JSON object per line (an event stream); each
      line's `match_field` (default "type") is compared against every rule
      in `rules`, and every match's `field` is extracted (opencode: an
      `"error"` event's `error.data.message`).

    There is deliberately no third kind for "I'm not sure" -- an adapter
    whose real envelope shape has not been captured (claude, codex) simply
    has no `envelope` at all; see normalize()'s `structured_output`
    parameter for how that case is still made legible without guessing a
    shape.
    """
    kind: str
    path: str = ""
    match_field: str = "type"
    rules: tuple[EnvelopeRule, ...] = ()


def parse_envelope(data: dict | None) -> "Envelope | None":
    """Build an Envelope from the `[envelope]` table of an adapter TOML, or
    return None if no (valid) envelope was declared. Never raises: a
    malformed or absent envelope section simply means "no envelope," which
    normalize() already treats as a safe, working fallback -- adapter config
    is trusted input, but there is no reason to make a typo here fatal when
    "don't unwrap" is always a safe degradation."""
    if not data:
        return None
    kind = data.get("kind")
    if kind == "json_path":
        path = data.get("path", "")
        if not path:
            return None
        return Envelope(kind="json_path", path=path)
    if kind == "ndjson":
        rules = tuple(
            EnvelopeRule(match_value=rule["type"], field=rule["field"])
            for rule in data.get("rules", [])
            if isinstance(rule, dict) and rule.get("type") and rule.get("field")
        )
        return Envelope(kind="ndjson", match_field=data.get("match_field", "type"),
                        rules=rules)
    return None


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _dotted_get(obj: object, path: str) -> object:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _unwrap_json_path(raw: str, envelope: Envelope) -> str | None:
    """The whole text must itself be exactly one JSON object (agy's stdout
    is not wrapped in prose or fencing -- it IS the envelope). Anything that
    fails to parse as a single top-level object, or whose target field is
    missing/empty, unwraps to nothing -- the caller falls back to scanning
    the raw text."""
    cleaned = strip_ansi(raw).strip()
    try:
        parsed = json.loads(cleaned)
    except _JSON_ERRORS:
        return None
    if not isinstance(parsed, dict):
        return None
    value = _dotted_get(parsed, envelope.path)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _unwrap_ndjson(raw: str, envelope: Envelope) -> str | None:
    """Scan every line as its own JSON object (an NDJSON event stream);
    every line whose `match_field` matches a rule contributes that rule's
    extracted field to the unwrapped text, in stream order. A line that
    fails to parse (blank, partial, non-JSON) is simply skipped -- one
    malformed event must not poison the rest of a real stream."""
    cleaned = strip_ansi(raw)
    extracted: list[str] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except _JSON_ERRORS:
            continue
        if not isinstance(parsed, dict):
            continue
        type_value = parsed.get(envelope.match_field)
        for rule in envelope.rules:
            if rule.match_value == type_value:
                value = _dotted_get(parsed, rule.field)
                if isinstance(value, str) and value.strip():
                    extracted.append(value)
    if not extracted:
        return None
    return "\n".join(extracted)


def unwrap_envelope(raw: str, envelope: Envelope) -> str | None:
    """Return the friend's answer text as described by `envelope`, or None
    if the envelope's own path/rules found nothing to extract -- "found
    nothing" is the caller's signal to fall back to scanning `raw` directly
    (see normalize())."""
    if envelope.kind == "json_path":
        return _unwrap_json_path(raw, envelope)
    if envelope.kind == "ndjson":
        return _unwrap_ndjson(raw, envelope)
    return None


def _iter_balanced_objects(text: str) -> Iterator[str]:
    """Yield every top-level ``{...}`` span in a single left-to-right pass.

    "Top-level" means depth returns to zero outside of any string. This is a
    single O(n) scan (not one restart per '{' found) so that adversarial input
    consisting of thousands of unmatched '{' characters can't turn extraction
    into an O(n^2) scan.

    Returning every span, not just the first, matters: a friend's prose can
    contain an earlier, incidental '{...}' (an example, a stray brace) before
    its real answer. Trying every candidate lets the caller prefer the one
    that actually matters, not just the one that appears first.
    """
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:index + 1]
                    start = None
    # A span still open when the text ends is unterminated JSON; it is simply
    # never yielded rather than being reported as a candidate.


def _candidate_sources(cleaned: str) -> Iterator[str]:
    """Fenced blocks first (a deliberate signal), then the whole text.

    Fencing also protects against a failure mode single-pass brace-depth
    tracking can't recover from on its own: a stray, unmatched '{' anywhere
    earlier in a friend's prose permanently perturbs depth for the rest of
    the document (it never returns to zero again), which can hide a
    perfectly well-formed object later in the SAME whole-text scan. Scanning
    each fenced block's content in isolation resets depth to zero at the
    fence boundary, so the real answer is still found even when the
    surrounding prose is brace-unbalanced.
    """
    for match in FENCE_RE.finditer(cleaned):
        yield match.group(1)
    yield cleaned


def _candidate_tier(parsed: dict, errors: list[str]) -> int:
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


def extract_json(text: str) -> dict | None:
    """Best-effort recovery of a single JSON object from untrusted text.

    Every candidate that parses as a JSON object is ranked by
    `_candidate_tier` (see its docstring for the ordering and why); the
    best-ranked candidate found across every source wins, not just the first
    one encountered. `validate_payload` is computed once per candidate and
    reused for tiering -- `normalize` runs it again on the single winning
    payload, but each candidate considered during the search is validated
    exactly once, not twice.
    """
    cleaned = strip_ansi(text).strip()
    best_tier = None
    best_payload = None
    for source in _candidate_sources(cleaned):
        pieces = [source]
        pieces.extend(_iter_balanced_objects(source))
        for piece in pieces:
            for attempt in (piece, TRAILING_COMMA_RE.sub("", piece)):
                try:
                    parsed = json.loads(attempt)
                except _JSON_ERRORS:
                    continue
                if not isinstance(parsed, dict):
                    continue
                errors = validate_payload(parsed)
                tier = _candidate_tier(parsed, errors)
                if best_tier is None or tier < best_tier:
                    best_tier, best_payload = tier, parsed
                    if tier == 0:
                        return best_payload
    return best_payload


_ENVELOPE_HINT = (
    "output was structured JSON but contained no findings; the adapter may "
    "need an envelope path"
)


def _normalize_text(raw: str, structured_output: bool) -> NormalizeResult:
    payload = extract_json(raw)
    if payload is None:
        return NormalizeResult(None, ["output contained no parseable JSON object"], False)
    errors = validate_payload(payload)
    if errors:
        if structured_output and "findings" not in payload:
            # The friend's CLI was explicitly asked for structured output
            # (e.g. --output-format json) but the JSON we found has no
            # findings/no_findings marker at all -- the likely cause is that
            # this whole payload IS the CLI's own wrapper object (the real
            # answer is nested inside one of its string fields, structurally
            # unreachable by a plain brace-scan), not a malformed critique.
            # Surfacing that distinction turns a mystery into something
            # actionable: declare an [envelope] for this adapter.
            errors = [*errors, _ENVELOPE_HINT]
        return NormalizeResult(payload, errors, False)
    if not is_successful_payload(payload):
        return NormalizeResult(
            payload,
            ["no findings and no explicit no_findings marker"],
            False,
        )
    return NormalizeResult(payload, [], True)


def normalize(raw: str, envelope: "Envelope | None" = None,
              structured_output: bool = False) -> NormalizeResult:
    """Turn `raw` stdout into a NormalizeResult.

    `envelope`, if given, is tried FIRST: `unwrap_envelope` extracts the
    friend's real answer text from `raw` per the envelope's declared shape,
    and the existing extraction/validation logic runs on THAT text instead.
    Two distinct ways this can fail to produce anything, both of which fall
    back to running the exact same logic on `raw` directly (unchanged from
    before envelopes existed at all):

    1. The envelope finds nothing to extract at all (a missing key, no
       matching NDJSON line -- see unwrap_envelope's docstring) --
       `unwrap_envelope` returns None.
    2. The envelope DID extract some text, but that text does not itself
       yield a successful result (e.g. an NDJSON stream whose first
       matching line is a benign "rate limited, retrying" notice under the
       declared `error` rule, while a separate, later line in that same
       stream is a perfectly good, schema-valid findings object; or an agy
       envelope whose `response` field holds unrelated prose while the
       envelope object ITSELF -- one level up -- also happens to carry a
       valid top-level `findings` array). Committing to the unwrapped text
       exclusively here would silently discard a raw scan that would have
       succeeded -- and report a status that is actively false ("no
       parseable JSON object" when the output plainly contained one).
       Retrying the full scan against the untouched `raw` text is what a
       currently-working case must never be made to fail by an envelope
       declaration means in practice: a currently-working case includes one
       where the envelope's rule matches something irrelevant, not only the
       case where it matches nothing.

    `structured_output` only affects an attempt that scans `raw` itself
    (case 1, case 2's retry, or no envelope at all): see `_normalize_text`
    for what it adds. It is intentionally NOT applied to the FIRST attempt,
    scanning the freshly unwrapped text -- at that point the friend's own
    answer text is what's being scanned, not the CLI's wrapper, so "the
    adapter may need an envelope path" would be a non sequitur there. It IS
    applied to the raw-text retry in case 2, on purpose: that retry scans
    the wrapper again, so the hint is exactly as relevant there as it would
    be with no envelope at all -- forcing it to False on that retry would
    suppress the one message that explains why the "obvious" unwrap didn't
    pan out.
    """
    if envelope is not None:
        unwrapped = unwrap_envelope(raw, envelope)
        if unwrapped is not None:
            unwrapped_result = _normalize_text(unwrapped, structured_output=False)
            if unwrapped_result.succeeded:
                return unwrapped_result
            # Case 2: the envelope matched something, but it wasn't the
            # answer. Retry against the untouched raw text -- it may still
            # contain the real findings elsewhere (a different NDJSON line,
            # or a key on the envelope object itself) -- and return that
            # attempt's result regardless of whether it succeeds: on
            # failure it is still the more informative diagnosis (it can
            # carry the structured_output hint; the unwrapped-only failure
            # never can, by design -- see above).
            return _normalize_text(raw, structured_output=structured_output)
    return _normalize_text(raw, structured_output=structured_output)

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

from collections.abc import Iterator
import dataclasses
from dataclasses import dataclass
import json
import re
from typing import Any

from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .envelopes import (
    Envelope,
    envelope_error,
    strip_ansi,
    unwrap_envelope,
)

FENCE_RE = re.compile(r"```[ \t]*[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", re.DOTALL)

_CLOSERS = "}]"

# json.loads recurses per nesting level; a maliciously (or accidentally) deep
# structure can blow the interpreter's recursion limit. Anything from a friend
# is untrusted text, so that must surface as a failed parse, not a crash.
_JSON_ERRORS = (json.JSONDecodeError, ValueError, RecursionError)


@dataclass(frozen=True)
class NormalizeResult:
    payload: dict[str, Any] | None
    errors: list[str]
    succeeded: bool


def drop_trailing_commas(text: str) -> str:
    """Remove comma runs that sit immediately before a closing bracket.

    A single left-to-right pass, for two reasons a regex could not satisfy
    at once. Both were found by cross-examining this module.

    **Linear.** The previous pattern, `(?:,\\s*)+(?=[}\\]])`, was documented
    as verified linear -- and is, when the run really is followed by a
    bracket. When it is not, every start position rescans the whole run:
    16k commas took 7.5 seconds against 0.3ms for the same count with a
    bracket. A repetition-looping local model emitting endless commas is the
    exact input the old comment cited, and `normalize()` runs after the
    process has already been killed, so that cost lands *past* the timeout
    that was supposed to bound it. Here a run is scanned once and then
    skipped whether or not it turned out to be trailing.

    **String-aware.** A flat regex cannot tell a structural comma from one
    inside a string literal, so `{"a": "x, }"}` was rewritten to
    `{"a": "x}"}` -- still valid JSON, silently different value. Repair is
    structural and has no business editing content.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            end = index
            while end < length and (text[end] == "," or text[end].isspace()):
                end += 1
            # Advance past the whole run either way -- dropping it when it
            # was trailing, keeping it verbatim when it was not. Never
            # re-examining it is what keeps this linear.
            if end < length and text[end] in _CLOSERS:
                index = end
            else:
                out.append(text[index:end])
                index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _with_reported_error(result: NormalizeResult, raw: str, envelope: Envelope) -> NormalizeResult:
    """A failed result, led by what the CLI itself said went wrong. Before
    this, agy's `{"status":"ERROR","response":"","error":"timeout waiting
    for response"}` was reported as "the adapter may need an envelope
    path" -- a diagnosis of the adapter, when the CLI had already given
    the diagnosis of itself."""
    if result.succeeded:
        return result
    reported = envelope_error(raw, envelope)
    if reported is None:
        return result
    return dataclasses.replace(
        result,
        errors=[f"the CLI reported an error in place of an answer: {reported!r}", *result.errors],
    )


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
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
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


def extract_json(
    text: str,
    contract: PayloadContract = CLAIM_CONTRACT,
    prefer_order: bool = False,
) -> dict[str, Any] | None:
    """Best-effort recovery of a single JSON object from untrusted text.

    Every candidate that parses as a JSON object is ranked by the contract's
    `tier` (see claimschema.claim_tier for the claim ordering and why); the
    best-ranked candidate found across every source wins, not just the first
    one encountered. Validation is computed once per candidate and reused for
    tiering -- `normalize` runs it again on the single winning payload, but
    each candidate considered during the search is validated exactly once,
    not twice.

    `prefer_order` inverts that for an ORDERED source: the first candidate
    that is successful under the contract wins outright, whatever its tier.
    Only an NDJSON stream sets it (see normalize()), because only there does
    a later event supersede an earlier one -- `_unwrap_ndjson` reverses its
    segments so "first" means "newest". Tier ranking alone could not express
    that: it is global and short-circuits on tier 0, so codex's schema-valid
    progress narration (tier 0, carries `findings`) beat a real
    `{"no_findings": true}` answer (tier 2) from ANY position, including
    when the real answer came first. Left off for a single document, where
    a stray well-formed marker must still lose to real findings.
    """
    cleaned = strip_ansi(text).strip()
    best_tier = None
    best_payload = None
    for source in _candidate_sources(cleaned):
        pieces = [source]
        pieces.extend(_iter_balanced_objects(source))
        for piece in pieces:
            for attempt in (piece, drop_trailing_commas(piece)):
                try:
                    parsed = json.loads(attempt)
                except _JSON_ERRORS:
                    continue
                if not isinstance(parsed, dict):
                    continue
                errors = contract.validate(parsed)
                if prefer_order and not errors and contract.is_successful(parsed):
                    return parsed
                tier = contract.tier(parsed, errors)
                if best_tier is None or tier < best_tier:
                    best_tier, best_payload = tier, parsed
                    if tier == 0:
                        return best_payload
    return best_payload


def _envelope_hint(contract: PayloadContract) -> str:
    return (
        f"output was structured JSON but contained no {contract.container_key}; "
        "the adapter may need an envelope path"
    )


def _normalize_text(
    raw: str,
    structured_output: bool,
    contract: PayloadContract,
    prefer_order: bool = False,
) -> NormalizeResult:
    payload = extract_json(raw, contract, prefer_order=prefer_order)
    if payload is None:
        return NormalizeResult(None, ["output contained no parseable JSON object"], False)
    errors = contract.validate(payload)
    if errors:
        if structured_output and contract.container_key not in payload:
            # The friend's CLI was explicitly asked for structured output
            # (e.g. --output-format json) but the JSON we found has no
            # findings/no_findings marker at all -- the likely cause is that
            # this whole payload IS the CLI's own wrapper object (the real
            # answer is nested inside one of its string fields, structurally
            # unreachable by a plain brace-scan), not a malformed critique.
            # Surfacing that distinction turns a mystery into something
            # actionable: declare an [envelope] for this adapter.
            errors = [*errors, _envelope_hint(contract)]
        return NormalizeResult(payload, errors, False)
    if not contract.is_successful(payload):
        return NormalizeResult(
            payload,
            [contract.empty_message],
            False,
        )
    return NormalizeResult(payload, [], True)


def normalize(
    raw: str,
    envelope: "Envelope | None" = None,
    structured_output: bool = False,
    contract: PayloadContract = CLAIM_CONTRACT,
) -> NormalizeResult:
    """Turn `raw` stdout into a NormalizeResult.

    `contract` selects which payload kind is being read -- claims from a
    critique round, or verdicts from a cross-examination round. Everything
    else here (envelope unwrapping, the candidate scan, repair, the
    fallback order below) is identical for both and deliberately shared:
    that machinery is where the hard-won handling of real agent output
    lives, and a second copy of it would rot.

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
            # An NDJSON stream is ordered and `_unwrap_ndjson` already put
            # the newest segment first, so the newest complete answer wins
            # over anything earlier -- see extract_json's `prefer_order`.
            unwrapped_result = _normalize_text(
                unwrapped, False, contract, prefer_order=envelope.kind == "ndjson"
            )
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
            return _with_reported_error(
                _normalize_text(raw, structured_output, contract), raw, envelope
            )
        return _with_reported_error(
            _normalize_text(raw, structured_output, contract), raw, envelope
        )
    return _normalize_text(raw, structured_output, contract)

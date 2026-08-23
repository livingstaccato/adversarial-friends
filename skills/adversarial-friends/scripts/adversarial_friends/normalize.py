"""Turn whatever a friend printed into a validated payload, or fail honestly.

Repair here is a pure transformation. Re-prompting a friend to fix its own
malformed output cannot work when rounds are stateless: the "repair prompt"
reaches a brand new process that never emitted the broken output, so it simply
redoes the entire critique at full cost and produces different claims.

Repair is limited to: stripping terminal control codes, pulling JSON out of a
fenced block or surrounding prose, balancing braces, and dropping trailing
commas. Nothing else. Friend output is untrusted text, so every function here
must return a result describing failure rather than raise.
"""
import json
import re
from dataclasses import dataclass
from typing import Iterator

from .claimschema import is_successful_payload, validate_payload

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
FENCE_RE = re.compile(r"```[ \t]*[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", re.DOTALL)
TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# json.loads recurses per nesting level; a maliciously (or accidentally) deep
# structure can blow the interpreter's recursion limit. Anything from a friend
# is untrusted text, so that must surface as a failed parse, not a crash.
_JSON_ERRORS = (json.JSONDecodeError, ValueError, RecursionError)


@dataclass(frozen=True)
class NormalizeResult:
    payload: dict | None
    errors: list[str]
    succeeded: bool


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _drop_trailing_commas(text: str) -> str:
    """Remove ",}" / ",]" patterns, including ones a prior pass exposes.

    A single substitution pass fixes every *non-overlapping* trailing comma
    (nesting depth doesn't matter to a textual find-and-replace). It misses
    only the pathological case of a doubled comma like ",,}", where removing
    the first offender exposes a second one right behind it. The loop is
    bounded by the number of trailing commas actually present, so it always
    terminates.
    """
    while True:
        fixed = TRAILING_COMMA_RE.sub(r"\1", text)
        if fixed == text:
            return fixed
        text = fixed


def _iter_balanced_objects(text: str) -> Iterator[str]:
    """Yield every top-level ``{...}`` span in a single left-to-right pass.

    "Top-level" means depth returns to zero outside of any string. This is a
    single O(n) scan (not one restart per '{' found) so that adversarial input
    consisting of thousands of unmatched '{' characters can't turn extraction
    into an O(n^2) scan.

    Returning every span, not just the first, matters: a friend's prose can
    contain an earlier, incidental '{...}' (an example, a stray brace) before
    its real answer. Trying every candidate lets the caller prefer the one
    that actually validates.
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
    """Fenced blocks first (a deliberate signal), then the whole text."""
    for match in FENCE_RE.finditer(cleaned):
        yield match.group(1)
    yield cleaned


def extract_json(text: str) -> dict | None:
    """Best-effort recovery of a single JSON object from untrusted text.

    Candidates are tried in order of how deliberate a signal they are: each
    fenced block's content (and every top-level object inside it), then the
    whole cleaned text (and every top-level object inside that). Among
    everything that parses as a JSON object, one that is schema-valid *and*
    a successful payload wins immediately; otherwise the first thing that
    merely parsed as a dict is returned, preserving "extraction found
    something, even if it doesn't validate" for the caller to report on.
    """
    cleaned = strip_ansi(text).strip()
    fallback = None
    for source in _candidate_sources(cleaned):
        pieces = [source]
        pieces.extend(_iter_balanced_objects(source))
        for piece in pieces:
            for attempt in (piece, _drop_trailing_commas(piece)):
                try:
                    parsed = json.loads(attempt)
                except _JSON_ERRORS:
                    continue
                if not isinstance(parsed, dict):
                    continue
                if fallback is None:
                    fallback = parsed
                if not validate_payload(parsed) and is_successful_payload(parsed):
                    return parsed
    return fallback


def normalize(raw: str) -> NormalizeResult:
    payload = extract_json(raw)
    if payload is None:
        return NormalizeResult(None, ["output contained no parseable JSON object"], False)
    errors = validate_payload(payload)
    if errors:
        return NormalizeResult(payload, errors, False)
    if not is_successful_payload(payload):
        return NormalizeResult(
            payload,
            ["no findings and no explicit no_findings marker"],
            False,
        )
    return NormalizeResult(payload, [], True)

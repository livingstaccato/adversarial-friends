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


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


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

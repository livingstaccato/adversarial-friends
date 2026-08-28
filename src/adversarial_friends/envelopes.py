"""Where a friend's real answer lives inside its CLI's output wrapper.

Several CLIs wrap the model's answer in structured output of their own
(`claude --print --output-format json`, `codex exec --json`, `opencode run
--format json`, `agy --output-format json`). That wrapping is itself
well-formed JSON, so the inner answer usually sits inside a quoted string
value -- and `normalize._iter_balanced_objects` treats string content as
opaque, never descending into it. A friend's findings object is therefore
structurally unreachable by a plain brace-scan unless it is unwrapped first.

`Envelope` is a small declarative description of that shape, applied by
`normalize()` before it falls back to scanning the raw text. Split out of
normalize.py, which had grown past this repo's 500-line file cap.
"""

from dataclasses import dataclass
import json
import re
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

_JSON_ERRORS = (json.JSONDecodeError, ValueError, RecursionError)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


@dataclass(frozen=True)
class EnvelopeRule:
    """One NDJSON matching rule: when `match_field` on a parsed line equals
    `match_value`, extract `field` (a dotted path) as one segment of the
    unwrapped answer text.

    `where`/`equals` add an optional SECOND condition, a dotted path on the
    same line and the value it must hold. codex needed it: its answer is
    "the item.completed event whose item.type is agent_message", and a rule
    that could only express the first half matched every item.completed --
    reasoning, command execution and file changes included -- resting on an
    unstated assumption that no other item kind carries `item.text`. A rule
    that declares neither matches on `match_value` alone, as opencode's do.
    """

    match_value: str
    field: str
    where: str = ""
    equals: str = ""


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

    There is deliberately no third kind for "I'm not sure". An adapter whose
    real envelope shape has not been captured simply has no `envelope` at
    all; see normalize()'s `structured_output` parameter for how that case
    is made legible without guessing a shape.

    Four of the five shipped adapters declare one, captured from the real
    CLIs. ollama does not, and takes the no-envelope path on every run: it
    is the HTTP transport rather than a wrapping CLI, so there is no
    envelope to capture. That path is therefore live and exercised, not
    merely reserved for adapters nobody has run yet -- which is how this
    docstring described it while claiming all five.
    """

    kind: str
    path: str = ""
    match_field: str = "type"
    rules: tuple[EnvelopeRule, ...] = ()
    # json_path only: a dotted path to the CLI's own error message, for the
    # case where the envelope carries one INSTEAD of an answer (agy: a
    # top-level `error` string beside an empty `response`). Read only after
    # normalizing has failed, so it can never turn a working answer into a
    # failure; it makes the failure say what the CLI said.
    error_path: str = ""


def parse_envelope(data: dict[str, Any] | None) -> "Envelope | None":
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
        error_path = data.get("error_path", "")
        return Envelope(
            kind="json_path",
            path=path,
            error_path=error_path if isinstance(error_path, str) else "",
        )
    if kind == "ndjson":
        rules = tuple(
            EnvelopeRule(
                match_value=rule["type"],
                field=rule["field"],
                where=rule.get("where", "") if isinstance(rule.get("where"), str) else "",
                equals=rule.get("equals", "") if isinstance(rule.get("equals"), str) else "",
            )
            for rule in data.get("rules", [])
            if isinstance(rule, dict) and rule.get("type") and rule.get("field")
        )
        return Envelope(kind="ndjson", match_field=data.get("match_field", "type"), rules=rules)
    return None


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
    if isinstance(value, dict) and value:
        # The target is the answer itself, not a string holding it. claude
        # under --json-schema puts the validated object in
        # `structured_output` and a serialized copy in `result`; pointing
        # the envelope at the object rather than the copy means the thing
        # the CLI validated is the thing we parse. Re-serialized so the rest
        # of normalize() sees the same text it would have unwrapped.
        return json.dumps(value)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _unwrap_ndjson(raw: str, envelope: Envelope) -> str | None:
    """Scan every line as its own JSON object (an NDJSON event stream);
    every line whose `match_field` matches a rule contributes that rule's
    extracted field to the unwrapped text, latest first. A line that
    fails to parse (blank, partial, non-JSON) is simply skipped -- one
    malformed event must not poison the rest of a real stream.

    Latest first because in an event stream the final matching event is
    the answer and the earlier ones are progress. codex under
    `--output-schema` emits "I'm inspecting the repository..." as an
    `agent_message` that is itself a schema-valid findings object, then
    the real answer; extract_json keeps the first candidate that ranks
    best, so stream order handed it the progress line and lost the answer
    -- a high-severity finding, in the run that found this. Captured in
    tests/fixtures/codex_progress_then_findings.ndjson."""
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
            if rule.match_value != type_value:
                continue
            if rule.where and _dotted_get(parsed, rule.where) != rule.equals:
                continue
            value = _dotted_get(parsed, rule.field)
            if isinstance(value, str) and value.strip():
                extracted.append(value)
    if not extracted:
        return None
    return "\n".join(reversed(extracted))


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


def envelope_error(raw: str, envelope: Envelope) -> str | None:
    """The CLI's own error message, when its json_path envelope declares
    where one lives and `raw` carries a non-empty one. None otherwise --
    including for every ndjson envelope, whose error events are rules."""
    if envelope.kind != "json_path" or not envelope.error_path:
        return None
    try:
        parsed = json.loads(strip_ansi(raw).strip())
    except _JSON_ERRORS:
        return None
    if not isinstance(parsed, dict):
        return None
    value = _dotted_get(parsed, envelope.error_path)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def answer_is_complete(text: str, envelope: Envelope) -> bool:
    """Whether a `json_path` friend has already said everything it will say.

    Only for that envelope kind, and the restriction is the whole argument:
    its contract is that stdout IS one JSON object, so a parseable object
    means the answer is in hand. An NDJSON friend streams events and a later
    line carries the answer, so the same check there would truncate it.

    This exists because agy, on its error path, writes its JSON and then does
    not exit until its own `--print-timeout` elapses. Measured across three
    occurrences against eleven clean ones: every successful run exits about
    2.5 seconds after the work it reports, and every hang exits at 906
    seconds having reported 163, 372 and 482 -- between seven and twelve
    minutes of waiting for a process that had already answered.

    The cheap guard first: a complete object ends with `}`, so the parse is
    attempted only when the buffer looks finished rather than on every poll.
    """
    if envelope.kind != "json_path":
        return False
    text = text.strip()
    if not text.endswith("}"):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, RecursionError):
        return False

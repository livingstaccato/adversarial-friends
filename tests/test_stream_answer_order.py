"""An NDJSON stream's LAST answer wins, even when an earlier one outranks it.

`_unwrap_ndjson` reverses its extracted segments so the newest event is
scanned first, and its docstring credits that reversal with fixing the codex
progress-narration bug: codex emits "I'm inspecting the repository..." as an
`agent_message` that is itself a schema-valid findings object, then the real
answer.

Cross-examining normalize.py established that the reversal alone cannot do
that. `extract_json` ranks every candidate by contract tier and returns
immediately on tier 0, so order only breaks TIES. A progress message
carrying `findings` is tier 0; a real `{"no_findings": true}` answer is tier
2. The progress message therefore won from any position -- including when
the real answer came first.

Tier ordering is still right for a single document, where a stray
`no_findings` fragment must not beat real findings (see claimschema.
claim_tier). It is wrong for an event stream, where a later event supersedes
an earlier one. So position wins only where the source is ordered.
"""

import json

from afriend.envelopes import Envelope, EnvelopeRule
from afriend.normalize import normalize

FINDING = {
    "severity": "high",
    "claim": "c",
    "location": None,
    "evidence": "e",
    "failure_scenario": "f",
    "suggested_fix": "s",
}
CODEX = Envelope(
    kind="ndjson",
    rules=(EnvelopeRule(match_value="item.completed", field="item.text"),),
)


def _event(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"text": text}})


def test_a_later_no_findings_answer_beats_earlier_schema_shaped_narration():
    """The exact codex shape: narration that validates, then the real answer."""
    narration = json.dumps({"findings": [FINDING], "no_findings": None})
    real = json.dumps({"findings": None, "no_findings": True})
    stream = _event(narration) + "\n" + _event(real)
    result = normalize(stream, envelope=CODEX)
    assert result.succeeded
    assert result.payload == {"findings": None, "no_findings": True}


def test_a_later_findings_answer_still_wins_over_earlier_narration():
    """The mirror case, so the fix is not just "prefer no_findings"."""
    narration = json.dumps({"findings": [{**FINDING, "claim": "narration"}], "no_findings": None})
    real = json.dumps({"findings": [{**FINDING, "claim": "the real one"}], "no_findings": None})
    stream = _event(narration) + "\n" + _event(real)
    result = normalize(stream, envelope=CODEX)
    assert result.succeeded
    assert result.payload["findings"][0]["claim"] == "the real one"


def test_a_single_document_still_prefers_findings_over_a_stray_marker():
    """The tier rule this must not break: with NO envelope, the source is not
    an ordered stream, so a trivially-valid `no_findings` scrap appearing
    first must not beat real findings found later in the same text."""
    stray = json.dumps({"no_findings": True})
    real = json.dumps({"findings": [FINDING], "no_findings": None})
    result = normalize(f"{stray}\n\nand then, actually:\n\n{real}")
    assert result.succeeded
    assert result.payload["findings"][0]["claim"] == "c"

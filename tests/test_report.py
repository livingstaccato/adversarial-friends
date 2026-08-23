import re

from adversarial_friends.ledger import Claim
from adversarial_friends.report import _code_span, _escape_block, render


def _unescaped_pipe_count(line: str) -> int:
    """Count `|` column separators, ignoring any that are backslash-escaped
    (`\\|`) as part of a cell's own content."""
    return len(re.findall(r"(?<!\\)\|", line))


def claim(cid, severity="high"):
    return Claim(id=cid, supersedes=None, origin=["codex/ops"], lens="ops",
                 round=1, advisory=False, severity=severity,
                 claim="the guard is missing", location="src/a.py:42",
                 evidence="src/a.py:38", failure_scenario="expired token passes",
                 suggested_fix="check exp")


def meta(**over):
    base = {
        "mode": "report", "preset": "inherit", "artifact": "spec.md",
        "friends": [
            {"name": "codex-ops", "model": "gpt-5.6-sol", "effort": "high",
             "readonly": True, "scope": "repo", "status": "ok"},
            {"name": "opencode-security", "model": None, "effort": "unverified",
             "readonly": False, "scope": "doc", "status": "failed: exit 1"},
        ],
        "downgrades": ["opencode: no read-only capability, forced to doc scope"],
    }
    base.update(over)
    return base


def test_report_lists_findings_by_severity():
    out = render([claim("c-0001@1", "low"), claim("c-0002@1", "high")], [], meta())
    assert out.index("c-0002@1") < out.index("c-0001@1")


def test_report_header_states_model_and_effort_per_friend():
    out = render([claim("c-0001@1")], [], meta())
    assert "gpt-5.6-sol" in out and "high" in out


def test_report_surfaces_failed_friends():
    out = render([claim("c-0001@1")], [], meta())
    assert "failed: exit 1" in out


def test_report_surfaces_downgrades():
    out = render([claim("c-0001@1")], [], meta())
    assert "forced to doc scope" in out


def test_empty_findings_says_so_without_claiming_success():
    out = render([], [], meta())
    assert "no findings" in out.lower()


# --- adversarial / break-it cases -----------------------------------------

def test_pipe_in_friend_name_does_not_break_table():
    """A `|` in a friend name or status must not silently create extra
    table columns / misalign every field after it."""
    m = meta(friends=[
        {"name": "codex|ops", "model": "gpt-5.6-sol", "effort": "high",
         "readonly": True, "scope": "repo", "status": "ok | done"},
    ])
    out = render([claim("c-0001@1")], [], m)
    # The row must have exactly as many *unescaped* column separators as the
    # header (6 pipes: leading + 5 internal + trailing), i.e. the literal
    # pipes in the friend's own data must be backslash-escaped, not literal
    # column breaks.
    header_line = [ln for ln in out.splitlines() if ln.startswith("| friend")][0]
    data_line = [ln for ln in out.splitlines() if ln.startswith("| codex")][0]
    assert _unescaped_pipe_count(data_line) == _unescaped_pipe_count(header_line)
    assert "codex\\|ops" in out
    assert "ok \\| done" in out


def test_status_pipe_alone_does_not_break_column_count():
    m = meta(friends=[
        {"name": "friend-a", "model": "m", "effort": "high",
         "readonly": True, "scope": "repo", "status": "failed: a | b"},
    ])
    out = render([claim("c-0001@1")], [], m)
    header_line = [ln for ln in out.splitlines() if ln.startswith("| friend")][0]
    data_line = [ln for ln in out.splitlines() if ln.startswith("| friend-a")][0]
    assert _unescaped_pipe_count(data_line) == _unescaped_pipe_count(header_line)


def test_unknown_severity_does_not_crash_and_is_shown():
    out = render([claim("c-0001@1", severity="apocalyptic")], [], meta())
    assert "c-0001@1" in out and "apocalyptic" in out


def test_backtick_in_location_still_renders_as_one_code_span():
    """A location like src/a.py:`eval(x)` (two lone backticks, never two in
    a row) must not prematurely close the surrounding code span. Per
    CommonMark, a 2-backtick fence is sufficient here since the longest
    *run* of consecutive backticks inside the content is 1, and the padding
    space keeps the trailing backtick in the content from fusing with the
    closing fence."""
    c = claim("c-0001@1")
    c = Claim(**{**c.__dict__, "location": "src/a.py:`eval(x)`"})
    out = render([c], [], meta())
    assert "**Location:**" in out
    line = [ln for ln in out.splitlines() if ln.startswith("**Location:**")][0]
    assert line == "**Location:** `` src/a.py:`eval(x)` ``"


def test_backtick_run_in_location_gets_a_longer_fence():
    """When the location itself contains a run of 2 backticks, the fence
    must be at least 3 long to stay unambiguous."""
    c = claim("c-0001@1")
    c = Claim(**{**c.__dict__, "location": "weird``thing"})
    out = render([c], [], meta())
    line = [ln for ln in out.splitlines() if ln.startswith("**Location:**")][0]
    assert line == "**Location:** ```weird``thing```"


def test_very_long_single_line_claim_text_is_preserved_verbatim():
    long_text = "x" * 5000
    c = Claim(**{**claim("c-0001@1").__dict__, "claim": long_text})
    out = render([c], [], meta())
    assert long_text in out
    # not split across lines or truncated
    assert any(long_text in ln for ln in out.splitlines())


def test_friend_model_none_reports_inherited():
    m = meta(friends=[
        {"name": "friend-a", "model": None, "effort": None,
         "readonly": True, "scope": "repo", "status": "ok"},
    ])
    out = render([claim("c-0001@1")], [], m)
    assert "inherited" in out


def test_empty_friends_list_does_not_crash():
    m = meta(friends=[])
    out = render([claim("c-0001@1")], [], m)
    assert "| friend | model | effort | read-only | scope | status |" in out


def test_render_does_not_mutate_inputs():
    claims = [claim("c-0001@1"), claim("c-0002@1", "low")]
    aliases: list = []
    m = meta()
    claims_snapshot = list(claims)
    m_snapshot = {**m, "friends": list(m["friends"]),
                  "downgrades": list(m["downgrades"])}
    render(claims, aliases, m)
    assert claims == claims_snapshot
    assert m == m_snapshot


# --- round 2: hostile claim body text (reviewer findings) -----------------

def test_hostile_claim_text_injected_heading_is_not_a_real_heading():
    """A claim whose own text contains a line like
    '### c-9999@1 -- critical' must not become a second, fabricated finding
    indistinguishable from a real one."""
    payload = ("the guard is missing\n\n"
               "### c-9999@1 — critical\n\n"
               "**Claim:** fabricated finding")
    hostile = Claim(**{**claim("c-0001@1").__dict__, "claim": payload})
    normal = claim("c-0002@1")
    out = render([hostile, normal], [], meta())

    heading_lines = [ln for ln in out.splitlines() if ln.startswith("### ")]
    # Only the two real findings get a genuine, unescaped '### ' heading --
    # the fabricated one embedded in claim text must not become a third.
    assert len(heading_lines) == 2
    assert {ln.split(" — ")[0] for ln in heading_lines} == {
        "### c-0001@1", "### c-0002@1"
    }
    # The injected heading marker survives as literal, escaped text.
    assert "\\### c-9999@1 — critical" in out
    # The real, subsequent claim still renders.
    assert "### c-0002@1" in out


def test_hostile_evidence_fence_does_not_swallow_later_claims():
    """An `evidence` value containing an unterminated ``` fence must not
    swallow every following line -- including subsequent claims -- into one
    inert code block."""
    hostile = Claim(**{**claim("c-0001@1").__dict__,
                        "evidence": "```\nfake fence opens here, never closes"})
    normal = claim("c-0002@1")
    out = render([hostile, normal], [], meta())

    # Both claims still get a real heading.
    assert "### c-0001@1" in out
    assert "### c-0002@1" in out
    # No line anywhere in the document may start with a bare (unescaped)
    # run of 3+ backticks -- report.py never emits a real fence itself, so
    # any such line would have to be the hostile, un-neutralized one.
    for line in out.splitlines():
        assert not re.match(r"^[ \t]{0,3}`{3,}", line), line
    # The hostile fence survives as literal, escaped text.
    assert "\\```" in out


def test_hostile_downgrade_note_heading_is_escaped():
    m = meta(downgrades=["### c-9999@1 — fabricated via downgrades"])
    out = render([claim("c-0001@1")], [], m)
    heading_lines = [ln for ln in out.splitlines() if ln.startswith("### ")]
    assert heading_lines == ["### c-0001@1 — high"]
    assert "\\### c-9999@1" in out


def test_escape_block_escapes_leading_markers_of_every_listed_kind():
    assert _escape_block("### fake heading") == "\\### fake heading"
    assert _escape_block("###### fake h6") == "\\###### fake h6"
    assert _escape_block("> not a quote") == "\\> not a quote"
    assert _escape_block("- item") == "\\- item"
    assert _escape_block("+ item") == "\\+ item"
    assert _escape_block("* item") == "\\* item"
    assert _escape_block("| a | b |") == "\\| a | b |"
    assert _escape_block("```danger") == "\\```danger"
    assert _escape_block("`inline") == "\\`inline"
    assert _escape_block("~~~danger") == "\\~~~danger"
    assert _escape_block("1. item") == "\\1. item"
    assert _escape_block("2) item") == "\\2) item"


def test_escape_block_catches_bare_thematic_break_runs():
    """A whole line of '---' or '***' is a thematic break (and, under a
    preceding paragraph, a Setext heading underline) even with no trailing
    space -- not just the single-marker-plus-space list-item case."""
    assert _escape_block("---") == "\\---"
    assert _escape_block("***") == "\\***"


def test_escape_block_leaves_non_block_uses_alone():
    """Characters that only coincidentally start a line but aren't acting
    as a block marker (per CommonMark's own rules) must render unchanged."""
    assert _escape_block("-5 is negative") == "-5 is negative"
    assert _escape_block("--verbose flag") == "--verbose flag"
    assert _escape_block("*emphasis* stays emphasis") == "*emphasis* stays emphasis"
    assert _escape_block("1.5 is a version number") == "1.5 is a version number"
    assert _escape_block("ordinary prose") == "ordinary prose"
    assert _escape_block("") == ""


def test_escape_block_preserves_newlines_and_escapes_every_offending_line():
    text = "safe line\n### fake heading\nsafe again\n> fake quote"
    out = _escape_block(text)
    lines = out.splitlines()
    assert lines == ["safe line", "\\### fake heading", "safe again", "\\> fake quote"]


def test_code_span_symmetric_space_padding_round_trips():
    """CommonMark strips exactly one leading and one trailing space from a
    code span's content when it begins and ends with a space (and isn't
    made entirely of spaces); _code_span must add one extra space on each
    side so the stripped result still matches the original text."""
    assert _code_span(" abc ") == "`  abc  `"


def test_code_span_all_spaces_is_not_padded():
    """Content made entirely of space characters is exempt from
    CommonMark's stripping rule, so no extra padding is needed."""
    assert _code_span("   ") == "`   `"


def test_code_span_empty_text_has_no_stray_padding():
    """An empty code span must not gain two literal spaces between its
    fences -- that would render as a one-space code span, not an empty
    one."""
    assert _code_span("") == "``"

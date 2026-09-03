import re
import shutil
import subprocess

import pytest
from report_helpers import claim, meta

from adversarial_friends.ledger import Claim
from adversarial_friends.report import (
    _code_span,
    _escape_block,
    _escape_cell,
    render as render_review,
)
from adversarial_friends.reviewstate import ReviewState

CMARK = shutil.which("cmark")


def render(claims, aliases, run_meta):
    """Keep the focused renderer tests terse while exercising its state API."""
    return render_review(ReviewState.replay([*claims, *aliases]), run_meta)


def _render_with_cmark(markdown_text: str) -> str:
    result = subprocess.run(
        ["cmark"], input=markdown_text, capture_output=True, text=True, check=True
    )
    return result.stdout


def _unescaped_pipe_count(line: str) -> int:
    """Count `|` column separators, ignoring any that are backslash-escaped
    (`\\|`) as part of a cell's own content."""
    return len(re.findall(r"(?<!\\)\|", line))


def test_report_lists_findings_by_severity():
    out = render([claim("c-0001@1", "low"), claim("c-0002@1", "high")], [], meta())
    assert out.index("c-0002@1") < out.index("c-0001@1")


def test_report_accepts_one_replayed_review_state():
    review = ReviewState.replay([claim("c-0001@1")])

    out = render_review(review, meta())

    assert "c-0001@1" in out


def test_report_header_states_model_and_effort_per_friend():
    out = render([claim("c-0001@1")], [], meta())
    assert "gpt-5.6-sol" in out and "high" in out


def test_report_header_records_profile_and_marks_legacy_metadata_unknown():
    profiled = render([claim("c-0001@1")], [], meta(profile="balanced"))
    legacy = render([claim("c-0001@1")], [], meta())

    assert "Mode: `report` · profile: `balanced` · preset: `inherit`" in profiled
    assert "profile: `legacy-unknown`" in legacy


def test_report_labels_host_self_review_as_advisory_and_non_independent():
    out = render([claim("c-0001@1")], [], meta())
    host_row = next(line for line in out.splitlines() if line.startswith("| codex-ops |"))

    assert "host-self-review (advisory)" in host_row
    assert "False" in host_row
    assert "independent reviewer" in out


def test_cross_exam_report_explains_advisory_host_verdicts_do_not_settle():
    out = render_review(
        ReviewState.replay([claim("c-0001@1")]),
        meta(mode="crossexam", rounds_run=2),
        {"c-0001@1": "unproven"},
    )

    assert "host self-review verdicts" in out.lower()
    assert "excluded from settlement and quorum" in out.lower()


def test_artifact_filename_cannot_forge_markdown_sections():
    out = render(
        [],
        [],
        meta(artifact="spec.md\n\n## FORGED `x` <https://evil.example>\x1b"),
    )

    first_line = out.splitlines()[0]
    assert first_line.startswith("# Adversarial review — `")
    assert first_line.endswith("`")
    assert "## FORGED" not in out
    assert "https://evil.example" in first_line
    assert "\x1b" not in out


def test_report_surfaces_failed_friends():
    out = render([claim("c-0001@1")], [], meta())
    assert "failed: exit 1" in out


def test_report_explains_that_zero_answers_provide_no_artifact_conclusion():
    out = render(
        [],
        [],
        meta(
            friends=[
                {
                    "name": "codex-security",
                    "independent": True,
                    "model": None,
                    "effort": None,
                    "round": 1,
                    "status": "failed: DNS temporary failure",
                }
            ]
        ),
    )

    assert "## Review completeness" in out
    assert "review incomplete: 0/1 friends answered; codex-security: DNS temporary failure" in out
    assert "no artifact conclusion follows from zero friend answers" in out.lower()
    assert out.index("## Review completeness") < out.index("## Friends")


def test_report_surfaces_downgrades():
    out = render([claim("c-0001@1")], [], meta())
    assert "forced to doc scope" in out


def test_empty_findings_says_so_without_claiming_success():
    out = render([], [], meta())
    assert "no findings" in out.lower()


def test_terminal_report_reads_the_persisted_outcome_without_redeciding_it():
    out = render(
        [],
        [],
        meta(
            lifecycle_state="terminal",
            stop_reason="max-loop-iterations",
            exit_code=11,
            converged=False,
            ceiling_hit="max-loop-iterations",
            started_at="2026-08-31T10:00:00Z",
            finished_at="2026-08-31T10:00:02Z",
            duration_s=2.0,
        ),
    )
    assert "Stop reason: `max-loop-iterations`" in out
    assert "Exit code: `11`" in out
    assert "Converged: `False`" in out


def test_terminal_gate_report_names_persisted_decision_and_ordered_blockers():
    out = render(
        [],
        [],
        meta(
            mode="gate",
            lifecycle_state="terminal",
            stop_reason="gate-blocked",
            exit_code=1,
            converged=True,
            gate_decision="blocked",
            gate_blocking_claims=["c-0002@1", "c-0001@1"],
        ),
    )
    assert "## Gate decision" in out
    assert "Decision: `blocked`" in out
    assert out.index("c-0002@1") < out.index("c-0001@1")
    gate = out.split("## Gate decision", 1)[1].split("## Friends", 1)[0]
    assert "Stop reason: `gate-blocked`" in gate


def test_terminal_clear_gate_report_explicitly_names_empty_blockers():
    out = render(
        [],
        [],
        meta(
            mode="gate",
            lifecycle_state="terminal",
            stop_reason="completed",
            exit_code=0,
            converged=True,
            gate_decision="clear",
            gate_blocking_claims=[],
        ),
    )
    assert "Decision: `clear`" in out
    assert "Blocking claims: _(none)_" in out


def test_advisory_host_failure_does_not_make_gate_evidence_partial():
    friends = [
        {**meta()["friends"][0], "status": "failed: exit 1"},
        {**meta()["friends"][1], "status": "ok"},
    ]
    out = render(
        [],
        [],
        meta(
            mode="gate",
            lifecycle_state="terminal",
            stop_reason="completed",
            exit_code=0,
            converged=True,
            gate_decision="clear",
            gate_blocking_claims=[],
            friends=friends,
        ),
    )

    gate = out.split("## Gate decision", 1)[1].split("## Friends", 1)[0]
    assert "Evidence caveat: _(none)_" in gate


def test_gate_report_names_ceiling_and_partial_evidence_caveat():
    out = render(
        [],
        [],
        meta(
            mode="gate",
            lifecycle_state="terminal",
            stop_reason="max-calls",
            exit_code=11,
            converged=False,
            gate_decision="blocked",
            gate_blocking_claims=["c-0002@1"],
            ceiling_hit="max-calls",
            incomplete=True,
        ),
    )

    gate = out.split("## Gate decision", 1)[1].split("## Friends", 1)[0]
    assert "max-calls" in gate
    assert "partial evidence" in gate.lower()


def test_read_exposed_names_are_stably_deduplicated():
    repeated = {
        "name": "claude-security",
        "model": None,
        "effort": None,
        "transport": "exec",
        "write_protected": True,
        "declared_scope": "repo",
        "os_confined": False,
        "status": "ok",
    }
    out = render([], [], meta(friends=[dict(repeated, round=1), dict(repeated, round=2)]))
    sentence = next(line for line in out.splitlines() if line.startswith("**Filesystem"))
    assert sentence.count("claude-security") == 1


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("deny", "denied"),
        ("scoped-allow", "scoped-allow"),
        ("allow", "explicitly-allowed"),
        (None, "legacy-unknown"),
    ],
)
def test_external_tool_authority_is_distinct_from_filesystem_confinement(policy, expected):
    values = {} if policy is None else {"external_tool_policy": policy}
    out = render([], [], meta(**values))
    section = out.split("## External tool authority", 1)[1].split("## Friends", 1)[0]
    assert f"Status: `{expected}`" in section
    assert "filesystem" not in section.lower()


def test_halt_report_names_the_nonterminal_waiting_state():
    out = render([], [], meta(lifecycle_state="waiting-for-orchestrator"))
    assert "Run state: `waiting-for-orchestrator`" in out
    assert "Exit code:" not in out


def test_theme_proposals_render_as_advisory_without_claiming_a_merge():
    out = render(
        [claim("c-0001@1"), claim("c-0002@1")],
        [],
        meta(
            theme_proposals=[
                {
                    "canonical": "c-0001@1",
                    "duplicate": "c-0002@1",
                    "score": 0.9375,
                    "anchor": "src/a.py:42",
                }
            ]
        ),
    )

    section = out.split("## Possible semantic duplicates", 1)[1].split("## Findings", 1)[0]
    assert "advisory only" in section.lower()
    assert "c-0001@1" in section and "c-0002@1" in section
    assert "merged" not in section.lower()


# --- adversarial / break-it cases -----------------------------------------


def test_pipe_in_friend_name_does_not_break_table():
    """A `|` in a friend name or status must not silently create extra
    table columns / misalign every field after it."""
    m = meta(
        friends=[
            {
                "name": "codex|ops",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "readonly": True,
                "scope": "repo",
                "status": "ok | done",
            },
        ]
    )
    out = render([claim("c-0001@1")], [], m)
    # The row must have exactly as many *unescaped* column separators as the
    # header (6 pipes: leading + 5 internal + trailing), i.e. the literal
    # pipes in the friend's own data must be backslash-escaped, not literal
    # column breaks.
    header_line = next(ln for ln in out.splitlines() if ln.startswith("| friend"))
    data_line = next(ln for ln in out.splitlines() if ln.startswith("| codex"))
    assert _unescaped_pipe_count(data_line) == _unescaped_pipe_count(header_line)
    assert "codex\\|ops" in out
    assert "ok \\| done" in out


def test_status_pipe_alone_does_not_break_column_count():
    m = meta(
        friends=[
            {
                "name": "friend-a",
                "model": "m",
                "effort": "high",
                "readonly": True,
                "scope": "repo",
                "status": "failed: a | b",
            },
        ]
    )
    out = render([claim("c-0001@1")], [], m)
    header_line = next(ln for ln in out.splitlines() if ln.startswith("| friend"))
    data_line = next(ln for ln in out.splitlines() if ln.startswith("| friend-a"))
    assert _unescaped_pipe_count(data_line) == _unescaped_pipe_count(header_line)


def test_hostile_friend_status_cannot_create_active_links_or_inline_code():
    m = meta(
        friends=[
            {
                "name": "friend-a",
                "model": None,
                "effort": None,
                "round": 1,
                "status": (
                    "failed: \x1b[31m`code`\x1b[0m bad\bstatus [click](javascript:alert(1)) "
                    "https://bad.test/ www.bad.test/path"
                ),
            }
        ]
    )

    out = render([], [], m)

    friend_line = next(ln for ln in out.splitlines() if ln.startswith("| friend-a"))
    assert "javascript:" not in friend_line
    assert "https://" not in friend_line
    assert "www.bad.test" not in friend_line
    assert "`code`" not in friend_line
    assert "\x1b" not in friend_line and "\b" not in friend_line


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
    line = next(ln for ln in out.splitlines() if ln.startswith("**Location:**"))
    assert line == "**Location:** `` src/a.py:`eval(x)` ``"


def test_backtick_run_in_location_gets_a_longer_fence():
    """When the location itself contains a run of 2 backticks, the fence
    must be at least 3 long to stay unambiguous."""
    c = claim("c-0001@1")
    c = Claim(**{**c.__dict__, "location": "weird``thing"})
    out = render([c], [], meta())
    line = next(ln for ln in out.splitlines() if ln.startswith("**Location:**"))
    assert line == "**Location:** ```weird``thing```"


def test_very_long_single_line_claim_text_is_preserved_verbatim():
    long_text = "x" * 5000
    c = Claim(**{**claim("c-0001@1").__dict__, "claim": long_text})
    out = render([c], [], meta())
    assert long_text in out
    # not split across lines or truncated
    assert any(long_text in ln for ln in out.splitlines())


def test_friend_model_none_reports_inherited():
    m = meta(
        friends=[
            {
                "name": "friend-a",
                "model": None,
                "effort": None,
                "readonly": True,
                "scope": "repo",
                "status": "ok",
            },
        ]
    )
    out = render([claim("c-0001@1")], [], m)
    assert "inherited" in out


def test_empty_friends_list_does_not_crash():
    m = meta(friends=[])
    out = render([claim("c-0001@1")], [], m)
    assert "| friend | role | independent | model | effort | transport | write-protected |" in out


def test_render_does_not_mutate_inputs():
    claims = [claim("c-0001@1"), claim("c-0002@1", "low")]
    aliases: list = []
    m = meta()
    claims_snapshot = list(claims)
    m_snapshot = {**m, "friends": list(m["friends"]), "downgrades": list(m["downgrades"])}
    render(claims, aliases, m)
    assert claims == claims_snapshot
    assert m == m_snapshot


# --- round 2: hostile claim body text (reviewer findings) -----------------


def test_hostile_claim_text_injected_heading_is_not_a_real_heading():
    """A claim whose own text contains a line like
    '### c-9999@1 -- critical' must not become a second, fabricated finding
    indistinguishable from a real one."""
    payload = "the guard is missing\n\n### c-9999@1 — critical\n\n**Claim:** fabricated finding"
    hostile = Claim(**{**claim("c-0001@1").__dict__, "claim": payload})
    normal = claim("c-0002@1")
    out = render([hostile, normal], [], meta())

    heading_lines = [ln for ln in out.splitlines() if ln.startswith("### ")]
    # Only the two real findings get a genuine, unescaped '### ' heading --
    # the fabricated one embedded in claim text must not become a third.
    assert len(heading_lines) == 2
    assert {ln.split(" — ")[0] for ln in heading_lines} == {"### c-0001@1", "### c-0002@1"}
    # The injected heading marker survives as literal, escaped text.
    assert "\\### c-9999@1 — critical" in out
    # The real, subsequent claim still renders.
    assert "### c-0002@1" in out


def test_hostile_evidence_fence_does_not_swallow_later_claims():
    """An `evidence` value containing an unterminated ``` fence must not
    swallow every following line -- including subsequent claims -- into one
    inert code block."""
    hostile = Claim(
        **{**claim("c-0001@1").__dict__, "evidence": "```\nfake fence opens here, never closes"}
    )
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


def test_every_untrusted_report_field_strips_terminal_and_bidi_controls():
    hostile = "safe\x1b]8;;https://evil.test\x07LINK\x1b]8;;\x07\u202eflip\x00end"
    c = Claim(
        **{
            **claim("c-0001@1").__dict__,
            "claim": hostile + "\n### forged",
            "evidence": hostile,
            "failure_scenario": hostile,
            "suggested_fix": hostile,
            "location": hostile,
            "origin": [hostile],
        }
    )
    m = meta(
        friends=[
            {
                "name": hostile,
                "model": hostile,
                "effort": hostile,
                "readonly": True,
                "scope": hostile,
                "status": "ok " + hostile,
            }
        ],
        downgrades=[hostile + "\n### forged downgrade"],
    )
    out = render([c], [], m)

    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert "\u202e" not in out
    assert "https://evil.test" not in out
    assert "\n### forged" not in out
    assert "\\### forged" in out


def test_table_cell_escapes_relative_markdown_links_after_backslashes():
    assert _escape_cell("[local](../../evil)") == "\\[local\\](../../evil)"


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


# --- C2: HTML blocks and Setext underlines (whole-branch review) ----------
#
# _BLOCK_LEADER_RE previously enumerated ATX headings, blockquotes, list
# markers, table pipes, fences, and ordered-list markers, but omitted HTML
# block starts ("<", including "<!--") and the "=" Setext underline.
# Reproduced directly against real `cmark` (0.31.2, on the machine this fix
# was written on): a two-field claim shaped exactly like render()'s own
# output --
#
#   **Evidence:** some evidence
#   <!-- never closes
#
# -- with no blank line between them (render() never puts one between a
# claim's own fields) renders as `<!-- raw HTML omitted -->` under cmark:
# an unterminated HTML comment block that swallows every following line,
# including a second, unrelated claim's own heading and every field, until
# EOF -- 0 of the intended findings rendered, exit 0, every friend "ok".
# The same shape with "===" instead forges an <h1> out of the preceding
# "**Evidence:** ..." line (a Setext heading, which -- unlike a thematic
# break "---"/"***" -- needs no leading marker of its own on the line it
# converts).


def _two_line_evidence_claim(cid, second_line):
    """A claim whose evidence is two lines: an innocuous first line, then
    `second_line` as its own bare line -- the exact shape render() produces
    for any multi-line field (see report.py's `_escape_block` docstring),
    and the shape needed to reproduce the bug: a hostile marker only
    matters at the START of a raw markdown line, and the first line of a
    field is never bare (it always follows "**Field:** " on the same
    source line)."""
    return Claim(**{**claim(cid).__dict__, "evidence": f"some evidence\n{second_line}"})


def test_escape_block_escapes_html_block_start():
    assert _escape_block("<div>never closes") == "\\<div>never closes"
    assert _escape_block("<!-- never closes") == "\\<!-- never closes"
    assert _escape_block("<script>alert(1)</script>") == "\\<script>alert(1)</script>"


def test_escape_block_escapes_setext_underline():
    assert _escape_block("===") == "\\==="
    assert _escape_block("====") == "\\===="
    assert _escape_block("=== ") == "\\=== "  # trailing whitespace is still valid Setext


def test_escape_block_leaves_non_setext_equals_uses_alone():
    """ "=" only means Setext when the ENTIRE rest of the line is blank --
    "=foo" (content after the run) has no other meaning to preserve and
    must render unchanged, same as the existing "-5 is negative" case."""
    assert _escape_block("=foo") == "=foo"
    assert _escape_block("x = 1") == "x = 1"


def test_hostile_html_comment_evidence_does_not_swallow_the_next_claim():
    """The exact defect: an unterminated HTML comment must not make a
    second, unrelated claim vanish from the rendered source."""
    hostile = _two_line_evidence_claim("c-0001@1", "<!-- never closes")
    victim = claim("c-0002@1")
    out = render([hostile, victim], [], meta())
    assert "\\<!-- never closes" in out
    assert "### c-0002@1" in out
    victim_block = out[out.index("### c-0002@1") :]
    assert "**Claim:**" in victim_block and "**Evidence:**" in victim_block


def test_hostile_setext_underline_does_not_forge_a_heading():
    hostile = _two_line_evidence_claim("c-0001@1", "===")
    victim = claim("c-0002@1")
    out = render([hostile, victim], [], meta())
    assert "\\===" in out
    # No line anywhere in the document is a bare Setext underline -- report.py
    # never emits one itself, so any such line would have to be the
    # un-neutralized hostile one.
    for line in out.splitlines():
        assert not re.fullmatch(r"[ \t]{0,3}=+[ \t]*", line), line
    assert "### c-0002@1" in out


@pytest.mark.skipif(CMARK is None, reason="cmark not installed on this machine")
def test_hostile_html_comment_evidence_does_not_swallow_findings_under_cmark():
    """End-to-end proof against a real CommonMark renderer, not just an
    assertion on the escaped source: both claims must render as real
    headings, and neither disappears into `<!-- raw HTML omitted -->`."""
    hostile = _two_line_evidence_claim("c-0001@1", "<!-- never closes")
    victim = claim("c-0002@1")
    html = _render_with_cmark(render([hostile, victim], [], meta()))
    assert html.count("<h3") == 2
    assert "raw HTML omitted" not in html


@pytest.mark.skipif(CMARK is None, reason="cmark not installed on this machine")
def test_hostile_setext_underline_does_not_forge_a_heading_under_cmark():
    hostile = _two_line_evidence_claim("c-0001@1", "===")
    victim = claim("c-0002@1")
    html = _render_with_cmark(render([hostile, victim], [], meta()))
    assert html.count("<h3") == 2
    # Exactly the document's own top-level "# Adversarial review" title --
    # not a second, forged <h1> out of the hostile claim's evidence line.
    assert html.count("<h1") == 1


@pytest.mark.skipif(CMARK is None, reason="cmark not installed on this machine")
def test_hostile_div_evidence_does_not_swallow_the_next_field_under_cmark():
    hostile = _two_line_evidence_claim("c-0001@1", "<div>never closes")
    victim = claim("c-0002@1")
    html = _render_with_cmark(render([hostile, victim], [], meta()))
    assert html.count("<h3") == 2
    assert "raw HTML omitted" not in html


def test_hostile_claim_cannot_remove_another_claims_id_from_the_rendered_source():
    """General property, across every marker this fix adds: no matter what
    a hostile claim's own fields contain, every OTHER claim's id must still
    be present in the rendered output, attached to a real heading -- not
    merely present as leftover text swallowed inside a comment/HTML span."""
    for hostile_marker in (
        "<!-- never closes",
        "<div>never closes",
        "===",
        "### c-9999@1 — fabricated",
        "```\nunterminated fence",
    ):
        hostile = _two_line_evidence_claim("c-0001@1", hostile_marker)
        victim = claim("c-0002@1")
        out = render([hostile, victim], [], meta())
        heading_lines = [ln for ln in out.splitlines() if ln.startswith("### ")]
        assert any(ln.startswith("### c-0002@1") for ln in heading_lines), (
            f"victim's real heading was lost for hostile marker {hostile_marker!r}: {heading_lines}"
        )


# --- I2: corroboration -- rendering claim.origin per finding --------------


def test_report_renders_origin_for_a_finding():
    out = render([claim("c-0001@1")], [], meta())
    assert "**Raised by:** codex/ops" in out


def test_report_marks_corroboration_when_multiple_friends_raised_the_same_claim():
    corroborated = Claim(
        **{**claim("c-0001@1").__dict__, "origin": ["codex/ops", "agy/security", "opencode/scope"]}
    )
    out = render([corroborated], [], meta())
    assert "**Raised by:** codex/ops, agy/security, opencode/scope" in out
    assert "corroborated by 3 friends" in out


def test_report_does_not_claim_corroboration_for_a_single_origin():
    out = render([claim("c-0001@1")], [], meta())
    assert "corroborated" not in out

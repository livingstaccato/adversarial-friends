"""Render report.md.

The header states the model and effort each friend actually received.
Without that, a weak critique from a friend that silently ran at default
effort reads as a signal about the artifact when it is really a signal
about the flag matrix -- and a run where several friends failed would
otherwise read as a clean bill of health. Every friend is listed, including
failures, and the empty-findings case says explicitly that it is not the
same as "no problems found."

`render` is a pure function: it only reads its arguments and returns a
string. It never writes files, never mutates `claims`/`aliases`/`run_meta`,
and never calls anything external.

`claim`, `evidence`, `failure_scenario`, and `suggested_fix` are untrusted
prose straight from an adversarial friend's stdout, as are `downgrades`
notes. A line in any of those fields that happens to start with a Markdown
block construct -- an ATX heading (`#`), a fence (backtick run or `~~~`), a
blockquote (`>`), a list marker (`-`/`+`/`*`/`1.`), or a table pipe (`|`) --
is otherwise interpreted as real document structure once concatenated into
report.md. A claim whose text opens with `### c-9999@1 -- critical` renders
as a second, fabricated finding indistinguishable from a real one; a claim
whose evidence contains an unterminated code fence swallows every
subsequent line of the document -- every following claim -- into one inert
code block. `_escape_block` neutralizes exactly the leading marker on each
line so these fields still render as prose, without collapsing newlines or
wrapping the field in a code block of its own.
"""

from collections import Counter
import re
from typing import Any

from .ledger import Claim, Verdict
from .reviewstate import ReviewState
from .verdicts import CONTESTED, DEADLOCKED, INCOMPLETE, UNPROVEN

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# The states where a reader has to see the argument rather than a label.
# `deadlocked` is the one §7.2 names explicitly ("both sides quoted
# verbatim"), but the same applies to a claim still contested when the run
# stopped and to one no judge could verify: in all three the tool has
# declined to decide, and hiding the reasoning behind a one-word state would
# make that look like a decision.
_NEEDS_BOTH_SIDES = frozenset({DEADLOCKED, CONTESTED, UNPROVEN, INCOMPLETE})

# A line-initial (up to 3 spaces of indentation) Markdown block construct:
# ATX heading, blockquote, bullet/thematic-break marker, table pipe, a
# backtick run (fence or inline span), a tilde fence, an ordered-list
# marker, a Setext H1 underline, or an HTML block start. Bullet/ordered-list
# markers require a trailing space/EOL per CommonMark (so inline uses like
# "*emphasis*", "-5 is negative", or "1.5" are left alone), but a run of 2+
# of the same -/+/* character followed by space/EOL is matched too, since a
# bare line of "---" or "***" is a thematic break (or, under the previous
# line, a Setext heading underline) even with no space after it -- the
# single-marker-plus-space check alone would miss that.
#
# "=" runs: a line consisting of nothing but "=" characters (plus optional
# trailing whitespace) is a Setext H1 underline -- it turns the PRECEDING
# line into a heading, with no leading marker of its own on the underline
# line itself. Per CommonMark this only counts when the entire rest of the
# line is blank, hence the lookahead (unlike "---", "===" has no other
# meaning to preserve, e.g. as a horizontal rule, so this is Setext-only).
#
# "<": CommonMark opens an HTML block on a line-initial "<" (a tag, a
# processing instruction, a declaration, or a comment opener) with no
# "space after the marker" requirement at all -- unescaped, a raw HTML
# block reads straight through to `cmark` (or GitHub's renderer) untouched.
# This single alternative also covers "<!--": an HTML comment block that
# terminates at "-->", not at the next blank line or heading, so left
# unescaped it can swallow every following claim into one inert (and,
# worse, literally invisible -- comments don't render at all) span.
# Reproduced directly: an `evidence` field starting with "<!--" and never
# closing ate 3 of 3 findings under `cmark`.
_BLOCK_LEADER_RE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<marker>"
    r"#{1,6}(?=[ \t]|$)"
    r"|>"
    r"|[-+*]+(?=[ \t]|$)"
    r"|=+(?=[ \t]*$)"
    r"|\|"
    r"|`+"
    r"|~{3,}"
    r"|\d+[.)](?=[ \t]|$)"
    r"|<"
    r")"
)


def _escape_cell(value: object) -> str:
    """Make `value` safe to place inside a single GFM table cell.

    A table cell that contains an unescaped `|` splits into extra columns
    for a human reading the rendered file, silently misaligning every field
    after it; a literal newline breaks the row entirely. Friend name and
    status are free text supplied by adapters/roster config, not claim
    authors, but nothing here guarantees they are pipe-free, so every cell
    is escaped the same way regardless of which field it came from.
    """
    text = str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _code_span(text: str) -> str:
    """Wrap `text` in backticks so it still renders as inline code even if
    `text` itself contains backticks (e.g. a location like
    ``src/a.py:`eval(...)```), and so a viewer displays exactly `text` --
    including its edge whitespace, if any.

    Per CommonMark, a backtick-delimited code span's fence must be a run of
    backticks strictly longer than the longest backtick run inside the
    content, and if the content starts or ends with a backtick, one space
    of padding on that side keeps the delimiter from fusing with it.
    Separately, CommonMark also strips exactly one leading and one trailing
    space from a code span's content whenever that content both begins and
    ends with a space (and isn't made entirely of spaces) -- so content
    like " abc " needs one extra space of padding on each side to survive
    that stripping and still display as " abc ". The two padding reasons
    are independent but never conflict: whenever either applies, adding
    exactly one space on each side is enough to round-trip the original
    text, because CommonMark only ever removes one space per side.
    """
    if text == "":
        return "``"
    longest_run = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            longest_run = max(longest_run, current)
        else:
            current = 0
    fence = "`" * (longest_run + 1)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    backtick_edge = text.startswith("`") or text.endswith("`")
    space_bracketed = text.startswith(" ") and text.endswith(" ") and text.strip(" ") != ""
    if backtick_edge or space_bracketed:
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _escape_block(text: str) -> str:
    """Backslash-escape a Markdown block-level construct at the start of
    each line of `text`, leaving everything else -- including newlines --
    untouched.

    Only the leading marker character is escaped (e.g. "### heading"
    becomes "\\### heading"); the rest of the line is left as-is, since a
    single leading backslash is enough to stop a block parser from
    recognizing the construct at all. This keeps the field rendering as
    ordinary prose rather than being wrapped in a code block, which would
    misrepresent prose fields like `claim` and `suggested_fix`.
    """
    escaped_lines = []
    for line in text.split("\n"):
        match = _BLOCK_LEADER_RE.match(line)
        if match:
            marker = match.group("marker")
            start, end = match.span("marker")
            line = line[:start] + "\\" + marker[0] + marker[1:] + line[end:]
        escaped_lines.append(line)
    return "\n".join(escaped_lines)


def _render_verdict_sections(
    claims: list[Claim],
    verdicts: list[Verdict],
    states: dict[str, str],
    run_meta: dict[str, Any],
) -> list[str]:
    """The cross-examination half of the report: what each claim's state is,
    and -- for anything the judges could not settle -- what each side
    actually said.

    §7.2 requires deadlocks be "reported as deadlocks with both sides quoted
    verbatim, never resolved by majority or orchestrator preference". That is
    the whole reason this section exists rather than a state column alone: a
    reader has to be able to see the disagreement and decide, because nothing
    in this tool is entitled to decide it for them.
    """
    lines: list[str] = ["## Cross-examination", ""]
    if run_meta.get("ceiling_hit"):
        lines.append(
            f"**{_escape_block(str(run_meta['ceiling_hit']))}** — this run stopped at a "
            "ceiling. It has neither converged nor cleared anything; the states "
            "below are where it was interrupted."
        )
        lines.append("")
    lines.append(f"Rounds run: {run_meta.get('rounds_run', 1)}")
    if run_meta.get("incomplete"):
        lines.append("")
        lines.append(
            "A required friend failed during at least one round, so this run is "
            "**incomplete**: any claim below that looks settled was settled by a "
            "smaller judge set than the roster promised."
        )
    lines.append("")

    tally = Counter(states.values())
    lines.append("| state | claims |")
    lines.append("|---|---|")
    for state, count in sorted(tally.items()):
        lines.append(f"| {_escape_cell(state)} | {count} |")
    lines.append("")

    by_id = {claim.id: claim for claim in claims}
    unsettled = [cid for cid, state in states.items() if state in _NEEDS_BOTH_SIDES]
    if unsettled:
        lines.append("### Unsettled")
        lines.append("")
        lines.append(
            "Judges did not agree, or could not decide. Both sides are quoted as "
            "written — nothing here was resolved by majority."
        )
        lines.append("")
        for cid in sorted(unsettled):
            claim = by_id.get(cid)
            lines.append(f"#### {_code_span(cid)} — {_escape_cell(states[cid])}")
            lines.append("")
            if claim is not None:
                lines.append(f"**Claim:** {_escape_block(claim.claim)}")
                lines.append("")
            cast = [v for v in verdicts if v.claim_id == cid]
            if not cast:
                # The heading above promises both sides quoted verbatim.
                # Nothing is quoted here, so say why rather than leaving a
                # bare state under that promise.
                lines.append(
                    "*No verdict was cast on this claim: no friend on the roster "
                    "was independent of it, or none reported.*"
                )
                lines.append("")
            for verdict in cast:
                lines.append(
                    f"- **{_escape_cell(verdict.verdict)}** "
                    f"(confidence {_escape_cell(verdict.confidence)}, "
                    f"evidence {_escape_cell(verdict.evidence_assessment or 'not stated')}): "
                    f"{_escape_block(verdict.reasoning)}"
                )
                if verdict.counter_evidence:
                    lines.append(f"  - counter-evidence: {_escape_block(verdict.counter_evidence)}")
                if verdict.amended_claim:
                    lines.append(
                        f"  - proposed amendment: {_escape_block(verdict.amended_claim)}"
                    )
            lines.append("")

    if run_meta.get("amendment_notes"):
        lines.append("### Amendments")
        lines.append("")
        for note in run_meta["amendment_notes"]:
            lines.append(f"- {_escape_block(str(note))}")
        lines.append("")
    return lines


def render(
    review: ReviewState,
    run_meta: dict[str, Any],
    states: dict[str, str] | None = None,
) -> str:
    claims = review.claims
    aliases = review.aliases
    verdicts = review.verdicts
    lines: list[str] = [f"# Adversarial review — {run_meta['artifact']}", ""]
    lines.append(f"Mode: `{run_meta['mode']}` · preset: `{run_meta['preset']}`")
    lines.append("")
    lines.append("## Friends")
    lines.append("")
    lines.append("| friend | model | effort | read-only | scope | status |")
    lines.append("|---|---|---|---|---|---|")
    for friend in run_meta["friends"]:
        lines.append(
            f"| {_escape_cell(friend['name'])} | "
            f"{_escape_cell(friend['model'] or 'inherited')} | "
            f"{_escape_cell(friend['effort'] or 'inherited')} | "
            f"{_escape_cell(friend['readonly'])} | "
            f"{_escape_cell(friend['scope'])} | "
            f"{_escape_cell(friend['status'])} |"
        )
    if not run_meta["friends"]:
        lines.append("| _(no friends were spawned)_ |  |  |  |  |  |")
    lines.append("")

    if run_meta.get("downgrades"):
        lines.append("## Downgrades")
        lines.append("")
        for note in run_meta["downgrades"]:
            lines.append(f"- {_escape_block(note)}")
        lines.append("")

    if states is not None:
        lines.extend(_render_verdict_sections(claims, verdicts or [], states, run_meta))

    lines.append("## Findings")
    lines.append("")
    if not claims:
        lines.append(
            "No findings were returned. This is not the same as a clean bill of "
            "health — check the friend table above for failures."
        )
        return "\n".join(lines) + "\n"

    ordered = sorted(claims, key=lambda c: (SEVERITY_ORDER.get(c.severity, 3), c.id))
    for claim in ordered:
        flag = " *(advisory)*" if claim.advisory else ""
        # The state belongs in the heading, not only in the table above: a
        # reader scrolling the findings must not read a refuted claim as a
        # live defect.
        state = (states or {}).get(claim.id)
        badge = f" — {state}" if state else ""
        lines.append(f"### {claim.id} — {claim.severity}{flag}{badge}")
        lines.append("")
        lines.append(f"**Claim:** {_escape_block(claim.claim)}")
        if claim.location:
            lines.append(f"**Location:** {_code_span(claim.location)}")
        lines.append(f"**Evidence:** {_escape_block(claim.evidence)}")
        lines.append(f"**Failure scenario:** {_escape_block(claim.failure_scenario)}")
        lines.append(f"**Suggested fix:** {_escape_block(claim.suggested_fix)}")
        if claim.supersedes:
            lines.append(f"**Supersedes:** {_code_span(claim.supersedes)} *(amended by judges)*")
        if claim.origin:
            corroborated = (
                f" *(corroborated by {len(claim.origin)} friends)*" if len(claim.origin) > 1 else ""
            )
            lines.append(f"**Raised by:** {_escape_block(', '.join(claim.origin))}{corroborated}")
        lines.append("")

    if aliases:
        lines.append("## Merged duplicates")
        lines.append("")
        for alias in aliases:
            lines.append(
                f"- {_code_span(alias.duplicate)} merged into {_code_span(alias.canonical)}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"

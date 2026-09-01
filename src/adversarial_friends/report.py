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
import unicodedata

from .dispatch import _strip_terminal_controls
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
_BIDI_CONTROL_RE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_URI_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://")
_ACTIVE_URI_RE = re.compile(r"(?i)\b(javascript|vbscript|data):")
_BARE_WWW_RE = re.compile(r"(?i)\bwww\.")
_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+)@"
    r"(?=[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9-]+)+\b)"
)


def _sanitize_display(value: object, *, single_line: bool = False) -> str:
    """Neutralize terminal/display controls before any Markdown escaping."""
    text = _strip_terminal_controls(str(value))
    text = _BIDI_CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if single_line:
        return text.replace("\n", " ")
    return text


def _defang_links(text: str) -> str:
    text = _URI_RE.sub(lambda match: f"{match.group(1)}: //", text)
    text = _ACTIVE_URI_RE.sub(lambda match: f"{match.group(1)}: ", text)
    text = _BARE_WWW_RE.sub("www .", text)
    return _EMAIL_RE.sub(lambda match: f"{match.group(1)} @", text)


def _escape_cell(value: object) -> str:
    """Make `value` safe to place inside a single GFM table cell.

    A table cell that contains an unescaped `|` splits into extra columns
    for a human reading the rendered file, silently misaligning every field
    after it; a literal newline breaks the row entirely. Friend name and
    status are free text supplied by adapters/roster config, not claim
    authors, but nothing here guarantees they are pipe-free, so every cell
    is escaped the same way regardless of which field it came from.
    """
    text = _sanitize_display(value, single_line=True)
    text = _defang_links(text).replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("`", "&#96;").replace("[", "\\[").replace("]", "\\]")
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _escape_status_cell(value: object) -> str:
    """Defense in depth for a status rendered without resume validation."""
    text = _escape_cell(_strip_terminal_controls(str(value))).replace("`", "&#96;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*)://", r"\1: //", text)
    text = re.sub(r"(?i)\bwww\.", "www .", text)
    return re.sub(r"(?i)\b(javascript|vbscript|data):", r"\1 :", text)


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
    text = _sanitize_display(text, single_line=True)
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
    text = _sanitize_display(text)
    escaped_lines = []
    for line in text.split("\n"):
        match = _BLOCK_LEADER_RE.match(line)
        if match:
            marker = match.group("marker")
            start, end = match.span("marker")
            line = line[:start] + "\\" + marker[0] + marker[1:] + line[end:]
        line = _defang_links(line)
        if not line.lstrip(" \t").startswith("\\<"):
            line = re.sub(r"(?<!\\)<", r"\\<", line)
        line = line.replace("[", "\\[").replace("]", "\\]")
        escaped_lines.append(line)
    return "\n".join(escaped_lines)


def _external_authority_lines(run_meta: dict[str, Any]) -> list[str]:
    policy = run_meta.get("external_tool_policy")
    if policy == "deny":
        status = "denied"
        detail = "Provider-managed tools and connectors were denied for this run."
    elif policy == "allow":
        status = "explicitly-allowed"
        detail = (
            "Provider-managed tools and connectors were explicitly allowed for this run; "
            "the provider may have inherited integrations not inventoried here."
        )
    elif policy == "scoped-allow":
        status = "scoped-allow"
        grants = run_meta.get("external_tool_grants", [])
        detail = (
            "Provider-managed tools and connectors were allowed only for the recorded "
            f"providers: {', '.join(str(name) for name in grants) or 'none'}."
        )
    else:
        status = "legacy-unknown"
        detail = "This legacy capture does not record provider-managed tool authority."
    return ["## External tool authority", "", f"Status: `{status}`", "", detail, ""]


def _gate_lines(run_meta: dict[str, Any]) -> list[str]:
    lines = [
        "## Gate decision",
        "",
        f"Decision: `{_escape_cell(run_meta.get('gate_decision'))}`",
        "",
    ]
    blockers = run_meta.get("gate_blocking_claims") or []
    if blockers:
        lines.extend(["Blocking claims:", ""])
        lines.extend(f"- {_code_span(str(claim_id))}" for claim_id in blockers)
        lines.append("")
    else:
        lines.extend(["Blocking claims: _(none)_", ""])
    lines.extend([f"Stop reason: `{_escape_cell(run_meta.get('stop_reason', 'unknown'))}`", ""])

    ceiling = run_meta.get("ceiling_hit")
    failed_or_skipped = any(
        friend.get("independent", True)
        and str(friend.get("status", "")).startswith(("failed: ", "skipped: "))
        for friend in run_meta.get("friends", [])
    )
    partial = bool(
        ceiling
        or run_meta.get("incomplete")
        or failed_or_skipped
        or run_meta.get("stop_reason")
        in {"auth-abort", "incomplete", "interrupted", "runtime-error"}
    )
    if partial:
        qualifier = f" after reaching `{_escape_cell(ceiling)}`" if ceiling else ""
        lines.extend(
            [
                "**Partial evidence caveat:** The run stopped or lost evidence"
                f"{qualifier}; do not treat this gate result as a complete review.",
                "",
            ]
        )
    else:
        lines.extend(["Evidence caveat: _(none)_", ""])
    return lines


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
    if any(friend.get("host_self_review", False) for friend in run_meta.get("friends", [])):
        lines.extend(
            [
                "Host self-review verdicts are retained for audit but are advisory: "
                "they are excluded from settlement and quorum.",
                "",
            ]
        )
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
                    lines.append(f"  - proposed amendment: {_escape_block(verdict.amended_claim)}")
            lines.append("")

    if run_meta.get("amendment_notes"):
        lines.append("### Amendments")
        lines.append("")
        for note in run_meta["amendment_notes"]:
            lines.append(f"- {_escape_block(str(note))}")
        lines.append("")
    return lines


_ARTIFACT_LABEL_BYTES = 240


def _artifact_label(value: object) -> str:
    """A bounded single-line inline-code label, never Markdown structure."""
    cleaned: list[str] = []
    for char in str(value):
        if char.isspace():
            cleaned.append(" ")
        elif unicodedata.category(char).startswith("C"):
            continue
        else:
            cleaned.append({"`": "&#96;", "#": "&#35;", "<": "&lt;", ">": "&gt;"}.get(char, char))
    compact = " ".join("".join(cleaned).split()) or "unnamed artifact"
    bounded: list[str] = []
    size = 0
    for char in compact:
        encoded = char.encode("utf-8")
        if size + len(encoded) > _ARTIFACT_LABEL_BYTES - 3:
            bounded.append("…")
            break
        bounded.append(char)
        size += len(encoded)
    return f"`{''.join(bounded)}`"


def render(
    review: ReviewState,
    run_meta: dict[str, Any],
    states: dict[str, str] | None = None,
) -> str:
    claims = review.claims
    aliases = review.aliases
    verdicts = review.verdicts
    lines: list[str] = [f"# Adversarial review — {_artifact_label(run_meta['artifact'])}", ""]
    lines.append(
        f"Mode: {_code_span(str(run_meta['mode']))} · preset: {_code_span(str(run_meta['preset']))}"
    )
    lines.append("")
    lifecycle_state = run_meta.get("lifecycle_state")
    if lifecycle_state == "terminal":
        lines.extend(
            [
                "## Outcome",
                "",
                f"Stop reason: `{_escape_cell(run_meta['stop_reason'])}` · "
                f"Exit code: `{_escape_cell(run_meta['exit_code'])}` · "
                f"Converged: `{_escape_cell(run_meta['converged'])}`",
                "",
            ]
        )
        if run_meta.get("mode") == "gate":
            lines.extend(_gate_lines(run_meta))
    elif lifecycle_state is not None:
        lines.extend(
            [
                "## Outcome",
                "",
                f"Run state: `{_escape_cell(lifecycle_state)}`",
                "",
            ]
        )
    lines.extend(_external_authority_lines(run_meta))
    lines.append("## Friends")
    lines.append("")
    lines.append(
        "| friend | role | independent | model | effort | transport | write-protected | "
        "declared scope | OS-confined | status |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for friend in run_meta["friends"]:
        transport = friend.get("transport", "exec")
        write_protected = friend.get("write_protected", friend.get("readonly", False))
        declared_scope = friend.get("declared_scope", friend.get("scope", "unknown"))
        os_confined = friend.get("os_confined", False)
        independent = friend.get("independent", True)
        if friend.get("host_self_review", False):
            role = "host-self-review (advisory)"
        elif not independent:
            role = "legacy role unknown (advisory)"
        else:
            role = "independent reviewer"
        lines.append(
            f"| {_escape_cell(friend['name'])} | "
            f"{_escape_cell(role)} | "
            f"{_escape_cell(independent)} | "
            f"{_escape_cell(friend['model'] or 'inherited')} | "
            f"{_escape_cell(friend['effort'] or 'inherited')} | "
            f"{_escape_cell(transport)} | "
            f"{_escape_cell(write_protected)} | "
            f"{_escape_cell(declared_scope)} | "
            f"{_escape_cell(os_confined)} | "
            f"{_escape_status_cell(friend['status'])} |"
        )
    if not run_meta["friends"]:
        lines.append("| _(no friends were spawned)_ |  |  |  |  |  |  |  |  |  |")
    read_exposed: list[str] = []
    exposed_seen: set[str] = set()
    for friend in run_meta["friends"]:
        name = _sanitize_display(friend["name"], single_line=True)
        exposed = (
            friend.get("transport", "exec") != "http"
            and friend.get("write_protected", friend.get("readonly", False))
            and not friend.get("os_confined", False)
        )
        if exposed and name not in exposed_seen:
            exposed_seen.add(name)
            read_exposed.append(name)
    if read_exposed:
        lines.extend(
            [
                "",
                "**Filesystem read scope:** "
                + ", ".join(_escape_cell(name) for name in read_exposed)
                + " were write-protected but not OS-confined; each retained "
                "same-user filesystem read access outside the declared prompt scope.",
            ]
        )
    lines.append("")

    if run_meta.get("downgrades"):
        lines.append("## Downgrades")
        lines.append("")
        for note in run_meta["downgrades"]:
            lines.append(f"- {_escape_block(note)}")
        lines.append("")

    if states is not None:
        lines.extend(_render_verdict_sections(claims, verdicts or [], states, run_meta))

    proposals = run_meta.get("theme_proposals") or []
    if proposals:
        lines.extend(
            [
                "## Possible semantic duplicates",
                "",
                "These are advisory only. Every claim remains separate and independently "
                "addressable in the ledger.",
                "",
            ]
        )
        for proposal in proposals:
            lines.append(
                f"- {_code_span(str(proposal['duplicate']))} may share a theme with "
                f"{_code_span(str(proposal['canonical']))} at "
                f"{_code_span(str(proposal['anchor']))} "
                f"(score {_code_span(str(proposal['score']))})."
            )
        lines.append("")

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
        lines.append(
            f"### {_escape_cell(claim.id)} — {_escape_cell(claim.severity)}"
            f"{flag}{_escape_cell(badge)}"
        )
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

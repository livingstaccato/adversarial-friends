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
"""
from .ledger import Alias, Claim

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


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
    ``src/a.py:`eval(...)```).

    Per CommonMark, a backtick-delimited code span's fence must be a run of
    backticks strictly longer than the longest backtick run inside the
    content, and if the content starts or ends with a backtick, one space
    of padding on that side keeps the delimiter from fusing with it.
    """
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
    if text == "" or text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def render(claims: list[Claim], aliases: list[Alias], run_meta: dict) -> str:
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
            lines.append(f"- {note}")
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
        lines.append(f"### {claim.id} — {claim.severity}{flag}")
        lines.append("")
        lines.append(f"**Claim:** {claim.claim}")
        if claim.location:
            lines.append(f"**Location:** {_code_span(claim.location)}")
        lines.append(f"**Evidence:** {claim.evidence}")
        lines.append(f"**Failure scenario:** {claim.failure_scenario}")
        lines.append(f"**Suggested fix:** {claim.suggested_fix}")
        lines.append("")

    if aliases:
        lines.append("## Merged duplicates")
        lines.append("")
        for alias in aliases:
            lines.append(
                f"- {_code_span(alias.duplicate)} merged into "
                f"{_code_span(alias.canonical)}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"

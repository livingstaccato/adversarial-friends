"""Live documentation for automatic and explicit repository review context."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AFRIEND = REPO / "src" / "adversarial_friends" / "assets" / "entrypoints" / "afriend"


def test_operator_docs_explain_explicit_repository_review_context():
    paths = (
        REPO / "README.md",
        AFRIEND / "SKILL.md",
        AFRIEND / "references" / "troubleshooting.md",
    )
    docs = " ".join(" ".join(path.read_text().lower().replace("`", "").split()) for path in paths)

    for contract in (
        "afriend run docs/plan.md --mode report",
        'afriend run /tmp/reviews/plan.md --repo "$pwd" --mode report',
        "independently frozen",
        "git worktree root",
        "does not grant new provider, external-tool, or write authority",
        "resume uses the saved repository scope",
        "rejects --repo",
    ):
        assert contract in docs, contract

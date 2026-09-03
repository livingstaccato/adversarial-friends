"""Focused report coverage for review completeness and unconfined-read warnings."""

from report_helpers import meta

from adversarial_friends.report import render as render_review
from adversarial_friends.reviewstate import ReviewState


def render(claims, aliases, run_meta):
    """Match the core report suite's replayed-state rendering helper."""
    return render_review(ReviewState.replay([*claims, *aliases]), run_meta)


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
    assert "write-protected and not recorded as OS-confined" in sentence
    assert "If started" in sentence
    assert "same-user filesystem read access" in sentence


def test_read_scope_does_not_claim_a_failed_before_launch_friend_ran_unconfined():
    friend = {
        "name": "claude-security",
        "model": None,
        "effort": None,
        "transport": "exec",
        "write_protected": True,
        "declared_scope": "repo",
        "os_confined": False,
        "status": "failed: refused before launch",
        "round": 1,
    }

    out = render([], [], meta(friends=[friend]))
    sentence = next(line for line in out.splitlines() if line.startswith("**Filesystem"))

    assert "not recorded as OS-confined" in sentence
    assert "If started, each retained same-user filesystem read access" in sentence
    assert "ran without OS confinement" not in sentence

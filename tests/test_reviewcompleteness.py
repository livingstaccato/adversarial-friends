"""The bounded, persisted zero-response review projection."""

from adversarial_friends.reviewcompleteness import from_friends


def test_zero_independent_answers_are_persisted_as_incomplete():
    rows = [
        {
            "name": "codex-security",
            "independent": True,
            "status": "failed: DNS temporary failure",
        },
        {
            "name": "host",
            "independent": False,
            "host_self_review": True,
            "status": "ok",
        },
    ]

    assert from_friends(rows) == {
        "state": "incomplete",
        "answered": 0,
        "dispatched": 1,
        "reasons": ["codex-security: DNS temporary failure"],
        "message": "review incomplete: 0/1 friends answered; codex-security: DNS temporary failure",
    }


def test_an_independent_answer_needs_no_zero_response_summary():
    assert from_friends([{"name": "codex-security", "independent": True, "status": "ok"}]) is None


def test_failure_reasons_are_normalized_sorted_and_bounded():
    rows = [
        {"name": "zeta", "independent": True, "status": "failed: last"},
        {
            "name": "beta",
            "independent": True,
            "status": "skipped: " + "x" * 400,
        },
        {
            "name": "alpha",
            "independent": True,
            "status": "failed: first (stderr: raw detail; full text in round-1/alpha.err)",
        },
        {"name": "gamma", "independent": True, "status": "failed: fourth"},
    ]

    summary = from_friends(rows)

    assert summary is not None
    assert summary["dispatched"] == 4
    assert summary["reasons"] == [
        "alpha: first",
        "beta: " + "x" * 199 + "…",
        "gamma: fourth",
    ]
    assert "zeta: last" not in summary["message"]


def test_failure_reasons_strip_bidirectional_controls():
    summary = from_friends(
        [
            {
                "name": "codex-security",
                "independent": True,
                "status": "failed: safe\u202eflip",
            }
        ]
    )

    assert summary is not None
    assert summary["reasons"] == ["codex-security: safeflip"]
    assert "\u202e" not in summary["message"]

"""Verdict-specific report rendering kept separate from the 500-line core suite."""

from adversarial_friends.ledger import Claim, Verdict
from adversarial_friends.report import render
from adversarial_friends.reviewstate import ReviewState


def test_report_shows_every_conflicting_amendment():
    item = Claim(
        "c-0001@1",
        None,
        ["origin"],
        "ops",
        1,
        False,
        "high",
        "claim",
        "src/a.py:1",
        "evidence",
        "failure",
        "fix",
    )
    cast = [
        Verdict(
            item.id, "friend-a", 2, "amended", "high", "confirmed", "first", None, "first wording"
        ),
        Verdict(
            item.id, "friend-b", 2, "amended", "high", "confirmed", "second", None, "second wording"
        ),
    ]
    review = ReviewState.replay([item, *cast])
    meta = {"artifact": "spec.md", "mode": "crossexam", "preset": "inherit", "friends": []}

    out = render(review, meta, {item.id: "contested"})

    assert "proposed amendment: first wording" in out
    assert "proposed amendment: second wording" in out


def test_friend_table_separates_write_protection_from_os_confinement():
    review = ReviewState.replay([])
    meta = {
        "artifact": "spec.md",
        "mode": "report",
        "preset": "inherit",
        "friends": [
            {
                "name": "claude-security-0",
                "model": None,
                "effort": None,
                "transport": "exec",
                "write_protected": True,
                "declared_scope": "repo",
                "os_confined": False,
                "status": "ok",
            }
        ],
    }

    out = render(review, meta)

    assert "| write-protected | declared scope | OS-confined |" in out
    assert "| True | repo | False |" in out
    assert "same-user filesystem read access" in out

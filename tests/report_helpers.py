"""Shared construction helpers for report rendering tests."""

from adversarial_friends.ledger import Claim


def claim(cid: str, severity: str = "high") -> Claim:
    return Claim(
        id=cid,
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity=severity,
        claim="the guard is missing",
        location="src/a.py:42",
        evidence="src/a.py:38",
        failure_scenario="expired token passes",
        suggested_fix="check exp",
    )


def meta(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "mode": "report",
        "preset": "inherit",
        "artifact": "spec.md",
        "friends": [
            {
                "name": "codex-ops",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "independent": False,
                "host_self_review": True,
                "readonly": True,
                "scope": "repo",
                "status": "ok",
            },
            {
                "name": "opencode-security",
                "model": None,
                "effort": "unverified",
                "independent": True,
                "host_self_review": False,
                "readonly": False,
                "scope": "doc",
                "status": "failed: exit 1",
            },
        ],
        "downgrades": ["opencode: no read-only capability, forced to doc scope"],
    }
    base.update(over)
    return base

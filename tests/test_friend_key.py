"""The ledger identity is the roster unit (§8.1), not `cli/lens`."""

from afriend.adapters import FriendSpec, friend_key


def _spec(model=None, effort=None):
    return FriendSpec(
        name="codex-ops-0",
        cli="codex",
        lens="ops",
        model=model,
        effort=effort,
        scope="doc",
        timeout=9,
    )


def test_a_friend_with_no_model_or_effort_keeps_the_short_identity():
    """What every existing ledger holds."""
    assert friend_key(_spec()) == "codex/ops"


def test_model_and_effort_are_part_of_the_identity():
    """Two models on one CLI under one lens are two judges. They were one,
    and flag order decided which of them counted."""
    assert friend_key(_spec(model="gpt-5")) == "codex/ops@gpt-5"
    assert friend_key(_spec(model="gpt-5", effort="high")) == "codex/ops@gpt-5+high"
    assert friend_key(_spec(effort="low")) == "codex/ops+low"
    assert friend_key(_spec(model="gpt-5")) != friend_key(_spec(model="gpt-5-mini"))

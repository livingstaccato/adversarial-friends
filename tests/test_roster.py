import pytest

from adversarial_friends import adapters, roster
from adversarial_friends.errors import NoFriendsError, UsageError

ADAPTER_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "skills" / "adversarial-friends" / "adapters"
LENSES = ["assumptions", "security", "ops"]


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def which_all(name):
    return f"/usr/local/bin/{name}"


def which_none(name):
    return None


def test_detects_claude_code_host():
    assert roster.detect_host({"CLAUDECODE": "1"}) == "claude"


def test_detects_codex_host():
    assert roster.detect_host({"CODEX_SANDBOX": "seatbelt"}) == "codex"


def test_no_host_detected_when_env_is_bare():
    assert roster.detect_host({}) is None


def test_host_cli_is_excluded_by_default(registry):
    friends = roster.resolve(registry, LENSES, {"CLAUDECODE": "1"}, which_all)
    assert all(f.cli != "claude" for f in friends)


def test_include_self_keeps_the_host(registry):
    friends = roster.resolve(registry, LENSES, {"CLAUDECODE": "1"}, which_all,
                             include_self=True)
    assert any(f.cli == "claude" for f in friends)


def test_lenses_are_assigned_round_robin(registry):
    friends = roster.resolve(registry, LENSES, {}, which_all)
    assigned = [f.lens for f in friends]
    assert assigned[:3] == LENSES[:3]
    assert len(set(assigned[:3])) == 3


def test_opencode_defaults_to_doc_scope(registry):
    """opencode has no read-only mode, so repo scope needs an explicit opt-in."""
    friends = roster.resolve(registry, LENSES, {}, which_all)
    opencode = next(f for f in friends if f.cli == "opencode")
    assert opencode.scope == "doc"


def test_no_binaries_raises_no_friends(registry):
    with pytest.raises(NoFriendsError):
        roster.resolve(registry, LENSES, {}, which_none)


def test_overrides_replace_discovery(registry):
    friends = roster.resolve(
        registry, LENSES, {}, which_all,
        overrides=[{"name": "codex-ops", "cli": "codex", "lens": "ops",
                    "model": "gpt-5.6-sol", "effort": "high"}],
    )
    assert len(friends) == 1
    assert friends[0].name == "codex-ops"
    assert friends[0].model == "gpt-5.6-sol"

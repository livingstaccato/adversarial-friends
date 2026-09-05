import pytest

from afriend import trust
from afriend.errors import UsageError


def test_valid_entry_passes():
    entry = {
        "name": "codex-ops",
        "cli": "codex",
        "lens": "ops",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "scope": "repo",
        "timeout": 900,
    }
    assert trust.validate_roster_entry(entry)["name"] == "codex-ops"


def test_extra_args_key_is_rejected():
    entry = {
        "name": "x",
        "cli": "codex",
        "lens": "ops",
        "extra_args": ["--dangerously-bypass-approvals-and-sandbox"],
    }
    with pytest.raises(UsageError) as excinfo:
        trust.validate_roster_entry(entry)
    assert "extra_args" in str(excinfo.value)


def test_profile_key_is_rejected():
    """--profile layers a TOML file the runner never reads, so argv would lie."""
    entry = {"name": "x", "cli": "codex", "lens": "ops", "profile": "review"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_traversal_name_is_rejected():
    entry = {"name": "../../../../tmp/owned", "cli": "codex", "lens": "ops"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_bad_scope_is_rejected():
    entry = {"name": "x", "cli": "codex", "lens": "ops", "scope": "everything"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "-s", "danger-full-access"],
        ["codex", "-s", "workspace-write"],
        ["codex", "--sandbox", "danger-full-access"],
        ["codex", "--sandbox", "workspace-write"],
        ["claude", "--dangerously-skip-permissions"],
        ["opencode", "--auto"],
        ["gemini", "-y"],
    ],
)
def test_denied_values_abort(argv):
    with pytest.raises(UsageError):
        trust.check_denied_values(argv)


def test_hardening_flags_are_permitted():
    """The check is direction-aware: making a run safer must never abort it."""
    trust.check_denied_values(["codex", "-s", "read-only"])
    trust.check_denied_values(["claude", "--permission-mode", "plan"])


def test_combined_equals_sandbox_value_is_denied():
    """--sandbox=value and -s=value must be caught the same as the
    space-separated form; a real (e.g. clap-based) CLI accepts both spellings,
    and checking only argv[index + 1] misses the combined-token one."""
    with pytest.raises(UsageError):
        trust.check_denied_values(["codex", "--sandbox=danger-full-access"])
    with pytest.raises(UsageError):
        trust.check_denied_values(["codex", "-s=workspace-write"])


def test_combined_equals_safe_sandbox_value_is_permitted():
    trust.check_denied_values(["codex", "--sandbox=read-only"])


DENIED_FLAG_SPELLINGS = [(flag, [flag]) for flag in sorted(trust.DENIED_FLAGS)] + [
    (flag, [f"{flag}=true"]) for flag in sorted(trust.DENIED_FLAGS)
]


@pytest.mark.parametrize(
    "flag,tail", DENIED_FLAG_SPELLINGS, ids=[f"{f}:{t[0]}" for f, t in DENIED_FLAG_SPELLINGS]
)
def test_every_denied_flag_is_caught_bare_and_with_equals(flag, tail):
    """The DENIED_FLAGS branch must partition on '=' the same way the
    sandbox branch does — a boolean flag spelled --flag=true is exactly as
    dangerous as bare --flag, and must not slip past because the check only
    compared the whole raw token."""
    with pytest.raises(UsageError):
        trust.check_denied_values(["some-cli", *tail])


@pytest.mark.parametrize("value", ["bypassPermissions", "dontAsk"])
def test_permission_mode_denied_values_abort(value):
    with pytest.raises(UsageError):
        trust.check_denied_values(["claude", "--permission-mode", value])
    with pytest.raises(UsageError):
        trust.check_denied_values(["claude", f"--permission-mode={value}"])


@pytest.mark.parametrize("value", ["plan", "acceptEdits"])
def test_permission_mode_safe_values_are_permitted(value):
    """Direction-aware, same as the sandbox rule: never reject someone for
    asking to be safer."""
    trust.check_denied_values(["claude", "--permission-mode", value])
    trust.check_denied_values(["claude", f"--permission-mode={value}"])


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5.6-sol",
        "claude-sonnet-4-6",
        "cloudflare-ai-gateway/openai/gpt-5-nano",
        "gemini-3.1-pro-high",
        "qwen3:0.6b",
    ],
)
def test_valid_model_ids_are_accepted(model):
    entry = {"name": "x", "cli": "codex", "lens": "ops", "model": model}
    assert trust.validate_roster_entry(entry)["model"] == model


def test_flag_looking_model_value_is_rejected():
    """model becomes a literal argv token; a value that starts with '-' must
    never be accepted, even though it can't inject a second flag on its own
    (argv is exec'd as a list, never through a shell) — an unconstrained
    string landing in argv is still a poor boundary."""
    entry = {"name": "x", "cli": "codex", "lens": "ops", "model": "--dangerously-skip-permissions"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_boolean_timeout_is_rejected():
    """bool is an int subclass in Python; isinstance(True, int) is True, and
    True <= 0 is False, so an unguarded check would silently accept a
    timeout of True as a 1-second timeout."""
    entry = {"name": "x", "cli": "codex", "lens": "ops", "timeout": True}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_contain_path_allows_paths_under_base(tmp_path):
    assert trust.contain_path(tmp_path, tmp_path / "round-1" / "a.raw")


def test_contain_path_rejects_escape(tmp_path):
    with pytest.raises(UsageError):
        trust.contain_path(tmp_path, tmp_path / ".." / "escaped.raw")

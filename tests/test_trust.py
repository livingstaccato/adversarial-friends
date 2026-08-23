from pathlib import Path

import pytest

from adversarial_friends import trust
from adversarial_friends.errors import UsageError


def test_valid_entry_passes():
    entry = {"name": "codex-ops", "cli": "codex", "lens": "ops",
             "model": "gpt-5.6-sol", "effort": "high", "scope": "repo",
             "timeout": 900}
    assert trust.validate_roster_entry(entry)["name"] == "codex-ops"


def test_extra_args_key_is_rejected():
    entry = {"name": "x", "cli": "codex", "lens": "ops",
             "extra_args": ["--dangerously-bypass-approvals-and-sandbox"]}
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


@pytest.mark.parametrize("argv", [
    ["codex", "-s", "danger-full-access"],
    ["codex", "-s", "workspace-write"],
    ["claude", "--dangerously-skip-permissions"],
    ["opencode", "--auto"],
    ["gemini", "-y"],
])
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


def test_contain_path_allows_paths_under_base(tmp_path):
    assert trust.contain_path(tmp_path, tmp_path / "round-1" / "a.raw")


def test_contain_path_rejects_escape(tmp_path):
    with pytest.raises(UsageError):
        trust.contain_path(tmp_path, tmp_path / ".." / "escaped.raw")

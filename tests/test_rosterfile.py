"""Tests for roster files (spec §10, §13, §17).

The trust boundary carries most of the weight here. §13 is explicit that a
repo-local roster is untrusted and a user-level one is not, and the whole
point of a roster is that it changes who reviews your code and how -- so
"which paths get picked up without being asked for" is a security property,
not a convenience.
"""

import pytest

from adversarial_friends import rosterfile
from adversarial_friends.errors import NoFriendsError, UsageError

VALID = """
[[friend]]
name = "codex-ops"
cli = "codex"
lens = "ops"

[[friend]]
name = "claude-security"
cli = "claude"
lens = "security"
model = "claude-sonnet-4-6"
effort = "high"
"""


def write(tmp_path, text, name="roster.toml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_entries_are_read_in_order(tmp_path):
    entries = rosterfile.load(write(tmp_path, VALID))
    assert [e["name"] for e in entries] == ["codex-ops", "claude-security"]
    assert entries[1]["effort"] == "high"


def test_a_missing_file_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="not found"):
        rosterfile.load(tmp_path / "nope.toml")


def test_malformed_toml_names_the_file(tmp_path):
    with pytest.raises(UsageError, match="not valid TOML"):
        rosterfile.load(write(tmp_path, "[[friend]\nbroken"))


def test_a_file_with_no_friend_table_explains_the_format(tmp_path):
    with pytest.raises(UsageError, match="no friends"):
        rosterfile.load(write(tmp_path, "title = 'not a roster'\n"))


def test_an_empty_roster_raises_rather_than_falling_back(tmp_path):
    """The landmine roster.resolve documents: it treats an explicit empty
    override list the same as "no overrides given" and falls through to full
    auto-discovery. A file deliberately naming zero friends would then
    silently run every discovered CLI instead of none."""
    with pytest.raises(NoFriendsError, match="not the"):
        rosterfile.load(write(tmp_path, "friend = []\n"))


def test_friend_must_be_a_list_of_tables(tmp_path):
    with pytest.raises(UsageError, match="list of tables"):
        rosterfile.load(write(tmp_path, "friend = 'codex'\n"))


# --- §13's trust boundary --------------------------------------------------


def test_the_trusted_path_is_under_user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert rosterfile.default_roster_path() == tmp_path / "adversarial-friends" / "roster.toml"


def test_discovery_finds_the_user_roster(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "adversarial-friends" / "roster.toml"
    target.parent.mkdir(parents=True)
    target.write_text(VALID)
    assert rosterfile.discover() == target


def test_discovery_never_looks_in_the_repository(monkeypatch, tmp_path):
    """§13: repo-local `.adversarial-friends/` is untrusted. A cloned repo
    must not be able to choose who reviews it, on what, with what flags.
    Naming one with --roster is an explicit act; finding one is not."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(tmp_path)
    hostile = tmp_path / ".adversarial-friends"
    hostile.mkdir()
    (hostile / "roster.toml").write_text(VALID)
    assert rosterfile.discover() is None


# --- Rendering -------------------------------------------------------------


def test_a_rendered_roster_round_trips(tmp_path):
    entries = [{"name": "codex-ops", "cli": "codex", "lens": "ops", "timeout": 900}]
    reloaded = rosterfile.load(write(tmp_path, rosterfile.render(entries)))
    assert reloaded == entries


def test_rendering_omits_unset_keys(tmp_path):
    """Checked by parsing rather than by searching the text: the header
    comment names every optional key, so a substring check would fail for
    the wrong reason."""
    text = rosterfile.render([{"name": "a", "cli": "codex", "lens": "ops"}])
    entry = rosterfile.load(write(tmp_path, text))[0]
    assert set(entry) == {"name", "cli", "lens"}


def test_notes_are_rendered_as_comments(tmp_path):
    text = rosterfile.render([{"name": "a", "cli": "codex", "lens": "ops"}], ["watch out"])
    assert "# watch out" in text
    # And the file still parses, which a stray uncommented note would break.
    rosterfile.load(write(tmp_path, text))

import json
from pathlib import Path

import pytest

from adversarial_friends import reviewprofiles, sessionconfig
from adversarial_friends.errors import UsageError


def test_missing_session_config_defaults_to_quick(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert sessionconfig.load(reviewprofiles.names()).default_profile == "quick"
    assert not sessionconfig.config_path().exists()


def test_set_default_round_trips_with_atomic_json_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    sessionconfig.set_default("balanced", known=reviewprofiles.names())

    assert sessionconfig.load(reviewprofiles.names()).default_profile == "balanced"
    assert json.loads(sessionconfig.config_path().read_text(encoding="utf-8")) == {
        "default_profile": "balanced",
        "profiles": {},
        "version": 2,
    }
    assert list(sessionconfig.config_path().parent.glob("*.tmp")) == []


def test_set_default_refuses_an_unknown_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(UsageError, match=r"default_profile.*one of.*quick"):
        sessionconfig.set_default("unsafe", known=reviewprofiles.names())

    assert not sessionconfig.config_path().exists()


def test_config_path_honors_absolute_xdg_home_and_rejects_relative(tmp_path, monkeypatch):
    absolute = tmp_path / "absolute-config"
    assert sessionconfig.config_path({"XDG_CONFIG_HOME": str(absolute)}) == (
        absolute / "adversarial-friends" / "session.json"
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert sessionconfig.config_path({"XDG_CONFIG_HOME": ".relative-config"}) == (
        tmp_path / "home" / ".config" / "adversarial-friends" / "session.json"
    )


@pytest.mark.parametrize(
    ("contents", "field"),
    [
        ("not json", "malformed JSON"),
        ("[]", "top-level"),
        ('{"version": 1}', "top-level keys"),
        ('{"version": 1, "default_profile": "quick", "provider": "codex"}', "top-level keys"),
        ('{"version": 2, "default_profile": "quick"}', "version"),
        ('{"version": 1, "default_profile": 7}', "default_profile"),
        ('{"version": 1, "default_profile": "unknown"}', "default_profile"),
    ],
)
def test_malformed_session_contract_is_rejected(tmp_path, monkeypatch, contents, field):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = sessionconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(UsageError, match=rf"session.json.*{field}"):
        sessionconfig.load(reviewprofiles.names())


def test_builtin_profiles_only_define_safe_run_defaults():
    assert reviewprofiles.names() == ("balanced", "quick", "thorough")
    assert reviewprofiles.get("quick") == reviewprofiles.ReviewProfile(name="quick", mode="report")
    assert reviewprofiles.get("balanced") == reviewprofiles.ReviewProfile(
        name="balanced", mode="crossexam"
    )
    assert reviewprofiles.get("thorough") == reviewprofiles.ReviewProfile(
        name="thorough", mode="loop"
    )
    assert reviewprofiles.get("unknown") is None
    for profile in reviewprofiles.builtins().values():
        assert tuple(vars(profile)) == ("name", "mode", "settings")
        assert "provider" not in vars(profile)
        assert "friend" not in vars(profile)
        assert "credential" not in vars(profile)
        assert "process" not in vars(profile)
        assert "authority" not in vars(profile)


def test_review_profile_default_settings_are_independent_immutable_mappings():
    first = reviewprofiles.ReviewProfile(name="first", mode="report")
    second = reviewprofiles.ReviewProfile(name="second", mode="report")

    assert first.settings == second.settings == {}
    assert first.settings is not second.settings
    with pytest.raises(TypeError):
        first.settings["timeout"] = 1  # type: ignore[index]


def test_session_config_default_profiles_are_independent_immutable_mappings():
    first = sessionconfig.SessionConfig()
    second = sessionconfig.SessionConfig()

    assert first.profiles == second.profiles == {}
    assert first.profiles is not second.profiles
    with pytest.raises(TypeError):
        first.profiles["ci"] = {}  # type: ignore[index]

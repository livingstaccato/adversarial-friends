"""Named, safe review profiles and their CLI management surface."""

import json

import pytest

from adversarial_friends import reviewprofiles, sessionconfig
from adversarial_friends.cli import main
from adversarial_friends.errors import UsageError


def test_v1_session_config_is_loaded_as_no_custom_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = sessionconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"default_profile": "balanced", "version": 1}\n', encoding="utf-8")

    config = sessionconfig.load()

    assert config.default_profile == "balanced"
    assert dict(config.profiles) == {}


def test_custom_profile_inherits_builtin_and_only_overrides_safe_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    sessionconfig.create_profile("ci", "balanced", {"max_friends": 3, "timeout": 120})
    config = sessionconfig.load()
    resolved = reviewprofiles.resolve("ci", config.profiles)

    assert resolved is not None
    assert resolved.mode == "crossexam"
    assert dict(resolved.settings) == {"max_friends": 3, "timeout": 120}
    assert json.loads(sessionconfig.config_path().read_text(encoding="utf-8")) == {
        "default_profile": "quick",
        "profiles": {"ci": {"base": "balanced", "max_friends": 3, "timeout": 120}},
        "version": 2,
    }


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"base": "quick", "provider": "codex"}, "unknown fields"),
        ({"base": "missing", "timeout": 30}, "unknown base"),
        ({"base": "a"}, "cycle"),
        ({"base": "quick", "timeout": 0}, "timeout.*positive"),
    ],
)
def test_profile_schema_refuses_unsafe_or_invalid_inheritance(
    tmp_path, monkeypatch, profile, message
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = sessionconfig.config_path()
    path.parent.mkdir(parents=True)
    profiles = {"a": profile}
    if profile == {"base": "a"}:
        profiles["a"] = {"base": "b", "timeout": 1}
        profiles["b"] = {"base": "a", "timeout": 1}
    path.write_text(
        json.dumps({"version": 2, "default_profile": "quick", "profiles": profiles}),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match=message):
        sessionconfig.load()


def test_profiles_command_lifecycle_and_default_protection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert main(["profiles", "create", "ci", "--base", "quick", "--max-friends", "2"]) == 0
    assert main(["profiles", "set-default", "ci"]) == 0
    assert main(["profiles", "delete", "ci"]) == 2
    assert "current default" in capsys.readouterr().err

    assert main(["profiles", "update", "ci", "--timeout", "60"]) == 0
    capsys.readouterr()
    assert main(["profiles", "show", "ci", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["name"] == "ci"
    assert shown["base"] == "quick"
    assert shown["max_friends"] == 2
    assert shown["timeout"] == 60

    assert main(["profiles", "set-default", "quick"]) == 0
    assert main(["profiles", "delete", "ci"]) == 0
    assert sessionconfig.load().profiles == {}


def test_profiles_list_prints_builtins_and_custom_inheritance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sessionconfig.create_profile("ci", "quick", {"timeout": 60})

    assert main(["profiles", "list"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "balanced  built-in  crossexam",
        "quick  built-in  report",
        "thorough  built-in  loop",
        "ci  custom  inherits quick",
    ]


def test_custom_profile_applies_defaults_without_overriding_explicit_flags(tmp_path, monkeypatch):
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands.runmeta import validate_run_args

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    artifact = tmp_path / "spec.md"
    artifact.write_text("# Spec\n", encoding="utf-8")
    sessionconfig.create_profile("ci", "balanced", {"timeout": 120, "max_friends": 2})
    args = build_parser().parse_args(["run", str(artifact), "--profile", "ci", "--timeout", "9"])

    resolved, _ = validate_run_args(args)

    assert resolved.mode == "crossexam"
    assert resolved.max_friends == 2
    assert resolved.timeout == 9


def test_guided_setup_can_select_an_existing_custom_profile(tmp_path, monkeypatch):
    from adversarial_friends.commands.init import cmd_init

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sessionconfig.create_profile("ci", "quick", {"timeout": 60})
    args = type(
        "Args",
        (),
        {
            "guided": True,
            "apply": False,
            "default_profile": "ci",
            "enable_provider": [],
            "disable_provider": [],
            "ollama_model": None,
            "json": True,
            "out": None,
            "force": False,
        },
    )()

    # Guided setup remains a preview but must not reject the profile it manages.
    assert cmd_init(args) == 0

"""The no-prompt, no-authority guided setup command."""

import json

import pytest

from afriend import providerconfig, readiness, reviewprofiles, sessionconfig
from afriend.cliargs import build_parser
from afriend.commands import init as init_module
from afriend.errors import NoFriendsError, UsageError
from afriend.paths import ADAPTER_DIR


def _args(*argv: str):
    return build_parser().parse_args(["init", "--guided", *argv])


def _known() -> set[str]:
    return set(init_module.load_adapters(ADAPTER_DIR))


def _ready_codex() -> dict[str, readiness.FriendReadiness]:
    return {
        "codex": readiness.FriendReadiness(
            "codex", readiness.ReadinessState.READY, "available", "/bin/codex", None
        )
    }


def test_guided_preview_is_a_no_write_no_probe_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_SESSION_ID", "guided-preview")
    roster = tmp_path / "roster.toml"
    roster.write_text("# existing roster\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: pytest.fail("probed"))

    assert (
        init_module.cmd_init(
            _args(
                "--default-profile", "balanced", "--enable-provider", "ollama", "--out", str(roster)
            )
        )
        == 0
    )

    captured = capsys.readouterr()
    output = captured.err
    assert captured.out == ""
    assert "schema version: 1" in output
    assert "default profile: balanced" in output
    assert "enable provider: ollama" in output
    assert "profiles: balanced (crossexam), quick (report), thorough (loop)" in output
    assert "host: codex (host-self-review; advisory=True; independent=False)" in output
    assert "external tools remain denied" in output
    assert "no files were written" in output
    assert roster.read_text(encoding="utf-8") == "# existing roster\n"
    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_preview_json_is_machine_readable_and_never_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for marker in readiness.HOST_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)

    assert (
        init_module.cmd_init(
            _args(
                "--json",
                "--disable-provider",
                "opencode",
                "--ollama-model",
                "qwen3:8b",
                "--enable-provider",
                "ollama",
            )
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["guided"] is True
    assert payload["apply"] is False
    assert payload["changes"] == {
        "providers": {
            "ollama": {"enabled": True, "model": "qwen3:8b"},
            "opencode": {"enabled": False},
        }
    }
    assert payload["external_tools"] == "denied"
    assert payload["profiles"] == [
        {"mode": "crossexam", "name": "balanced"},
        {"mode": "report", "name": "quick"},
        {"mode": "loop", "name": "thorough"},
    ]
    assert {row["name"] for row in payload["providers"]} == _known()
    assert payload["host"] is None
    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_apply_changes_only_selected_settings_and_preserves_others(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: _ready_codex())
    known = _known()
    providerconfig.set_enabled("opencode", False, known=known)
    providerconfig.set_model("opencode", "gpt-5.6-sol", known=known)
    providerconfig.set_model("ollama", "old-model", known=known)
    sessionconfig.set_default("quick", known=reviewprofiles.names())

    assert (
        init_module.cmd_init(
            _args(
                "--apply",
                "--default-profile",
                "thorough",
                "--enable-provider",
                "ollama",
                "--disable-provider",
                "codex",
                "--ollama-model",
                "qwen3:8b",
            )
        )
        == 0
    )

    assert sessionconfig.load().default_profile == "thorough"
    policy = providerconfig.load(known)
    assert policy.setting("ollama") == providerconfig.ProviderSetting(True, "qwen3:8b")
    assert policy.setting("codex") == providerconfig.ProviderSetting(False, None)
    assert policy.setting("opencode") == providerconfig.ProviderSetting(False, "gpt-5.6-sol")
    output = capsys.readouterr().err
    assert "changed:" in output
    assert "first review: afriend run <artifact>" in output
    assert "external tools remain denied" in output


def test_guided_apply_also_generates_the_normal_roster(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "roster.toml"
    registry = init_module.load_adapters(ADAPTER_DIR)
    rows = {
        "codex": readiness.FriendReadiness(
            "codex", readiness.ReadinessState.READY, "available", "/bin/codex", None
        )
    }
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: rows)

    assert (
        init_module.cmd_init(
            _args("--apply", "--default-profile", "balanced", "--out", str(target))
        )
        == 0
    )

    assert target.exists()
    assert 'cli = "codex"' in target.read_text(encoding="utf-8")
    output = capsys.readouterr().err
    assert str(target) in output
    assert str(sessionconfig.config_path()) in output
    assert set(rows) <= set(registry)


def test_guided_apply_refuses_an_existing_roster_before_config_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "roster.toml"
    target.write_text("# do not replace\n", encoding="utf-8")

    with pytest.raises(UsageError, match="--force"):
        init_module.cmd_init(
            _args("--apply", "--default-profile", "balanced", "--out", str(target))
        )

    assert target.read_text(encoding="utf-8") == "# do not replace\n"
    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_apply_does_not_persist_settings_when_no_roster_can_be_generated(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "roster.toml"
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: {})

    with pytest.raises(NoFriendsError):
        init_module.cmd_init(
            _args("--apply", "--default-profile", "balanced", "--out", str(target))
        )

    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()
    assert not target.exists()


def test_guided_apply_stages_a_roster_against_requested_provider_enablement(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "roster.toml"
    known = _known()
    providerconfig.set_enabled("codex", False, known=known)

    def only_enabled_codex(_registry, policy, **_kwargs):
        if not policy.setting("codex").enabled:
            return {}
        return {
            "codex": readiness.FriendReadiness(
                "codex", readiness.ReadinessState.READY, "available", "/bin/codex", None
            )
        }

    monkeypatch.setattr(init_module, "assess_all", only_enabled_codex)

    assert (
        init_module.cmd_init(_args("--apply", "--enable-provider", "codex", "--out", str(target)))
        == 0
    )

    assert 'cli = "codex"' in target.read_text(encoding="utf-8")
    assert providerconfig.load(known).setting("codex").enabled is True


def test_guided_apply_force_replaces_the_existing_roster(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "roster.toml"
    target.write_text("# replace me\n", encoding="utf-8")
    rows = {
        "codex": readiness.FriendReadiness(
            "codex", readiness.ReadinessState.READY, "available", "/bin/codex", None
        )
    }
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: rows)

    assert init_module.cmd_init(_args("--apply", "--force", "--out", str(target))) == 0

    assert target.read_text(encoding="utf-8") != "# replace me\n"


@pytest.mark.parametrize(
    ("argv", "match"),
    [
        (("--apply",), "requires --guided"),
        (("--default-profile", "unknown"), "default profile.*one of"),
        (("--enable-provider", "unknown"), "provider.*one of"),
        (("--enable-provider", "codex", "--disable-provider", "codex"), "both enable and disable"),
        (("--ollama-model", "qwen3:8b"), "requires --enable-provider ollama"),
        (("--enable-provider", "ollama", "--ollama-model", ""), "model"),
        (
            ("--disable-provider", "ollama", "--ollama-model", "qwen3:8b"),
            "requires --enable-provider ollama",
        ),
    ],
)
def test_guided_setup_rejects_invalid_combinations_before_writes(
    tmp_path, monkeypatch, argv, match
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    parser = build_parser()
    args = (
        parser.parse_args(["init", *argv])
        if argv == ("--apply",)
        else parser.parse_args(["init", "--guided", *argv])
    )

    with pytest.raises(UsageError, match=match):
        init_module.cmd_init(args)

    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_apply_requires_explicit_changes_before_creating_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: _ready_codex())

    assert init_module.cmd_init(_args("--apply")) == 0

    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()

"""The no-prompt, no-authority guided setup command."""

import json

import pytest

from adversarial_friends import providerconfig, reviewprofiles, sessionconfig
from adversarial_friends.cliargs import build_parser
from adversarial_friends.commands import init as init_module
from adversarial_friends.errors import UsageError
from adversarial_friends.paths import ADAPTER_DIR


def _args(*argv: str):
    return build_parser().parse_args(["init", "--guided", *argv])


def _known() -> set[str]:
    return set(init_module.load_adapters(ADAPTER_DIR))


def test_guided_preview_is_a_no_write_no_probe_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
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

    output = capsys.readouterr().out
    assert "default profile: balanced" in output
    assert "enable provider: ollama" in output
    assert "external tools remain denied" in output
    assert "no files were written" in output
    assert roster.read_text(encoding="utf-8") == "# existing roster\n"
    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_preview_json_is_machine_readable_and_never_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

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

    assert json.loads(capsys.readouterr().out) == {
        "apply": False,
        "changes": {
            "providers": {
                "ollama": {"enabled": True, "model": "qwen3:8b"},
                "opencode": {"enabled": False},
            }
        },
        "external_tools": "denied",
        "guided": True,
    }
    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()


def test_guided_apply_changes_only_selected_settings_and_preserves_others(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
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
    assert "external tools remain denied" in capsys.readouterr().out


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

    assert init_module.cmd_init(_args("--apply")) == 0

    assert not sessionconfig.config_path().exists()
    assert not providerconfig.config_path().exists()

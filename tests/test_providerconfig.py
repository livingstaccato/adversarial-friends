import json
import multiprocessing
import os
from pathlib import Path

import pytest

from adversarial_friends import providerconfig
from adversarial_friends.errors import UsageError


def _disable_ollama(config_home: str, ready) -> None:
    os.environ["XDG_CONFIG_HOME"] = config_home
    ready.send("ready-to-lock")
    ready.close()
    providerconfig.set_enabled("ollama", False, known={"codex", "ollama"})


def test_missing_config_defaults_every_known_provider_to_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    policy = providerconfig.load(["codex", "ollama"])
    assert policy.setting("codex") == providerconfig.ProviderSetting(enabled=True, model=None)
    assert policy.setting("ollama") == providerconfig.ProviderSetting(enabled=True, model=None)


def test_disabled_provider_and_model_round_trip_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    providerconfig.set_enabled("ollama", False, known={"ollama"})
    providerconfig.set_model("ollama", "qwen3:0.6b", known={"ollama"})
    assert providerconfig.load(["ollama"]).setting("ollama") == providerconfig.ProviderSetting(
        enabled=False, model="qwen3:0.6b"
    )
    assert json.loads(providerconfig.config_path().read_text(encoding="utf-8")) == {
        "providers": {"ollama": {"enabled": False, "model": "qwen3:0.6b"}},
        "version": 1,
    }


def test_locked_update_rereads_changes_made_before_it_acquires(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    receiver, sender = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(target=_disable_ollama, args=(str(tmp_path), sender))
    started = False
    try:
        with providerconfig._update_lock():
            process.start()
            started = True
            sender.close()
            assert receiver.poll(5), "second updater did not reach the lock attempt"
            assert receiver.recv() == "ready-to-lock"
            assert process.is_alive(), "second updater must wait for the transaction lock"
            initial = providerconfig.load(["codex", "ollama"])
            settings = dict(initial.providers)
            settings["codex"] = providerconfig.ProviderSetting(model="gpt-5.6-sol")
            providerconfig._write_locked(providerconfig.ProviderPolicy(settings))
        process.join(5)
        assert process.exitcode == 0
    finally:
        receiver.close()
        sender.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(5)
    policy = providerconfig.load(["codex", "ollama"])
    assert policy.setting("ollama").enabled is False
    assert policy.setting("codex").model == "gpt-5.6-sol"


def test_update_lock_preserves_oserror_from_transaction_body(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sentinel = OSError("sentinel transaction failure")
    with (
        pytest.raises(OSError, match="sentinel transaction failure") as excinfo,
        providerconfig._update_lock(),
    ):
        raise sentinel
    assert excinfo.value is sentinel


def test_invalid_config_names_the_file_and_field(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "providers": {"ollama": {"enabled": "yes"}}}')
    with pytest.raises(UsageError, match=r"config.json.*providers.ollama.enabled.*got 'yes'"):
        providerconfig.load(["ollama"])


@pytest.mark.parametrize(
    ("contents", "field", "invalid_value"),
    [
        ("not json", "malformed JSON", None),
        ("[]", "top-level", "got []"),
        ('{"version": 1}', "top-level keys", "got ['version']"),
        (
            '{"version": 1, "providers": {}, "extra": true}',
            "top-level keys",
            "got ['extra', 'providers', 'version']",
        ),
        ('{"version": 2, "providers": {}}', "version", "got 2"),
        ('{"version": 1, "providers": []}', "providers", "got []"),
        ('{"version": 1, "providers": {"other": {}}}', "providers.other", "got 'other'"),
        ('{"version": 1, "providers": {"ollama": []}}', "providers.ollama", "got []"),
        (
            '{"version": 1, "providers": {"ollama": {"enabled": true, "extra": 1}}}',
            "providers.ollama.extra",
            "got ['extra']",
        ),
        (
            '{"version": 1, "providers": {"ollama": {"enabled": 1}}}',
            "providers.ollama.enabled",
            "got 1",
        ),
        (
            '{"version": 1, "providers": {"ollama": {"model": 7}}}',
            "providers.ollama.model",
            "got 7",
        ),
        (
            '{"version": 1, "providers": {"ollama": {"model": ""}}}',
            "providers.ollama.model",
            "got ''",
        ),
        (
            '{"version": 1, "providers": {"ollama": {"model": "--unsafe"}}}',
            "providers.ollama.model",
            "got '--unsafe'",
        ),
    ],
)
def test_invalid_config_contract_is_rejected(tmp_path, monkeypatch, contents, field, invalid_value):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(UsageError, match=rf"config.json.*{field}") as excinfo:
        providerconfig.load(["ollama"])
    if invalid_value is not None:
        assert invalid_value in str(excinfo.value)


def test_invalid_model_and_unknown_provider_updates_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(UsageError, match=r"provider.*got 'other'"):
        providerconfig.set_enabled("other", False, known={"ollama"})
    with pytest.raises(UsageError, match=r"model.*got '--unsafe'"):
        providerconfig.set_model("ollama", "--unsafe", known={"ollama"})


def test_clear_model_preserves_enabled_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    providerconfig.set_enabled("ollama", False, known={"ollama"})
    providerconfig.set_model("ollama", "qwen3:0.6b", known={"ollama"})
    providerconfig.set_model("ollama", None, known={"ollama"})
    assert providerconfig.load(["ollama"]).setting("ollama") == providerconfig.ProviderSetting(
        enabled=False, model=None
    )


def test_failed_temporary_replacement_preserves_previous_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    providerconfig.set_model("ollama", "qwen3:0.6b", known={"ollama"})
    path = providerconfig.config_path()
    previous = path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("mock replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(UsageError, match=r"config.json.*mock replacement failure"):
            providerconfig.set_enabled("ollama", False, known={"ollama"})

    assert path.read_bytes() == previous
    assert providerconfig.load(["ollama"]).setting("ollama") == providerconfig.ProviderSetting(
        enabled=True, model="qwen3:0.6b"
    )
    assert list(path.parent.glob("*.tmp")) == []


def test_config_path_honors_explicit_environment_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient"))
    explicit = tmp_path / "explicit"
    assert providerconfig.config_path({"XDG_CONFIG_HOME": str(explicit)}) == (
        explicit / "adversarial-friends" / "config.json"
    )


def test_config_path_expands_tilde_in_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert providerconfig.config_path({"XDG_CONFIG_HOME": "~/custom-config"}) == (
        tmp_path / "home" / "custom-config" / "adversarial-friends" / "config.json"
    )


def test_relative_xdg_config_home_falls_back_to_user_config(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert providerconfig.config_path({"XDG_CONFIG_HOME": ".repo-config"}) == (
        tmp_path / "home" / ".config" / "adversarial-friends" / "config.json"
    )


def test_config_path_falls_back_to_user_config_not_repository(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path / "home")
    assert providerconfig.config_path() == (
        tmp_path / "home" / ".config" / "adversarial-friends" / "config.json"
    )


def test_read_os_error_names_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    def fail_read(*_args, **_kwargs):
        raise PermissionError("mock permission failure")

    monkeypatch.setattr(providerconfig, "read_bounded_bytes", fail_read)
    with pytest.raises(UsageError, match=r"config.json.*mock permission failure"):
        providerconfig.load(["ollama"])


def test_invalid_utf8_is_reported_as_invalid_provider_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(UsageError, match=r"config.json.*invalid provider configuration"):
        providerconfig.load(["ollama"])


def test_provider_config_is_bounded_before_json_decode(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(UsageError, match=r"config.json.*262144-byte limit"):
        providerconfig.load(["ollama"])


def test_provider_config_never_follows_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    path = providerconfig.config_path()
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 1, "providers": {}}', encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(UsageError, match=r"config.json.*regular file"):
        providerconfig.load(["ollama"])

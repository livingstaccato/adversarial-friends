"""Explicit friends override selection policy, never dispatchability."""

from e2e_helpers import run_af
import pytest

from adversarial_friends import adapters, cli, readiness
from adversarial_friends.commands import friends as friends_module, setup as run_setup_module
from adversarial_friends.errors import NoFriendsError
from adversarial_friends.paths import ADAPTER_DIR
from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting


def _artifact(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# contract\n")
    return artifact


def test_missing_explicit_subprocess_is_refused_before_run_directory_creation(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "codex:ops")
    assert result.returncode == 3
    assert "executable 'codex' was not found" in result.stderr
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "friend, reachable, message",
    [
        ("ollama:ops", True, "no model is configured"),
        ("ollama:ops:qwen3:0.6b", False, "endpoint is unreachable"),
    ],
)
def test_explicit_http_readiness_is_refused_before_run_directory_creation(
    monkeypatch, tmp_path, friend, reachable, message
):
    ollama = adapters.load_adapters(ADAPTER_DIR)["ollama"]
    monkeypatch.setattr(run_setup_module, "load_adapters", lambda _path: {"ollama": ollama})
    monkeypatch.setattr(readiness.http_transport, "probe", lambda _endpoint: reachable)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = cli.build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            friend,
        ]
    )

    with pytest.raises(NoFriendsError, match=message):
        cli.cmd_run(args)
    assert not (tmp_path / "runs").exists()


def test_explicit_http_uses_configured_model_while_bypassing_disabled(monkeypatch, tmp_path):
    registry = adapters.load_adapters(ADAPTER_DIR)
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {"ollama": ProviderSetting(enabled=False, model="qwen3:0.6b")}
        ),
    )
    monkeypatch.setattr(readiness.http_transport, "probe", lambda _endpoint: True)
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = cli.build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--friend", "ollama:ops"]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [(spec.cli, spec.model) for spec in resolved.specs] == [("ollama", "qwen3:0.6b")]


def test_max_friends_drops_explicit_friend_before_readiness_probe(monkeypatch, tmp_path):
    registry = adapters.load_adapters(ADAPTER_DIR)
    probes: list[str] = []
    monkeypatch.setattr(friends_module.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        readiness.http_transport,
        "probe",
        lambda endpoint: probes.append(endpoint) or False,
    )
    args = cli.build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--friend",
            "codex:ops",
            "--friend",
            "ollama:ops:qwen3:0.6b",
            "--max-friends",
            "1",
        ]
    )
    downgrades: list[str] = []

    resolved = friends_module.resolve_friends(args, registry, None, downgrades)

    assert [spec.cli for spec in resolved.specs] == ["codex"]
    assert probes == []
    assert any("dropped" in note and "ollama" in note for note in downgrades)

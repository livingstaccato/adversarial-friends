"""Explicit friends override selection policy, never dispatchability."""

from e2e_helpers import run_af
import pytest

from afriend import adapters, cli, readiness
from afriend.commands import friends as friends_module, setup as run_setup_module
from afriend.errors import NoFriendsError
from afriend.paths import ADAPTER_DIR
from afriend.providerconfig import ProviderPolicy, ProviderSetting


@pytest.fixture(autouse=True)
def _verified_deny_probe(monkeypatch):
    monkeypatch.setattr(
        readiness,
        "probe_deny_argv",
        lambda *_args: readiness.DenyProbeResult(True, "verified test shim"),
    )


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


def test_max_friends_applies_to_ready_explicit_friends_not_unavailable_prefix(
    monkeypatch, tmp_path
):
    registry = adapters.load_adapters(ADAPTER_DIR)
    checks: list[str] = []
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda binary: checks.append(binary) or ("/bin/claude" if binary == "claude" else None),
    )
    args = cli.build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--friend",
            "codex:ops",
            "--friend",
            "claude:security",
            "--max-friends",
            "1",
        ]
    )
    downgrades: list[str] = []

    resolved = friends_module.resolve_friends(args, registry, None, downgrades)

    assert [spec.cli for spec in resolved.specs] == ["claude"]
    assert checks.count("codex") == 1
    assert checks.count("claude") == 1
    assert any("codex" in note and "not found" in note for note in downgrades)


def test_explicit_capacity_preserves_ready_order_and_probes_each_provider_once(
    monkeypatch, tmp_path
):
    registry = adapters.load_adapters(ADAPTER_DIR)
    checks: list[str] = []
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda binary: checks.append(binary) or f"/bin/{binary}",
    )
    args = cli.build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--friend",
            "claude:ops",
            "--friend",
            "codex:security",
            "--friend",
            "claude:testability",
            "--max-friends",
            "2",
        ]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [(spec.cli, spec.lens) for spec in resolved.specs] == [
        ("claude", "ops"),
        ("codex", "security"),
    ]
    assert checks.count("claude") == 1
    assert checks.count("codex") == 1

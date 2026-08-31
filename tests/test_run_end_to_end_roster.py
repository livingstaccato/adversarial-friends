"""End-to-end `--roster`, `--preset`, and `afriend init` (spec §10.1, §13, §17).

Most tests assert on roster resolution rather than spending a real agent
call. Roster entries now pass through canonical readiness, so unit-level
tests inject executable/endpoint availability explicitly; subprocess tests
retain the safe PATH that prevents accidental metered calls.
"""

import json
from pathlib import Path
import subprocess
import sys

from e2e_helpers import AF, _env, run_af
import pytest

ROSTER = """
[[friend]]
name = "codex-ops"
cli = "codex"
lens = "ops"
scope = "doc"
"""
ADAPTER_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "adversarial_friends" / "assets" / "adapters"
)


def _selection_fixture(monkeypatch, *, enabled, executables=(), models=None):
    from adversarial_friends import adapters
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(ADAPTER_DIR)
    configured_models = models or {}
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {
                name: ProviderSetting(
                    enabled=name in enabled,
                    model=configured_models.get(name),
                )
                for name in registry
            }
        ),
    )
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in executables else None,
    )
    return registry, friends_module


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n")
    return path


def _run_json(tmp_path):
    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    return json.loads((run_dir / "run.json").read_text())


def _roster(tmp_path, text=ROSTER):
    path = tmp_path / "roster.toml"
    path.write_text(text)
    return path


# --- --roster --------------------------------------------------------------


def test_a_roster_file_replaces_discovery(monkeypatch, tmp_path):
    from adversarial_friends.cliargs import build_parser

    registry, friends_module = _selection_fixture(
        monkeypatch, enabled={"codex"}, executables={"codex"}
    )
    roster = _roster(tmp_path)
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--roster", str(roster), "--include-self"]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [spec.name for spec in resolved.specs] == ["codex-ops"]
    assert resolved.source == str(roster)


def test_friend_flags_beat_a_roster(tmp_path):
    """§10.1's precedence, strongest last: --friend is the invocation flag
    and outranks a roster file."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--roster",
        str(_roster(tmp_path)),
        "--friend",
        "fake:cwd_probe",
    )
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert [f["name"] for f in meta["friends"]] == ["fake-cwd_probe-0"]
    assert any("--friend replaces the roster" in d for d in meta["downgrades"])


def test_a_missing_roster_is_a_usage_error(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(tmp_path / "nope.toml"))
    assert result.returncode == 2
    assert "not found" in result.stderr


def test_a_roster_naming_an_unknown_cli_is_refused(tmp_path):
    bad = _roster(tmp_path, '[[friend]]\nname = "x"\ncli = "nope"\nlens = "ops"\n')
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(bad))
    assert result.returncode != 0
    assert "nope" in result.stderr


def test_a_roster_cannot_smuggle_arbitrary_flags(tmp_path):
    """§13: a roster supplies values only, for a fixed set of keys. There is
    no mechanism for a file to inject a flag."""
    bad = _roster(
        tmp_path,
        '[[friend]]\nname = "x"\ncli = "codex"\nlens = "ops"\nextra_args = "--yolo"\n',
    )
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(bad))
    assert result.returncode == 2
    assert "extra_args" in result.stderr


def test_a_repo_local_roster_is_not_picked_up_on_its_own(tmp_path, monkeypatch):
    """§13: repo-local `.adversarial-friends/` is untrusted. A cloned repo
    must not be able to choose who reviews it."""
    hostile = tmp_path / ".adversarial-friends"
    hostile.mkdir()
    (hostile / "roster.toml").write_text(ROSTER)
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        env_extra={"XDG_CONFIG_HOME": str(tmp_path / "empty")},
    )
    assert result.returncode == 0, result.stderr
    assert _run_json(tmp_path)["roster_source"] is None


def test_the_user_config_roster_is_picked_up(monkeypatch, tmp_path):
    """The trusted half of §13: this is the operator's own machine-wide
    configuration, and using it is the point of writing one."""
    config = tmp_path / "config" / "adversarial-friends"
    config.mkdir(parents=True)
    (config / "roster.toml").write_text(ROSTER)
    from adversarial_friends.cliargs import build_parser

    registry, friends_module = _selection_fixture(
        monkeypatch, enabled={"codex"}, executables={"codex"}
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(["run", str(_artifact(tmp_path)), "--include-self"])

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [spec.name for spec in resolved.specs] == ["codex-ops"]
    assert resolved.source == str(config / "roster.toml")


@pytest.mark.parametrize("automatic", [False, True], ids=["explicit", "user-config"])
def test_roster_files_filter_disabled_providers_before_dispatch(monkeypatch, tmp_path, automatic):
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.errors import NoFriendsError

    registry, friends_module = _selection_fixture(
        monkeypatch,
        enabled={"codex"} if not automatic else set(),
    )
    if automatic:
        config = tmp_path / "config"
        roster = config / "adversarial-friends" / "roster.toml"
        roster.parent.mkdir(parents=True)
        roster.write_text(ROSTER)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        argv = ["run", str(_artifact(tmp_path))]
    else:
        roster = _roster(tmp_path)
        argv = [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--disable-provider",
            "codex",
        ]
    executable_probes: list[str] = []
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: executable_probes.append(name) or f"/bin/{name}",
    )
    args = build_parser().parse_args(argv)

    with pytest.raises(NoFriendsError, match=r"codex-ops.*disabled by provider policy"):
        friends_module.resolve_friends(args, registry, None, [])

    assert executable_probes == []


def test_roster_file_excludes_detected_host_unless_include_self(monkeypatch, tmp_path):
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.errors import NoFriendsError
    from adversarial_friends.readiness import HOST_ENV_MARKERS

    registry, friends_module = _selection_fixture(
        monkeypatch, enabled={"codex"}, executables={"codex"}
    )
    roster = _roster(tmp_path)
    for marker in HOST_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")

    excluded = build_parser().parse_args(["run", str(_artifact(tmp_path)), "--roster", str(roster)])
    with pytest.raises(NoFriendsError, match=r"codex-ops.*detected host provider"):
        friends_module.resolve_friends(excluded, registry, None, [])

    included = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--roster", str(roster), "--include-self"]
    )
    resolved = friends_module.resolve_friends(included, registry, None, [])
    assert [spec.name for spec in resolved.specs] == ["codex-ops"]


def test_unready_roster_entries_do_not_consume_capacity_or_trigger_duplicate_probes(
    monkeypatch, tmp_path
):
    from adversarial_friends import readiness as readiness_module
    from adversarial_friends.cliargs import build_parser

    selected = {"codex", "ollama", "opencode"}
    registry, friends_module = _selection_fixture(
        monkeypatch, enabled=selected, executables={"opencode"}
    )
    roster = _roster(
        tmp_path,
        '[[friend]]\nname = "missing"\ncli = "codex"\nlens = "ops"\n'
        '[[friend]]\nname = "model-less"\ncli = "ollama"\nlens = "security"\n'
        '[[friend]]\nname = "model-less-two"\ncli = "ollama"\nlens = "testability"\n'
        '[[friend]]\nname = "survivor"\ncli = "opencode"\nlens = "assumptions"\n',
    )
    probes: list[str] = []
    monkeypatch.setattr(
        readiness_module.http_transport,
        "probe",
        lambda endpoint: probes.append(endpoint) or True,
    )
    monkeypatch.delenv("AF_NO_HTTP_DISCOVERY", raising=False)
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--include-self",
            "--max-friends",
            "1",
        ]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [spec.name for spec in resolved.specs] == ["survivor"]
    assert probes == [registry["ollama"].endpoint]


def test_roster_file_projects_configured_http_model(monkeypatch, tmp_path):
    from adversarial_friends import readiness as readiness_module
    from adversarial_friends.cliargs import build_parser

    registry, friends_module = _selection_fixture(
        monkeypatch,
        enabled={"ollama"},
        models={"ollama": "qwen3:0.6b"},
    )
    roster = _roster(
        tmp_path,
        '[[friend]]\nname = "local-model"\ncli = "ollama"\nlens = "ops"\n',
    )
    monkeypatch.setattr(readiness_module.http_transport, "probe", lambda _endpoint: True)
    monkeypatch.delenv("AF_NO_HTTP_DISCOVERY", raising=False)
    args = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--roster", str(roster), "--include-self"]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [(spec.cli, spec.model) for spec in resolved.specs] == [("ollama", "qwen3:0.6b")]


# --- afriend init ----------------------------------------------------------


def _init(tmp_path, *extra):
    return subprocess.run(
        [sys.executable, str(AF), "init", "--out", str(tmp_path / "roster.toml"), *extra],
        capture_output=True,
        text=True,
        env=_env(),
    )


def test_init_writes_a_roster_from_what_is_installed(tmp_path):
    """The safe PATH contains only git, so nothing is discoverable -- which
    is the honest outcome to report, not an empty file."""
    result = _init(tmp_path)
    assert result.returncode == 3
    assert "no agent CLIs found" in result.stderr


def test_init_refuses_to_clobber_without_force(tmp_path):
    """It is a file you are meant to edit by hand."""
    (tmp_path / "roster.toml").write_text("# mine\n")
    result = _init(tmp_path)
    assert result.returncode == 2
    assert "--force" in result.stderr
    assert (tmp_path / "roster.toml").read_text() == "# mine\n"


def test_init_does_not_probe_disabled_http_provider(monkeypatch, tmp_path):
    from adversarial_friends import providerconfig, readiness as readiness_module
    from adversarial_friends.commands import init as init_module
    from adversarial_friends.errors import NoFriendsError
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = init_module.load_adapters(init_module.ADAPTER_DIR)
    monkeypatch.setattr(
        providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=False) for name in registry}
        ),
    )
    probes: list[str] = []
    monkeypatch.setattr(
        readiness_module.http_transport,
        "probe",
        lambda endpoint: probes.append(endpoint) or True,
    )
    target = tmp_path / "roster.toml"
    args = type("Args", (), {"out": str(target), "force": False})()

    with pytest.raises(NoFriendsError):
        init_module.cmd_init(args)

    assert probes == []
    assert not target.exists()


def test_init_uses_configured_http_model(monkeypatch, tmp_path):
    from adversarial_friends import providerconfig, readiness as readiness_module
    from adversarial_friends.commands import init as init_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = init_module.load_adapters(init_module.ADAPTER_DIR)
    monkeypatch.setattr(
        providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {
                name: ProviderSetting(
                    enabled=name == "ollama",
                    model="qwen3:0.6b" if name == "ollama" else None,
                )
                for name in registry
            }
        ),
    )
    monkeypatch.setattr(readiness_module.http_transport, "probe", lambda _endpoint: True)
    monkeypatch.delenv("AF_NO_HTTP_DISCOVERY", raising=False)
    target = tmp_path / "roster.toml"
    args = type("Args", (), {"out": str(target), "force": False})()

    init_module.cmd_init(args)

    text = target.read_text()
    assert 'cli = "ollama"' in text
    assert 'model = "qwen3:0.6b"' in text
    assert "CHANGE-ME" not in text


def test_init_excludes_detected_host_provider(monkeypatch, tmp_path):
    from adversarial_friends import providerconfig
    from adversarial_friends.commands import init as init_module
    from adversarial_friends.errors import NoFriendsError
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = init_module.load_adapters(init_module.ADAPTER_DIR)
    monkeypatch.setattr(
        providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name == "codex") for name in registry}
        ),
    )
    monkeypatch.setattr(
        init_module.shutil,
        "which",
        lambda name: "/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    target = tmp_path / "roster.toml"
    args = type("Args", (), {"out": str(target), "force": False})()

    with pytest.raises(NoFriendsError):
        init_module.cmd_init(args)

    assert not target.exists()


def test_init_projects_only_eligible_canonical_readiness_states(monkeypatch, tmp_path):
    from adversarial_friends.commands import init as init_module
    from adversarial_friends.readiness import FriendReadiness, ReadinessState

    rows = {
        "codex": FriendReadiness("codex", ReadinessState.READY, "available", "/bin/codex", None),
        "opencode": FriendReadiness(
            "opencode",
            ReadinessState.POLICY_BLOCKED,
            "blocked by policy",
            "/bin/opencode",
            None,
        ),
    }
    monkeypatch.setattr(init_module, "assess_all", lambda *_args, **_kwargs: rows, raising=False)
    monkeypatch.setattr(
        init_module.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in rows else None,
    )
    target = tmp_path / "roster.toml"
    args = type("Args", (), {"out": str(target), "force": False})()

    init_module.cmd_init(args)

    text = target.read_text()
    assert 'cli = "codex"' in text
    assert 'cli = "opencode"' not in text


# --- --preset --------------------------------------------------------------


def test_a_preset_is_recorded_as_used(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--preset", "cheap")
    assert result.returncode == 0, result.stderr
    assert _run_json(tmp_path)["preset"] == "cheap"


def test_a_roster_effort_beats_the_preset(monkeypatch, tmp_path):
    """§10.1: roster outranks preset. The preset fills only what nothing
    stronger set, which is what makes it weaker rather than merely
    different."""
    roster = _roster(
        tmp_path,
        '[[friend]]\nname = "codex-ops"\ncli = "codex"\nlens = "ops"\neffort = "medium"\n',
    )
    from adversarial_friends.cliargs import build_parser

    registry, friends_module = _selection_fixture(
        monkeypatch, enabled={"codex"}, executables={"codex"}
    )
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--preset",
            "thorough",
            "--include-self",
        ]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert resolved.specs[0].effort == "medium"


def test_capacity_is_applied_before_discarded_friend_preset_diagnostics(monkeypatch, tmp_path):
    from adversarial_friends.cliargs import build_parser

    registry, friends_module = _selection_fixture(
        monkeypatch,
        enabled={"codex", "opencode"},
        executables={"codex", "opencode"},
    )
    roster = _roster(
        tmp_path,
        '[[friend]]\nname = "selected"\ncli = "codex"\nlens = "ops"\n'
        '[[friend]]\nname = "discarded"\ncli = "opencode"\nlens = "security"\n',
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--preset",
            "thorough",
            "--max-friends",
            "1",
            "--include-self",
        ]
    )
    downgrades: list[str] = []

    resolved = friends_module.resolve_friends(args, registry, None, downgrades)

    assert [spec.name for spec in resolved.specs] == ["selected"]
    assert any("--max-friends=1 dropped" in note for note in downgrades)
    assert not any("opencode reports effort as unverified" in note for note in downgrades)


def test_init_does_not_claim_an_http_friend_is_sandboxed(tmp_path, monkeypatch):
    """Found by running `afriend init` on a real machine: ollama was
    described as running "under OS confinement", which never engages for it.

    An HTTP friend is a bare model behind an endpoint -- no subprocess, no
    filesystem access -- so dispatch returns before the sandbox is even
    considered. Describing a mechanism that never runs is worse than saying
    nothing, in a file the operator is meant to read and trust.
    """
    from adversarial_friends import providerconfig, readiness as readiness_module
    from adversarial_friends.commands import init as init_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = init_module.load_adapters(init_module.ADAPTER_DIR)
    monkeypatch.setattr(
        providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name == "ollama") for name in registry}
        ),
    )
    monkeypatch.setattr(readiness_module.http_transport, "probe", lambda _endpoint: True)
    monkeypatch.delenv("AF_NO_HTTP_DISCOVERY", raising=False)
    target = tmp_path / "roster.toml"
    args = type("Args", (), {"out": str(target), "force": False})()
    init_module.cmd_init(args)

    text = target.read_text()
    assert "ollama" in text
    # The specific false claim, not the word: the corrected note says it
    # "needs no confinement", which is the opposite and must survive.
    assert "runs under OS confinement" not in text
    assert "no filesystem access" in text
    assert registry["ollama"].transport == "http"

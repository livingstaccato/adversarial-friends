"""End-to-end coverage for §17's remaining flags."""

import json
import subprocess
import sys

from e2e_helpers import AF, _env, run_af
import pytest


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def test_default_external_tool_policy_is_recorded_for_fake_dispatch(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["external_tool_policy"] == "deny"
    assert {row["external_tools"] for row in meta["friends"]} == {"not-applicable"}


def test_allow_external_tools_is_explicit_and_recorded(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--allow-external-tools",
    )
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["external_tool_policy"] == "allow"
    assert {row["external_tools"] for row in meta["friends"]} == {"not-applicable"}


def test_explicit_uncontrolled_friend_is_refused_before_run_directory_creation(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "agy:ops")
    assert result.returncode == 2
    assert "cannot deny external tools" in result.stderr
    assert "--allow-external-tools" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_explicit_roster_cannot_bypass_external_tool_authority(tmp_path):
    roster = tmp_path / "roster.toml"
    roster.write_text('[[friend]]\nname="agy-ops"\ncli="agy"\nlens="ops"\n')
    result = run_af(tmp_path, _artifact(tmp_path), "--roster", str(roster))
    assert result.returncode == 2
    assert "agy cannot deny external tools" in result.stderr
    assert not (tmp_path / "runs").exists()


# --- --json ----------------------------------------------------------------


def test_run_json_prints_the_metadata(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--json")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["mode"] == "report"
    assert parsed["friends"]


def test_without_json_the_path_is_still_what_is_printed(tmp_path):
    """A shell pipeline wants the path; --json is for a caller that would
    otherwise have to go read run.json itself."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.stdout.strip() == str(_run_dir(tmp_path))


def test_doctor_json_is_machine_readable(tmp_path):
    result = subprocess.run(
        [sys.executable, str(AF), "doctor", "--json"], capture_output=True, text=True, env=_env()
    )
    parsed = json.loads(result.stdout)
    assert isinstance(parsed["friends"], list)
    assert all("auth_classifiable" in row for row in parsed["friends"])
    assert all({"state", "reason", "where", "model"} <= row.keys() for row in parsed["friends"])
    assert parsed["usable"] == sum(row["state"] == "ready" for row in parsed["friends"])


def test_doctor_text_uses_the_same_readiness_state_and_reason(tmp_path):
    result = subprocess.run(
        [sys.executable, str(AF), "doctor"], capture_output=True, text=True, env=_env()
    )
    assert "state=" in result.stdout
    assert "reason=" in result.stdout


def test_doctor_preserves_http_discovery_opt_out(monkeypatch, capsys):
    import argparse

    from adversarial_friends.commands import doctor as doctor_module

    probes: list[str] = []
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        doctor_module.http_transport,
        "probe",
        lambda endpoint: probes.append(endpoint) or True,
    )

    code = doctor_module.cmd_doctor(argparse.Namespace(json=True, gc=False, out=None))
    payload = json.loads(capsys.readouterr().out)
    ollama = next(row for row in payload["friends"] if row["name"] == "ollama")

    assert code == 3
    assert probes == []
    assert ollama["state"] == "disabled"
    assert "AF_NO_HTTP_DISCOVERY" in ollama["reason"]
    assert payload["usable"] == 0


@pytest.mark.parametrize("provider", ["agy", "opencode"])
@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_reports_disabled_uncontrolled_provider_without_enforcing_or_building(
    monkeypatch, capsys, provider, json_output
):
    import argparse

    from adversarial_friends import adapters
    from adversarial_friends.commands import doctor as doctor_module
    from adversarial_friends.paths import ADAPTER_DIR
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    adapter = adapters.load_adapters(ADAPTER_DIR)[provider]
    monkeypatch.setattr(doctor_module, "load_adapters", lambda _path: {provider: adapter})
    monkeypatch.setattr(
        doctor_module.providerconfig,
        "load",
        lambda _registry: ProviderPolicy({provider: ProviderSetting(enabled=False)}),
    )
    monkeypatch.setattr(
        doctor_module,
        "build_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled provider reached argv construction")
        ),
    )
    monkeypatch.setattr(
        doctor_module,
        "enforce",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled provider reached authority enforcement")
        ),
    )
    monkeypatch.setattr(
        doctor_module.shutil,
        "which",
        lambda _binary: (_ for _ in ()).throw(
            AssertionError("disabled provider reached executable probe")
        ),
    )

    code = doctor_module.cmd_doctor(argparse.Namespace(json=json_output, gc=False, out=None))
    output = capsys.readouterr().out

    assert code == 3
    if json_output:
        row = json.loads(output)["friends"][0]
        assert row["name"] == provider
        assert row["state"] == "disabled"
        assert row["reason"] == "disabled by provider policy"
        assert row["external_tools"] == "uncontrolled"
    else:
        assert provider in output
        assert "state=disabled" in output
        assert "reason=disabled by provider policy" in output
        assert "external_tools=uncontrolled" in output


def test_same_provider_cannot_be_enabled_and_disabled_for_one_run(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--enable-provider",
        "codex",
        "--disable-provider",
        "codex",
    )
    assert result.returncode == 2
    assert "both --enable-provider and --disable-provider" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_explicit_friend_bypasses_disabled_and_host_excluded_provider(monkeypatch, tmp_path):
    from adversarial_friends import adapters
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy({"codex": ProviderSetting(enabled=False)}),
    )
    monkeypatch.setenv("CODEX_SESSION_ID", "session")
    monkeypatch.setattr(friends_module.shutil, "which", lambda binary: f"/bin/{binary}")
    args = build_parser().parse_args(["run", str(_artifact(tmp_path)), "--friend", "codex:ops"])
    resolved = friends_module.resolve_friends(args, registry, None, [])
    assert [spec.cli for spec in resolved.specs] == ["codex"]


def test_per_run_enable_overrides_persistently_disabled_provider(monkeypatch, tmp_path):
    from adversarial_friends import adapters
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name != "codex") for name in registry}
        ),
    )
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: f"/bin/{name}" if name == "codex" else None,
    )
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--enable-provider",
            "codex",
            "--include-self",
        ]
    )
    resolved = friends_module.resolve_friends(args, registry, None, [])
    assert [spec.cli for spec in resolved.specs] == ["codex"]


def test_per_run_disable_overrides_persistently_enabled_provider(monkeypatch, tmp_path):
    from adversarial_friends import adapters
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.errors import NoFriendsError
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name == "codex") for name in registry}
        ),
    )
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: f"/bin/{name}" if name == "codex" else None,
    )
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--disable-provider", "codex"]
    )
    with pytest.raises(NoFriendsError):
        friends_module.resolve_friends(args, registry, None, [])


def test_frozen_resume_roster_still_validates_restored_provider_controls(monkeypatch):
    from adversarial_friends.adapters import FriendSpec
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.errors import UsageError

    frozen = FriendSpec(
        name="codex-ops",
        cli="codex",
        lens="ops",
        model=None,
        effort=None,
        scope="repo",
        timeout=900,
    )
    args = build_parser().parse_args(["run", "spec.md", "--mode", "report"])
    args._resume_roster = [frozen]
    args._resume_meta = {"roster_source": None}
    args.enable_provider = ["codex"]
    args.disable_provider = ["codex"]

    monkeypatch.setattr(
        friends_module,
        "resolve_friends",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resume attempted fresh provider resolution")
        ),
    )

    with pytest.raises(UsageError, match="both --enable-provider and --disable-provider"):
        friends_module.roster_for_run(args, {}, None, [])


# --- --model / --effort (§10.1 layer 4) ------------------------------------


def test_model_and_effort_override_everything(monkeypatch, tmp_path):
    """Invocation flags are §10.1's strongest layer -- they outrank a roster
    entry's own values, which is what makes them layer 4 rather than
    another way of spelling the same thing."""
    roster = tmp_path / "roster.toml"
    roster.write_text(
        '[[friend]]\nname = "codex-ops"\ncli = "codex"\nlens = "ops"\n'
        'model = "from-roster"\neffort = "low"\n'
    )
    from adversarial_friends import adapters
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name == "codex") for name in registry}
        ),
    )
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: "/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--model",
            "from-flag",
            "--effort",
            "high",
            "--include-self",
        ]
    )

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert resolved.specs[0].model == "from-flag"
    assert resolved.specs[0].effort == "high"


def test_global_model_uses_the_friend_model_allowlist(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--model=--settings",
    )
    assert result.returncode == 2
    assert "invalid model" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_global_model_makes_reachable_http_provider_discoverable(monkeypatch, tmp_path):
    from adversarial_friends import adapters, readiness as readiness_module
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name == "ollama") for name in registry}
        ),
    )
    monkeypatch.setattr(friends_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(readiness_module.http_transport, "probe", lambda _endpoint: True)
    monkeypatch.delenv("AF_NO_HTTP_DISCOVERY", raising=False)
    args = build_parser().parse_args(["run", str(_artifact(tmp_path)), "--model", "qwen3:0.6b"])

    resolved = friends_module.resolve_friends(args, registry, None, [])

    assert [(spec.cli, spec.model) for spec in resolved.specs] == [("ollama", "qwen3:0.6b")]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "0"),
        ("--max-friends", "0"),
        ("--max-calls", "0"),
        ("--max-wall-clock", "0"),
        ("--max-loop-iterations", "0"),
        ("--require-friends", "0"),
        ("--max-rounds", "0"),
    ],
)
def test_positive_run_limits_are_validated_before_dispatch(tmp_path, flag, value):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        flag,
        value,
    )
    assert result.returncode == 2
    assert "positive integer" in result.stderr or "at least 1" in result.stderr
    assert not (tmp_path / "runs").exists()


# --- --lens / --max-friends ------------------------------------------------


def test_an_unknown_lens_is_refused(tmp_path):
    """A typo would otherwise quietly shrink the run to whichever lenses
    happened to match."""
    result = run_af(tmp_path, _artifact(tmp_path), "--lens", "not-a-lens")
    assert result.returncode == 2
    assert "unknown lens" in result.stderr


def test_max_friends_caps_and_says_so(monkeypatch, tmp_path):
    """A silently shortened roster is a run with fewer independent judges
    than the operator thinks it has."""
    roster = tmp_path / "roster.toml"
    roster.write_text(
        '[[friend]]\nname = "a"\ncli = "codex"\nlens = "ops"\n'
        '[[friend]]\nname = "b"\ncli = "claude"\nlens = "security"\n'
    )
    from adversarial_friends import adapters
    from adversarial_friends.cliargs import build_parser
    from adversarial_friends.commands import friends as friends_module
    from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting

    registry = adapters.load_adapters(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "adversarial_friends"
        / "assets"
        / "adapters"
    )
    selected = {"codex", "claude"}
    monkeypatch.setattr(
        friends_module.providerconfig,
        "load",
        lambda *_args, **_kwargs: ProviderPolicy(
            {name: ProviderSetting(enabled=name in selected) for name in registry}
        ),
    )
    monkeypatch.setattr(
        friends_module.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in selected else None,
    )
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    args = build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--roster",
            str(roster),
            "--max-friends",
            "1",
            "--include-self",
        ]
    )
    downgrades: list[str] = []

    resolved = friends_module.resolve_friends(args, registry, None, downgrades)

    assert len(resolved.specs) == 1
    assert any("--max-friends=1 dropped" in note for note in downgrades)


# --- --require-friends -------------------------------------------------------


def test_require_friends_fails_a_run_below_quorum(tmp_path):
    """c-0013. Without this, one friend answering out of a large roster
    exits 0 identically to every friend answering -- the report already
    says plainly that a single answer is one opinion rather than
    disagreement between several, but the exit code carried none of that,
    so a CI wrapper reading only the exit code could not tell the two
    apart."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--friend",
        "fake:crash",
        "--require-friends",
        "2",
    )
    assert result.returncode == 12, result.stderr
    assert "only 1 of 2 required friends" in result.stderr


def test_require_friends_passes_a_run_at_or_above_quorum(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--friend",
        "fake:good",
        "--require-friends",
        "2",
    )
    assert result.returncode == 0, result.stderr


def test_require_friends_is_unenforced_when_unset(tmp_path):
    """Opt-in: a fresh checkout with one CLI installed is a normal use of
    this tool, not a degraded one. A default floor would fail that case for
    no reason."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr


# --- --keep ----------------------------------------------------------------


def test_keep_leaves_the_isolation_directory_behind(tmp_path):
    """§12.4. A "kept" worktree at a path that no longer exists would be
    worse than not keeping it, so isolation moves into the run directory,
    which persists."""
    run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:cwd_probe", "--keep")
    kept = _run_dir(tmp_path) / "isolation" / "round-1"
    assert kept.is_dir()
    assert any(kept.iterdir())


def test_without_keep_nothing_survives(tmp_path):
    run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:cwd_probe")
    assert not (_run_dir(tmp_path) / "isolation").exists()


# --- --unsafe-extra-args (§13) ---------------------------------------------


def test_unsafe_extra_args_requires_the_acknowledgement(tmp_path):
    result = run_af(
        tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--unsafe-extra-args", "--verbose"
    )
    assert result.returncode == 2
    assert "--i-accept-unsandboxed" in result.stderr


def test_allow_external_tools_does_not_waive_extra_args_acknowledgement(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--unsafe-extra-args",
        "--verbose",
        "--allow-external-tools",
    )
    assert result.returncode == 2
    assert "--i-accept-unsandboxed" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_a_denied_flag_is_refused_even_with_the_acknowledgement(tmp_path):
    """An escape hatch for "I need one more option" is not an escape hatch
    for "run with no guardrails at all"."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        # The = form: argparse only accepts a dash-leading value when it
        # contains a space, so this is the spelling that always works.
        "--unsafe-extra-args=--yolo",
        "--i-accept-unsandboxed",
    )
    assert result.returncode == 2
    assert "disables approval" in result.stderr


def test_default_denial_refuses_unvalidated_extra_args_before_run_directory(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--unsafe-extra-args",
        "--tools Read --mcp-config evil.json",
        "--i-accept-unsandboxed",
    )
    assert result.returncode == 2
    assert "--allow-external-tools" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_allowed_extra_args_are_recorded_as_a_downgrade(tmp_path):
    """A run carrying unvalidated flags has weaker guarantees than its
    friend table implies, so the report has to say so."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--unsafe-extra-args",
        "--verbose --colour never",
        "--i-accept-unsandboxed",
        "--allow-external-tools",
    )
    assert result.returncode == 0, result.stderr
    downgrades = " ".join(_run_json(tmp_path)["downgrades"])
    assert "--unsafe-extra-args" in downgrades
    assert "read-only is reported as False" in downgrades


# --- doctor --gc -----------------------------------------------------------


def test_gc_removes_an_abandoned_run(tmp_path):
    """A run directory with no report.md means the process died before
    finishing -- every path out of cmd_run writes one."""
    runs = tmp_path / "runs"
    abandoned = runs / "run-20260101T000000-deadbeef"
    abandoned.mkdir(parents=True)
    (abandoned / "claims.jsonl").write_text("")
    result = subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert not abandoned.exists(), result.stderr


def test_gc_keeps_a_finished_run(tmp_path):
    runs = tmp_path / "runs"
    finished = runs / "run-20260101T000000-cafe0000"
    finished.mkdir(parents=True)
    (finished / "report.md").write_text("# report\n")
    subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert finished.exists()


def test_gc_keeps_a_run_halted_for_the_orchestrator(tmp_path):
    """It is waiting for a RESPONSE.json, not abandoned -- and the halt path
    writes a report precisely so it survives this."""
    runs = tmp_path / "runs"
    run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--merge",
        "orchestrator",
    )
    halted = _run_dir(tmp_path)
    subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert halted.exists()


def test_a_downgrade_is_recorded_once_not_twice(tmp_path):
    """Every downgrade must appear exactly once in run.json.

    A duplicated block in cmd_run ran the whole resolve/validate/downgrade
    sequence twice: `resolve_friends` was called twice, the second call
    silently reassigned `specs` AFTER confinement_downgrades had already
    been computed from the first, and the single-friend note was appended
    twice. Visible in a real report as the same sentence printed twice --
    which is also the cheapest thing to assert on.
    """
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n\nSome design text.\n")
    run_af(tmp_path, artifact, "--friend", "fake:good")
    meta = json.loads((sorted((tmp_path / "runs").iterdir())[0] / "run.json").read_text())
    downgrades = meta["downgrades"]
    duplicated = [d for d in set(downgrades) if downgrades.count(d) > 1]
    assert not duplicated, f"downgrade recorded more than once: {duplicated}"


@pytest.fixture(autouse=True)
def _verified_deny_probe(monkeypatch):
    from adversarial_friends import readiness

    monkeypatch.setattr(
        readiness,
        "probe_deny_argv",
        lambda *_args: readiness.DenyProbeResult(True, "verified test shim"),
    )

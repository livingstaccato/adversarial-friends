"""Participation quorum excludes advisory host successes, including across resume."""

from dataclasses import replace
import json
import subprocess
import sys
import threading

from e2e_helpers import AF, _env, run_af

from adversarial_friends import rounds as rounds_mod
from adversarial_friends.adapters import Capability, FriendSpec
from adversarial_friends.authority import ExternalToolPolicy
from adversarial_friends.commands import critique as critique_mod
from adversarial_friends.commands.critique import run_critique
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult


def _spec(name: str = "friend-ops-0", lens: str = "ops") -> FriendSpec:
    return FriendSpec(
        name=name,
        cli="fake",
        lens=lens,
        model=None,
        effort=None,
        scope="doc",
        timeout=30,
    )


def _success(stderr: str = "") -> SpawnResult:
    return SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout='{"no_findings": true}',
        stderr=stderr,
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({"findings": None, "no_findings": True}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with problems.\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _write_run_json(tmp_path, meta):
    (_run_dir(tmp_path) / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


def _halt(tmp_path, *modes, mode="report", extra=()):
    args = []
    for selected_mode in modes:
        args += ["--friend", f"fake:{selected_mode}"]
    return run_af(
        tmp_path, _artifact(tmp_path), *args, "--merge", "orchestrator", *extra, mode=mode
    )


def _respond(tmp_path, merges, round_no=1):
    request = _run_dir(tmp_path) / f"round-{round_no}" / "REQUEST.json"
    data = json.loads(request.read_text())
    data["merges"] = merges
    (request.parent / "RESPONSE.json").write_text(json.dumps(data))
    return data


def _resume(tmp_path, env_extra=None, extra=()):
    return subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            "--resume",
            _run_dir(tmp_path).name,
            "--out",
            str(tmp_path / "runs"),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=_env(env_extra),
    )


def test_advisory_host_success_does_not_satisfy_participation_floor(monkeypatch, tmp_path):
    host = replace(
        _spec("host-ops-0"),
        independent=False,
        host_self_review=True,
    )
    peer = _spec("peer-security-0", lens="security")
    failed = replace(_success(), exit_code=1, failure_reason="exit 1")
    store = RunStore(tmp_path, "run-independent-participation")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    monkeypatch.setattr(
        critique_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: rounds_mod.DispatchRoundOutcome(
            [
                (host, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY),
                (peer, Capability(False, True, "none"), failed, ExternalToolPolicy.DENY),
            ]
        ),
    )

    outcome, _claims, _counter = run_critique(
        [host, peer],
        1,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert outcome.any_success is True
    assert outcome.succeeded_friends == 0
    assert outcome.successful_friend_ids == []
    assert [row["status"].split(":", 1)[0] for row in outcome.friends_meta] == ["ok", "failed"]


def test_require_friends_does_not_count_a_successful_advisory_host(monkeypatch, tmp_path):
    from adversarial_friends import cli
    from adversarial_friends.commands import friends as friends_module

    for name, value in _env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    original = friends_module._specs_from_flags

    def marked_specs(*args, **kwargs):
        specs = original(*args, **kwargs)
        specs[0] = replace(specs[0], independent=False, host_self_review=True)
        return specs

    monkeypatch.setattr(friends_module, "_specs_from_flags", marked_specs)
    parsed = cli.build_parser().parse_args(
        [
            "run",
            str(_artifact(tmp_path)),
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:good",
            "--friend",
            "fake:crash",
            "--require-friends",
            "1",
        ]
    )

    assert cli.cmd_run(parsed) == 12
    meta = _run_json(tmp_path)
    assert meta["successful_friend_ids"] == []
    assert meta["succeeded_friends"] == 0
    assert meta["stop_reason"] == "incomplete"


def test_advisory_host_success_cannot_satisfy_resumed_participation_floor(
    tmp_path,
):
    halted = _halt(
        tmp_path,
        "good",
        "crash",
        extra=("--require-friends", "1"),
    )
    assert halted.returncode == 10, halted.stderr
    # Model a checkpoint whose successful row is advisory. The resume path
    # must preserve that usable audit result without restoring its authority
    # to satisfy --require-friends.
    checkpoint = _run_json(tmp_path)
    checkpoint.pop("detected_host", None)
    checkpoint.pop("effective_include_self", None)
    checkpoint["roster"][0].update({"independent": False, "host_self_review": True})
    checkpoint["friends"][0].update({"independent": False, "host_self_review": True})
    _write_run_json(tmp_path, checkpoint)
    _respond(tmp_path, [])

    resumed = _resume(tmp_path)

    assert resumed.returncode == 12, resumed.stderr
    terminal = _run_json(tmp_path)
    assert terminal["successful_friend_ids"] == []
    assert terminal["stop_reason"] == "incomplete"

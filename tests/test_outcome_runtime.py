"""Lifecycle integrations for the pure RunOutcome contract."""

import argparse
from datetime import UTC, datetime
import json
import sys

from e2e_helpers import FAKE
import pytest

from adversarial_friends.ceilings import Budget
from adversarial_friends.cliargs import build_parser
from adversarial_friends.commands import run as run_command, runmeta, status
from adversarial_friends.commands.critique import CritiqueOutcome
from adversarial_friends.commands.crossexam import CrossexamOutcome
from adversarial_friends.commands.resume import ResumedRun, ResumedStep
from adversarial_friends.errors import UsageError
from adversarial_friends.failures import RepeatTracker
from adversarial_friends.orchestrator import NeedsOrchestrator
from adversarial_friends.outcomes import RunOutcome, terminal_outcome
from adversarial_friends.progress import Progress
from adversarial_friends.runstore import RunStore


def _outcome(**facts):
    return terminal_outcome(
        mode="report",
        converged=False,
        loop_exhausted=False,
        budget_reason=None,
        blocking_ids=[],
        any_success=True,
        unresolved=False,
        **facts,
    )


def _args(tmp_path, artifact):
    return build_parser().parse_args(
        [
            "run",
            str(artifact),
            "--friend",
            "fake:good",
            "--out",
            str(tmp_path / "runs"),
        ]
    )


def _fake_environment(monkeypatch):
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")


def test_wall_clock_rollback_does_not_invalidate_monotonic_duration():
    got = _outcome(
        started_at="2026-08-31T10:00:01Z",
        finished_at="2026-08-31T10:00:00Z",
        duration_s=2.0,
    )
    assert got.duration_s == 2.0


def test_frozen_dataclass_exposes_the_contract_fields():
    fields = set(RunOutcome.__dataclass_fields__)
    assert fields == {
        "started_at",
        "finished_at",
        "duration_s",
        "stop_reason",
        "exit_code",
        "converged",
        "gate_decision",
        "blocker_ids",
        "ceiling_hit",
        "attempted_calls",
        "spent_calls",
        "iterations_run",
        "rounds_run",
        "dry_streak",
        "repeat_tracker",
    }


def test_unexpected_runtime_error_is_persisted_then_reraised(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)
    monkeypatch.setattr(
        run_command,
        "run_critique",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated bug")),
    )

    with pytest.raises(RuntimeError, match="simulated bug"):
        run_command.cmd_run(_args(tmp_path, artifact))

    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["stop_reason"] == "runtime-error"
    assert meta["exit_code"] == 1
    assert meta["lifecycle_state"] == "terminal"
    assert "Stop reason: `runtime-error`" in (run_dir / "report.md").read_text(encoding="utf-8")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    terminal = [event for event in events if event["type"] == "run_finished"]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["status"] == "error"
    assert terminal[0]["payload"]["next_action"] == "inspect_report"


def test_mid_dispatch_stop_terminalizes_after_preserving_partial_friend_rows(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)

    def partial_critique(*_args, **_kwargs):
        return (
            CritiqueOutcome(
                friends_meta=[
                    {
                        "name": "fake-good-0",
                        "model": None,
                        "effort": None,
                        "round": 1,
                        "status": "failed: refused unsafe dispatch",
                    }
                ],
                calls=1,
                any_failed=True,
                dispatch_error=UsageError("refused unsafe dispatch"),
            ),
            [],
            0,
        )

    monkeypatch.setattr(run_command, "run_critique", partial_critique)

    assert run_command.cmd_run(_args(tmp_path, artifact)) == 1

    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["stop_reason"] == "runtime-error"
    assert meta["lifecycle_state"] == "terminal"
    assert [row["name"] for row in meta["friends"]] == ["fake-good-0"]


@pytest.mark.parametrize(
    "raised",
    [UsageError("resumed dispatch refused"), KeyboardInterrupt("resumed dispatch interrupted")],
    ids=("af-error", "interruption"),
)
def test_resumed_judging_dispatch_stop_terminalizes_with_partial_evidence(
    monkeypatch, tmp_path, raised
):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)
    out = tmp_path / "runs"
    halted_args = build_parser().parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "crossexam",
            "--friend",
            "fake:judge_uphold_a",
            "--friend",
            "fake:judge_uphold_b",
            "--merge",
            "orchestrator",
            "--out",
            str(out),
        ]
    )
    with pytest.raises(NeedsOrchestrator):
        run_command.cmd_run(halted_args)
    run_dir = next(out.iterdir())
    request_path = run_dir / "round-1" / "REQUEST.json"
    response = json.loads(request_path.read_text(encoding="utf-8"))
    response["merges"] = []
    (request_path.parent / "RESPONSE.json").write_text(json.dumps(response), encoding="utf-8")

    def partial_resume(args, _store, review, specs, *_args, **_kwargs):
        row = {
            "name": specs[0].name,
            "model": specs[0].model,
            "effort": specs[0].effort,
            "round": 2,
            "status": "failed: resumed dispatch stopped",
        }
        cross = CrossexamOutcome(
            claims=list(review.claims),
            friends_meta=[row],
            rounds_run=2,
            incomplete=True,
            dispatch_error=raised,
        )
        resumed = ResumedRun(
            claims=list(review.claims),
            friends_meta=[*args._resume_meta["friends"], row],
            cross=cross,
        )
        return ResumedStep(resumed=resumed, streak=0, done=True)

    monkeypatch.setattr(run_command, "resume_iteration", partial_resume)
    resume_args = build_parser().parse_args(["run", "--resume", run_dir.name, "--out", str(out)])

    assert run_command.cmd_run(resume_args) == 1

    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["stop_reason"] == "runtime-error"
    assert meta["exit_code"] == 1
    assert meta["friends"][-1]["status"] == "failed: resumed dispatch stopped"


def test_signal_interruption_precedes_a_runtime_dispatch_error():
    got = _outcome(runtime_error=True, abort_signum=2)

    assert got.stop_reason == "interrupted"
    assert got.exit_code == 130


def test_terminal_persistence_failure_does_not_hide_the_original_runtime_error(
    monkeypatch, tmp_path
):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)
    monkeypatch.setattr(
        run_command,
        "run_critique",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated bug")),
    )
    monkeypatch.setattr(
        runmeta,
        "render",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("report unavailable")),
    )

    with pytest.raises(RuntimeError, match="simulated bug") as caught:
        run_command.cmd_run(_args(tmp_path, artifact))

    assert any("report unavailable" in note for note in caught.value.__notes__)


def test_terminal_duration_uses_monotonic_time_when_utc_moves_backward(monkeypatch, tmp_path):
    class LaterStartClock:
        @classmethod
        def now(cls, _tz) -> datetime:
            return datetime(2026, 8, 31, 10, 0, 1, tzinfo=UTC)

    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)
    monkeypatch.setattr(run_command, "datetime", LaterStartClock)
    monkeypatch.setattr(runmeta, "_finished_at", lambda: "2026-08-31T10:00:00Z")

    assert run_command.cmd_run(_args(tmp_path, artifact)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["finished_at"] < meta["started_at"]
    assert meta["duration_s"] >= 0


def test_zero_response_completeness_does_not_mask_a_terminal_ceiling(tmp_path, capsys):
    store = RunStore(tmp_path, "run-completeness-ceiling")
    budget = Budget(max_calls=2, max_rounds=1, max_wall_clock_s=60)
    budget.exhaust("max-wall-clock")

    assert (
        runmeta.finish_run(
            argparse.Namespace(
                mode="report", require_friends=None, json=False, failure_summary="terminal"
            ),
            store,
            {
                "artifact": "spec.md",
                "mode": "report",
                "preset": "inherit",
                "friends": [
                    {
                        "name": "fake-security-0",
                        "independent": True,
                        "host_self_review": False,
                        "model": None,
                        "effort": None,
                        "round": 1,
                        "status": "failed: DNS temporary failure",
                    }
                ],
                "downgrades": [],
                "started_at": "2026-08-31T00:00:00Z",
            },
            None,
            None,
            False,
            0,
            [],
            1,
            0,
            [],
            budget,
            1,
            RepeatTracker(),
            False,
            False,
            60.0,
        )
        == 11
    )

    stderr = capsys.readouterr().err
    assert "max-wall-clock" in stderr
    assert "review incomplete" not in stderr
    meta = json.loads((store.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["review_completeness"]["state"] == "incomplete"
    assert meta["review_completeness"]["message"] in (store.run_dir / "report.md").read_text(
        encoding="utf-8"
    )
    assert (
        status.summarize(store.run_dir, root=store.run_dir.parent)["review_completeness"]
        == meta["review_completeness"]
    )


def test_non_utf8_artifact_is_refused_before_run_directory_creation(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_bytes(b"\xff\xfe")
    _fake_environment(monkeypatch)

    with pytest.raises(UsageError, match="UTF-8"):
        run_command.cmd_run(_args(tmp_path, artifact))
    assert not (tmp_path / "runs").exists()


def test_post_create_initialization_failure_removes_unexplained_partial_run(monkeypatch, tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    _fake_environment(monkeypatch)
    monkeypatch.setattr(
        run_command,
        "select_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    with pytest.raises(RuntimeError, match="init failed"):
        run_command.cmd_run(_args(tmp_path, artifact))
    assert not list((tmp_path / "runs").glob("run-*"))


def test_terminal_render_failure_preserves_the_prior_artifact_pair(monkeypatch, tmp_path):
    store = RunStore(tmp_path, "run-render-failure")
    store.write_run_json({"lifecycle_state": "waiting-for-orchestrator"})
    store.write_report("# waiting\n")
    meta_path = store.run_dir / "run.json"
    report_path = store.run_dir / "report.md"
    before = (meta_path.read_bytes(), report_path.read_bytes())
    monkeypatch.setattr(
        runmeta,
        "render",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    with pytest.raises(RuntimeError, match="render failed"):
        runmeta.finish_run(
            argparse.Namespace(mode="report", require_friends=None, json=False),
            store,
            {
                "artifact": "spec.md",
                "mode": "report",
                "preset": "inherit",
                "friends": [],
                "downgrades": [],
                "started_at": "2026-08-31T00:00:00Z",
            },
            None,
            None,
            True,
            1,
            ["fake-good-0"],
            1,
            0,
            [],
            Budget(max_calls=2, max_rounds=1, max_wall_clock_s=60),
            1,
            RepeatTracker(),
            False,
            False,
            1.0,
        )

    assert meta_path.read_bytes() == before[0]
    assert report_path.read_bytes() == before[1]


def test_broken_stdout_still_leaves_one_durable_terminal_event(monkeypatch, tmp_path):
    store = RunStore(tmp_path, "run-broken-stdout")
    reporter = Progress(event_writer=store.events_writer())
    monkeypatch.setitem(
        runmeta.__dict__,
        "print",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokenPipeError()),
    )

    with pytest.raises(BrokenPipeError):
        runmeta.finish_run(
            argparse.Namespace(mode="report", require_friends=None, json=False),
            store,
            {
                "artifact": "spec.md",
                "mode": "report",
                "preset": "inherit",
                "friends": [],
                "downgrades": [],
                "started_at": "2026-08-31T00:00:00Z",
            },
            None,
            None,
            True,
            1,
            ["fake-good-0"],
            1,
            0,
            [],
            Budget(max_calls=2, max_rounds=1, max_wall_clock_s=60),
            1,
            RepeatTracker(),
            False,
            False,
            1.0,
            reporter=reporter,
        )

    events = [json.loads(line) for line in store.events_path().read_text().splitlines()]
    terminal = [event for event in events if event["type"] == "run_finished"]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["status"] == "completed"

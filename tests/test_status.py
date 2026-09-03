"""Read-only inspection of persisted Adversarial Friends runs."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from e2e_helpers import _git_commit, _git_repo
import pytest

from adversarial_friends import cli
from adversarial_friends.commands import status
from adversarial_friends.errors import UsageError
from adversarial_friends.events import MAX_EVENT_LOG_BYTES, EventRecord, EventWriter
from adversarial_friends.progress import Progress


def _args(run_id: str, *, out: Path | None = None, json_output: bool = False, watch: bool = False):
    return argparse.Namespace(
        run_id=run_id, out=str(out) if out is not None else None, json=json_output, watch=watch
    )


def _event(event_type: str, payload: dict[str, object]) -> str:
    return json.dumps(
        EventRecord.create(
            event_type, payload, run_id="run-status", timestamp="2026-09-03T12:00:00Z"
        ).to_dict()
    )


def _run(root: Path, *, state: str = "terminal", events: bool = True) -> Path:
    run = root / "run-status"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "lifecycle_state": state,
                "mode": "report",
                "profile": "quick",
                "downgrades": ["doc scope only"],
                "friends": [{"name": "fake-security-0", "status": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    (run / "claims.jsonl").write_text(
        json.dumps(
            {
                "type": "claim",
                "id": "c-0001@1",
                "supersedes": None,
                "origin": ["fake-security-0"],
                "lens": "security",
                "round": 1,
                "advisory": False,
                "severity": "high",
                "claim": "Missing check",
                "location": "src/app.py:1",
                "evidence": "missing check",
                "failure_scenario": "bad input",
                "suggested_fix": "add check",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if events:
        (run / "events.jsonl").write_text(
            _event("run_started", {"mode": "report", "profile": "quick", "status": "started"})
            + "\n"
            + _event("run_finished", {"status": "completed", "next_action": "inspect_report"})
            + "\n",
            encoding="utf-8",
        )
    return run


def test_status_summarizes_a_terminal_run_without_mutating(tmp_path, capsys):
    root = tmp_path / "runs"
    run = _run(root)
    before = {
        path: path.stat().st_mode for path in [root, run, run / "run.json", run / "claims.jsonl"]
    }

    assert status.cmd_status(_args("run-status", out=root)) == 0

    output = capsys.readouterr().out
    assert "terminal" in output
    assert "completed" in output
    assert "next: inspect_report" in output
    assert {path: path.stat().st_mode for path in before} == before
    assert not (run / ".lock").exists()


def test_status_json_is_versioned_and_uses_legacy_artifacts_when_events_are_absent(
    tmp_path, capsys
):
    root = tmp_path / "runs"
    _run(root, events=False)

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert payload["state"] == "terminal"
    assert payload["mode"] == "report"
    assert payload["profile"] == "quick"
    assert payload["claims"] == {"by_status": {"pending": 1}, "total": 1}
    assert payload["downgrades"] == ["doc scope only"]


def test_status_projects_safe_legacy_friend_metadata_without_events(tmp_path, capsys):
    root = tmp_path / "runs"
    run = _run(root, events=False)
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    meta["roster"] = [
        {
            "name": "fake-doc-0",
            "cli": "fake",
            "lens": "security",
            "scope": "doc",
            "model": None,
            "effort": None,
            "timeout": 1,
        },
        {
            "name": "fake-repo-0",
            "cli": "fake",
            "lens": "ops",
            "scope": "repo",
            "model": None,
            "effort": None,
            "timeout": 1,
        },
    ]
    meta["friends"] = [
        {"name": "fake-doc-0", "round": 1, "status": "ok"},
        {"name": "fake-repo-0", "round": 2, "status": "failed: timed out"},
    ]
    (run / "run.json").write_text(json.dumps(meta), encoding="utf-8")

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["friends"]["rows"] == [
        {
            "name": "fake-doc-0",
            "provider": "fake",
            "scope": "doc",
            "round": 1,
            "status": "succeeded",
        },
        {
            "name": "fake-repo-0",
            "provider": "fake",
            "scope": "repo",
            "round": 2,
            "status": "failed",
        },
    ]
    assert summary["friends"]["finished"] == 2
    assert summary["friends"]["failed"] == 1


def test_status_rejects_an_empty_directory(tmp_path):
    root = tmp_path / "runs"
    (root / "run-status").mkdir(parents=True)

    with pytest.raises(UsageError, match="not a run directory"):
        status.cmd_status(_args("run-status", out=root))


@pytest.mark.parametrize("kind", ["directory", "oversized"])
def test_status_wraps_unreadable_event_artifacts_as_usage_errors(tmp_path, kind):
    root = tmp_path / "runs"
    run = _run(root, events=False)
    events_path = run / "events.jsonl"
    if kind == "directory":
        events_path.mkdir()
    else:
        events_path.write_bytes(b"x" * (MAX_EVENT_LOG_BYTES + 1))

    with pytest.raises(UsageError, match="cannot read lifecycle events"):
        status.cmd_status(_args("run-status", out=root))


def test_status_rejects_path_outside_the_selected_run_root(tmp_path):
    root = tmp_path / "runs"
    outside = _run(tmp_path / "elsewhere")
    root.mkdir()

    with pytest.raises(UsageError, match="outside the run root"):
        status.cmd_status(_args(str(outside), out=root))


def test_status_accepts_an_explicit_directory_only_when_contained(tmp_path, capsys):
    root = tmp_path / "runs"
    run = _run(root)

    assert status.cmd_status(_args(str(run), out=root)) == 0

    assert "run-status: terminal" in capsys.readouterr().out


def test_watch_ignores_a_torn_tail_then_stops_at_run_finished(tmp_path):
    root = tmp_path / "runs"
    run = _run(root, state="running", events=False)
    events_path = run / "events.jsonl"
    started = _event("run_started", {"mode": "report", "profile": "quick", "status": "started"})
    finished = _event("run_finished", {"status": "completed", "next_action": "inspect_report"})
    events_path.write_text(started + "\n" + finished[:20], encoding="utf-8")

    observed = list(
        status.watch_events(
            events_path,
            root=root,
            poll_s=0,
            snapshots=[started + "\n" + finished[:20], started + "\n" + finished + "\n"],
        )
    )

    assert [event.type for event in observed] == ["run_started", "run_finished"]


def test_watch_started_at_end_does_not_repeat_prior_progress(tmp_path):
    root = tmp_path / "runs"
    run = _run(root, state="running", events=False)
    started = _event("run_started", {"mode": "report", "profile": "quick", "status": "started"})
    finished = _event("run_finished", {"status": "completed", "next_action": "inspect_report"})

    observed = list(
        status.watch_events(
            run / "events.jsonl",
            root=root,
            poll_s=0,
            start_at_end=True,
            snapshots=[started + "\n", started + "\n" + finished + "\n"],
        )
    )

    assert [event.type for event in observed] == ["run_finished"]


def test_status_summarizes_a_live_event_first_run_before_run_json_exists(tmp_path, capsys):
    """cmd_run writes this durable event before its first run.json checkpoint."""
    root = tmp_path / "runs"
    run = root / "run-status"
    run.mkdir(parents=True)
    writer = EventWriter(run / "events.jsonl", root, "run-status")
    writer.append(
        EventRecord.create(
            "run_started",
            {"mode": "crossexam", "profile": "balanced", "status": "started"},
            run_id="run-status",
        )
    )

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "live"
    assert summary["mode"] == "crossexam"
    assert summary["profile"] == "balanced"
    assert summary["rounds"] == {"current": 0, "final": None}


def test_cmd_run_exposes_an_event_first_status_checkpoint(monkeypatch, tmp_path, capsys):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    root = tmp_path / "runs"
    fake = Path(__file__).with_name("fake_friend.py")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {fake}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    observed: list[dict[str, object]] = []
    original = Progress.run_started

    def inspect_before_metadata(self, mode: str, profile: str, scope: str) -> None:
        original(self, mode, profile, scope)
        run = next(root.iterdir())
        assert not (run / "run.json").exists()
        observed.append(status.summarize(run, root=root))

    monkeypatch.setattr(Progress, "run_started", inspect_before_metadata)

    assert (
        cli.main(
            [
                "run",
                str(artifact),
                "--out",
                str(root),
                "--friend",
                "fake:good",
                "--no-progress",
            ]
        )
        == 0
    )

    assert observed[0]["state"] == "live"
    assert observed[0]["mode"] == "report"
    assert observed[0]["profile"] == "quick"
    assert observed[0]["rounds"] == {
        "current": 0,
        "final": None,
    }
    capsys.readouterr()


def test_event_first_status_reports_validated_repo_scope(monkeypatch, tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    artifact = repo / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    subprocess.run(["git", "add", "spec.md"], cwd=repo, check=True, capture_output=True)
    _git_commit(repo, "add spec")
    root = tmp_path / "runs"
    fake = Path(__file__).with_name("fake_friend.py")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {fake}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    observed: list[dict[str, object]] = []
    original = Progress.run_started

    def inspect_before_metadata(self, mode: str, profile: str, scope: str) -> None:
        original(self, mode, profile, scope)
        run = next(root.iterdir())
        assert not (run / "run.json").exists()
        observed.append(status.summarize(run, root=root))

    monkeypatch.setattr(Progress, "run_started", inspect_before_metadata)

    assert (
        cli.main(
            [
                "run",
                str(artifact),
                "--out",
                str(root),
                "--friend",
                "fake:good:repo",
                "--no-progress",
            ]
        )
        == 0
    )

    assert observed[0]["scope"] == "repo"
    capsys.readouterr()


def test_repo_started_run_keeps_its_scope_after_a_friend_event(tmp_path, capsys):
    root = tmp_path / "runs"
    run = root / "run-status"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        _event(
            "run_started",
            {"mode": "report", "profile": "quick", "scope": "repo", "status": "started"},
        )
        + "\n"
        + _event(
            "friend_finished",
            {
                "friend": "fake-repo-0",
                "provider": "fake",
                "lens": "configured",
                "round": 1,
                "duration_s": 1.0,
                "status": "succeeded",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["scope"] == "repo"
    assert summary["friends"]["rows"][0]["scope"] == "unknown"


def test_status_and_watch_use_only_the_latest_lifecycle_invocation(tmp_path, capsys):
    root = tmp_path / "runs"
    run = _run(root, state="waiting-for-orchestrator", events=False)
    first_start = _event(
        "run_started",
        {"mode": "report", "profile": "quick", "scope": "doc", "status": "started"},
    )
    first_finish = _event("run_finished", {"status": "halted", "next_action": "resume"})
    resumed_start = _event(
        "run_started",
        {"mode": "crossexam", "profile": "balanced", "scope": "repo", "status": "started"},
    )
    resumed_finish = _event(
        "run_finished", {"status": "completed", "next_action": "inspect_report"}
    )
    (run / "events.jsonl").write_text(
        first_start + "\n" + first_finish + "\n" + resumed_start + "\n", encoding="utf-8"
    )

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "live"
    assert summary["mode"] == "crossexam"
    assert summary["profile"] == "balanced"
    assert summary["scope"] == "repo"
    observed = list(
        status.watch_events(
            run / "events.jsonl",
            root=root,
            poll_s=0,
            start_at_end=True,
            snapshots=[
                first_start + "\n" + first_finish + "\n" + resumed_start + "\n",
                first_start
                + "\n"
                + first_finish
                + "\n"
                + resumed_start
                + "\n"
                + resumed_finish
                + "\n",
            ],
        )
    )
    assert [event.type for event in observed] == ["run_finished"]


def test_status_surfaces_safe_scope_rounds_and_finished_friend_rows(tmp_path, capsys):
    root = tmp_path / "runs"
    run = _run(root)
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    meta["rounds_run"] = 2
    meta["roster"] = [
        {
            "name": "fake-doc-0",
            "cli": "fake",
            "lens": "security",
            "scope": "doc",
            "model": None,
            "effort": None,
            "timeout": 1,
        },
        {
            "name": "fake-repo-0",
            "cli": "fake",
            "lens": "ops",
            "scope": "repo",
            "model": None,
            "effort": None,
            "timeout": 1,
        },
    ]
    (run / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    (run / "events.jsonl").write_text(
        _event("run_started", {"mode": "report", "profile": "quick", "status": "started"})
        + "\n"
        + _event(
            "friend_finished",
            {
                "friend": "fake-doc-0",
                "provider": "fake",
                "lens": "configured",
                "round": 1,
                "duration_s": 1.0,
                "status": "succeeded",
            },
        )
        + "\n"
        + _event(
            "friend_failed",
            {
                "friend": "fake-repo-0",
                "provider": "fake",
                "lens": "configured",
                "round": 2,
                "duration_s": 2.0,
                "status": "failed",
            },
        )
        + "\n"
        + _event("round_finished", {"round": 2, "status": "completed"})
        + "\n"
        + _event("run_finished", {"status": "completed", "next_action": "inspect_report"})
        + "\n",
        encoding="utf-8",
    )

    assert status.cmd_status(_args("run-status", out=root, json_output=True)) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["scope"] == "repo"
    assert summary["rounds"] == {"current": 2, "final": 2}
    assert summary["friends"]["rows"] == [
        {
            "name": "fake-doc-0",
            "provider": "fake",
            "scope": "doc",
            "round": 1,
            "status": "succeeded",
        },
        {
            "name": "fake-repo-0",
            "provider": "fake",
            "scope": "repo",
            "round": 2,
            "status": "failed",
        },
    ]


@pytest.mark.parametrize("state", ["terminal", "running"])
def test_watch_reports_unavailable_events_and_returns(tmp_path, capsys, state):
    root = tmp_path / "runs"
    _run(root, state=state, events=False)

    assert status.cmd_status(_args("run-status", out=root, watch=True)) == 0

    assert "live events unavailable" in capsys.readouterr().err

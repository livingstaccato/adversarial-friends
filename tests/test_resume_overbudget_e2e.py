"""End-to-end contracts for checkpoint spend against the saved call cap."""

import json
import subprocess
import sys

from e2e_helpers import AF, _env, run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with problems.\n")
    return path


def _run_dir(tmp_path):
    return next((tmp_path / "runs").iterdir())


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _write_run_json(tmp_path, meta):
    (_run_dir(tmp_path) / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


def _halt(tmp_path):
    return run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:judge_uphold_a",
        "--friend",
        "fake:judge_uphold_b",
        "--merge",
        "orchestrator",
        "--max-calls",
        "2",
    )


def _respond(tmp_path):
    request = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    response = json.loads(request.read_text())
    response["merges"] = []
    (request.parent / "RESPONSE.json").write_text(json.dumps(response))
    return response


def _resume(tmp_path):
    return subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            "--resume",
            _run_dir(tmp_path).name,
            "--out",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )


def test_resume_refuses_saved_spend_above_the_original_cap_without_consuming_state(tmp_path):
    halted = _halt(tmp_path)
    assert halted.returncode == 10, halted.stderr
    response = _respond(tmp_path)
    run_json = _run_dir(tmp_path) / "run.json"
    report = _run_dir(tmp_path) / "report.md"
    meta = _run_json(tmp_path)
    assert meta["invocation"]["max_calls"] == 2
    meta["spent_calls"] = meta["attempted_calls"] = 3
    _write_run_json(tmp_path, meta)
    before = (run_json.read_bytes(), report.read_bytes())

    resumed = _resume(tmp_path)

    assert resumed.returncode == 2
    assert "spent_calls" in resumed.stderr
    assert "max_calls" in resumed.stderr
    assert (run_json.read_bytes(), report.read_bytes()) == before
    round_dir = _run_dir(tmp_path) / "round-1"
    assert json.loads((round_dir / "RESPONSE.json").read_text()) == response
    assert not (round_dir / "RESPONSE.json.applied").exists()


def test_resume_at_the_exact_saved_call_cap_can_complete_without_another_call(tmp_path):
    halted = _halt(tmp_path)
    assert halted.returncode == 10, halted.stderr
    checkpoint = _run_json(tmp_path)
    assert checkpoint["spent_calls"] == checkpoint["invocation"]["max_calls"] == 2
    _respond(tmp_path)

    resumed = _resume(tmp_path)

    assert resumed.returncode == 0, resumed.stderr
    terminal = _run_json(tmp_path)
    assert terminal["spent_calls"] == terminal["attempted_calls"] == 2

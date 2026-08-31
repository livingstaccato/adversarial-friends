"""Regression found by the bounded 0.2.1 release dogfood."""

from test_run_end_to_end_orchestrator import _halt, _respond, _resume, _run_json


def test_report_resume_can_finish_after_spending_exact_call_budget(tmp_path):
    halted = _halt(
        tmp_path,
        "good",
        "good",
        extra=("--max-calls", "2"),
    )
    assert halted.returncode == 10, halted.stderr
    checkpoint = _run_json(tmp_path)
    assert checkpoint["spent_calls"] == 2
    _respond(tmp_path, [])

    resumed = _resume(tmp_path)

    assert resumed.returncode == 0, resumed.stderr
    terminal = _run_json(tmp_path)
    assert terminal["stop_reason"] == "completed"
    assert terminal["exit_code"] == 0
    assert terminal["spent_calls"] == 2

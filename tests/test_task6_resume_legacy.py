"""Legacy quorum recovery and hostile saved-friend rows for Task 6."""

import json

import pytest
from test_run_end_to_end_orchestrator import (
    _halt,
    _respond,
    _resume,
    _run_dir,
    _run_json,
    _write_run_json,
)

from afriend.commands.checkpoint import normalize_resume_report_state
from afriend.errors import UsageError


def _remove_task6_success_checkpoint(tmp_path):
    meta = _run_json(tmp_path)
    for field in ("successful_friend_ids", "succeeded_friends", "required_friends"):
        meta.pop(field, None)
    _write_run_json(tmp_path, meta)


@pytest.mark.parametrize(
    ("modes", "expected_exit", "expected_successes"),
    [
        (("good", "good"), 0, ["fake-good-0", "fake-good-1"]),
        (("good", "crash"), 12, ["fake-good-0"]),
    ],
)
def test_legacy_checkpoint_recovers_exact_critique_quorum(
    tmp_path, modes, expected_exit, expected_successes
):
    halted = _halt(tmp_path, *modes, extra=("--require-friends", "2"))
    assert halted.returncode == 10, halted.stderr
    _remove_task6_success_checkpoint(tmp_path)
    _respond(tmp_path, [])

    resumed = _resume(tmp_path)

    assert resumed.returncode == expected_exit, resumed.stderr
    assert _run_json(tmp_path)["successful_friend_ids"] == expected_successes


def test_legacy_zero_success_extraction_checkpoint_does_not_fail_open(tmp_path):
    halted = _halt(
        tmp_path,
        "offtopic",
        "crash",
        extra=("--require-friends", "2"),
    )
    assert halted.returncode == 10, halted.stderr
    _remove_task6_success_checkpoint(tmp_path)
    request_path = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    data = json.loads(request_path.read_text())
    data["unparseable"][0]["findings"] = []
    (request_path.parent / "RESPONSE.json").write_text(json.dumps(data))

    resumed = _resume(tmp_path)

    assert resumed.returncode == 1, resumed.stderr
    assert _run_json(tmp_path)["successful_friend_ids"] == []


def test_legacy_quorum_recovery_survives_two_loop_halts_with_repeated_names(tmp_path):
    halted = _halt(
        tmp_path,
        "judge_uphold_a",
        "judge_uphold_b",
        mode="loop",
        extra=(
            "--max-rounds",
            "2",
            "--max-loop-iterations",
            "2",
            "--require-friends",
            "2",
        ),
    )
    assert halted.returncode == 10, halted.stderr
    _remove_task6_success_checkpoint(tmp_path)
    _respond(tmp_path, [])

    halted_again = _resume(tmp_path)

    assert halted_again.returncode == 10, halted_again.stderr
    _remove_task6_success_checkpoint(tmp_path)
    _respond(tmp_path, [], round_no=3)

    terminal = _resume(tmp_path)

    assert terminal.returncode == 11, terminal.stderr
    assert len(_run_json(tmp_path)["successful_friend_ids"]) == 2


@pytest.mark.parametrize(
    "friends",
    [
        [1],
        [{"name": 1, "model": None, "effort": None, "round": 1, "status": "ok"}],
        [{"name": "fake-good-0", "round": "1", "status": "ok"}],
        [{"name": "fake-good-0", "model": None, "effort": None, "round": 1}],
        [
            {
                "name": "fake-good-0",
                "model": [],
                "effort": None,
                "round": 1,
                "status": "ok",
            }
        ],
    ],
)
def test_malformed_saved_friend_rows_refuse_resume_without_mutating_artifacts(tmp_path, friends):
    halted = _halt(tmp_path, "good")
    assert halted.returncode == 10, halted.stderr
    meta = _run_json(tmp_path)
    meta["friends"] = friends
    _write_run_json(tmp_path, meta)
    meta_path = _run_dir(tmp_path) / "run.json"
    report_path = _run_dir(tmp_path) / "report.md"
    before_meta = meta_path.read_bytes()
    before_report = report_path.read_bytes()

    resumed = _resume(tmp_path)

    assert resumed.returncode == 2, resumed.stderr
    assert "saved friends" in resumed.stderr
    assert meta_path.read_bytes() == before_meta
    assert report_path.read_bytes() == before_report


def test_saved_downgrades_are_strictly_validated_and_restored_in_order():
    assert normalize_resume_report_state({"downgrades": ["first", "second", "first"]})[
        "downgrades"
    ] == ["first", "second"]


@pytest.mark.parametrize("value", ["not-a-list", [1], ["x" * 8193]])
def test_hostile_saved_downgrades_are_refused(value):
    with pytest.raises(UsageError, match="saved downgrades"):
        normalize_resume_report_state({"downgrades": value})

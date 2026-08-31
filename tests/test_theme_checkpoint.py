"""Durability and strict resume validation for advisory theme metadata."""

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


def test_theme_novelty_survives_multiple_loop_halts_without_duplicate_restore(tmp_path):
    halted = _halt(
        tmp_path,
        "judge_theme_variant_a",
        "judge_theme_variant_b",
        mode="loop",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )
    assert halted.returncode == 10, halted.stderr
    assert _run_json(tmp_path)["produced_new_themes"] is True
    _respond(tmp_path, [])

    halted_again = _resume(tmp_path)
    assert halted_again.returncode == 10, halted_again.stderr
    second = _run_json(tmp_path)
    assert second["produced_new_themes"] is False
    assert len(second["theme_proposals"]) == 2
    _respond(tmp_path, [], round_no=3)

    halted_third = _resume(tmp_path)
    assert halted_third.returncode == 10, halted_third.stderr
    third = _run_json(tmp_path)
    assert third["produced_new_themes"] is False
    # Round 5 is an exact repeat of round 3, so exact identity is the
    # fallback and the prior fuzzy proposals are preserved, not duplicated.
    assert third["theme_proposals"] == second["theme_proposals"]
    _respond(tmp_path, [], round_no=5)

    terminal = _resume(tmp_path)
    assert terminal.returncode == 0, terminal.stderr
    final = _run_json(tmp_path)
    assert final["theme_proposals"] == third["theme_proposals"]
    assert final["produced_new_themes"] is False
    assert final["dry_streak"] == 2


def test_extraction_halt_preserves_theme_facts_from_successful_peer(tmp_path):
    halted = _halt(tmp_path, "theme_batch", "offtopic")
    assert halted.returncode == 10, halted.stderr
    checkpoint = _run_json(tmp_path)
    assert checkpoint["produced_new_themes"] is True
    assert len(checkpoint["theme_proposals"]) == 1

    request = _run_dir(tmp_path) / "round-1" / "REQUEST.json"
    response = json.loads(request.read_text())
    response["unparseable"][0]["findings"] = []
    (request.parent / "RESPONSE.json").write_text(json.dumps(response))

    terminal = _resume(tmp_path)
    assert terminal.returncode == 0, terminal.stderr
    final = _run_json(tmp_path)
    assert final["theme_proposals"] == checkpoint["theme_proposals"]
    assert final["produced_new_themes"] is True


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        {},
        [{"canonical": "c-0001@1"}],
        [
            {
                "canonical": "c-0001@1",
                "duplicate": "c-0002@1",
                "score": "high",
                "anchor": "src/a.py:1",
            }
        ],
    ],
)
def test_malformed_saved_theme_proposals_refuse_resume_without_mutation(tmp_path, bad_value):
    halted = _halt(tmp_path, "good")
    assert halted.returncode == 10, halted.stderr
    meta = _run_json(tmp_path)
    meta["theme_proposals"] = bad_value
    _write_run_json(tmp_path, meta)
    _respond(tmp_path, [])
    run_json = _run_dir(tmp_path) / "run.json"
    before = run_json.read_bytes()

    resumed = _resume(tmp_path)

    assert resumed.returncode == 2, resumed.stderr
    assert "theme_proposals" in resumed.stderr
    assert run_json.read_bytes() == before


def test_malformed_saved_theme_novelty_fact_refuses_resume_without_mutation(tmp_path):
    halted = _halt(tmp_path, "good")
    assert halted.returncode == 10, halted.stderr
    meta = _run_json(tmp_path)
    meta["produced_new_themes"] = 1
    _write_run_json(tmp_path, meta)
    _respond(tmp_path, [])
    run_json = _run_dir(tmp_path) / "run.json"
    before = run_json.read_bytes()

    resumed = _resume(tmp_path)

    assert resumed.returncode == 2, resumed.stderr
    assert "produced_new_themes" in resumed.stderr
    assert run_json.read_bytes() == before


def test_whole_metadata_bound_refuses_1638_proposals_without_mutation(tmp_path):
    halted = _halt(tmp_path, "good")
    assert halted.returncode == 10, halted.stderr
    meta = _run_json(tmp_path)
    meta["theme_proposals"] = [
        {
            "canonical": f"c-{index * 2 + 1:04d}@1",
            "duplicate": f"c-{index * 2 + 2:04d}@1",
            "score": 1.0,
            "anchor": f"src/a{index}.py:1",
        }
        for index in range(1_638)
    ]
    _write_run_json(tmp_path, meta)
    _respond(tmp_path, [])
    run_json = _run_dir(tmp_path) / "run.json"
    before = run_json.read_bytes()

    resumed = _resume(tmp_path)

    assert resumed.returncode == 2, resumed.stderr
    assert "metadata bound" in resumed.stderr
    assert run_json.read_bytes() == before

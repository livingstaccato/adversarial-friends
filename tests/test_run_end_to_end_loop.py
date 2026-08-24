"""End-to-end `--mode loop` (spec §7.3).

A loop is repeated crossexam iterations over the same artifact. **The runner
never edits an artifact** (§7.5), so what a loop actually buys is two things:
convergence detection -- proving a roster keeps finding the same things and
nothing more -- and picking up a revision if something outside the run made
one between iterations.

That makes the interesting assertions about round numbering (iterations must
not collide in the run directory or the ledger), the dry-round streak, and
the ceilings, rather than about the artifact changing.
"""

import json

from e2e_helpers import run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nA design with problems.\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _ledger(tmp_path):
    text = (_run_dir(tmp_path) / "claims.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _loop(tmp_path, *friends, extra=()):
    args = []
    for friend in friends:
        args += ["--friend", friend]
    return run_af(tmp_path, _artifact(tmp_path), *args, *extra, mode="loop")


def test_an_unchanged_artifact_converges_and_stops_early(tmp_path):
    """The point of the mode. Nothing revises the artifact between
    iterations, so each one re-raises the same claims, they all alias, and
    two dry rounds end the loop well before the iteration ceiling."""
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "5"),
    )
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["iterations_run"] < 5, "the loop should converge, not run to its ceiling"
    assert meta["dry_streak"] >= 2


def test_iterations_do_not_collide_in_the_run_directory(tmp_path):
    """Each iteration owns a distinct block of round numbers, so iteration
    2's critique cannot overwrite iteration 1's friend output."""
    _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "3", "--max-rounds", "2"),
    )
    rounds = sorted(p.name for p in _run_dir(tmp_path).glob("round-*"))
    # Iteration 1 uses rounds 1-2, iteration 2 uses 3-4, and so on.
    assert "round-1" in rounds
    assert "round-3" in rounds


def test_repeated_claims_alias_rather_than_multiplying(tmp_path):
    """A loop that re-raised the same finding as a new claim every iteration
    would report several copies of one defect."""
    _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "3"),
    )
    aliases = [r for r in _ledger(tmp_path) if r["type"] == "alias"]
    assert aliases, "a second identical iteration should have produced aliases"


def test_a_single_iteration_behaves_like_crossexam(tmp_path):
    """--max-loop-iterations 1 is a crossexam with loop bookkeeping, and must
    not change any of its conclusions."""
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "1"),
    )
    meta = _run_json(tmp_path)
    assert meta["iterations_run"] == 1
    assert set(meta["claim_states"].values()) == {"settled-upheld"}
    assert result.returncode == 0, result.stderr


def test_the_call_ceiling_stops_a_loop(tmp_path):
    """§7.4's budget is a whole-run total, not a per-iteration allowance --
    a loop is exactly where a per-iteration budget would run away."""
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "5", "--max-calls", "2"),
    )
    assert result.returncode == 11, (result.returncode, result.stderr)
    assert "budget-exhausted" in result.stderr


def test_a_loop_records_how_many_iterations_it_ran(tmp_path):
    _loop(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b")
    assert _run_json(tmp_path)["iterations_run"] >= 1


def test_report_mode_is_still_unaffected(tmp_path):
    """The regression that matters most: extracting the critique round for
    the loop must not have changed what --mode report does."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert "iterations_run" not in meta
    assert "claim_states" not in meta
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "the guard is missing" in report

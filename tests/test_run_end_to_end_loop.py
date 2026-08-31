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


def test_same_anchor_wording_variant_is_advisory_and_does_not_reset_dry_streak(tmp_path):
    result = _loop(
        tmp_path,
        "fake:judge_theme_variant_a",
        "fake:judge_theme_variant_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )

    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["converged"] is True
    assert meta["dry_streak"] == 2
    assert meta["produced_new_themes"] is False
    assert meta["theme_proposals"]
    assert all(
        set(item) == {"canonical", "duplicate", "score", "anchor"}
        for item in meta["theme_proposals"]
    )

    claims = [record for record in _ledger(tmp_path) if record["type"] == "claim"]
    texts = {record["claim"] for record in claims}
    assert {"expiry guard is missing", "missing expiration guard"} <= texts
    assert len({record["id"] for record in claims}) == len(claims)
    assert not [
        record
        for record in _ledger(tmp_path)
        if record["type"] == "alias" and record["source"] == "theme"
    ]

    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "## Possible semantic duplicates" in report
    assert "advisory only" in report.lower()


def test_genuinely_new_theme_resets_a_prior_dry_streak(tmp_path):
    result = _loop(
        tmp_path,
        "fake:judge_theme_new_late_a",
        "fake:judge_theme_new_late_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )

    assert result.returncode == 11, result.stderr
    meta = _run_json(tmp_path)
    assert meta["produced_new_themes"] is True
    assert meta["dry_streak"] == 0
    assert meta["converged"] is False


def test_a_single_unconverged_iteration_is_the_loop_ceiling(tmp_path):
    """One iteration keeps crossexam conclusions but exhausts the loop range."""
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "1"),
    )
    meta = _run_json(tmp_path)
    assert meta["iterations_run"] == 1
    assert set(meta["claim_states"].values()) == {"settled-upheld"}
    assert result.returncode == 11, result.stderr
    assert meta["stop_reason"] == "max-loop-iterations"
    assert meta["ceiling_hit"] == "max-loop-iterations"


def test_terminal_metadata_has_one_exact_lifecycle_and_checkpoint_state(tmp_path):
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:judge_uphold_b",
        extra=("--max-loop-iterations", "1"),
    )
    meta = _run_json(tmp_path)
    assert result.returncode == meta["exit_code"]
    assert meta["schema_version"] == 2
    assert meta["lifecycle_state"] == "terminal"
    assert meta["started_at"].endswith("Z")
    assert meta["finished_at"].endswith("Z")
    assert meta["duration_s"] >= 0
    assert meta["spent_calls"] == meta["attempted_calls"]
    assert set(meta["repeat_tracker"]) == {"last", "count", "disabled"}


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
    assert "max-calls" in result.stderr


def test_a_loop_records_how_many_iterations_it_ran(tmp_path):
    _loop(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b")
    assert _run_json(tmp_path)["iterations_run"] >= 1


def test_report_mode_is_still_unaffected(tmp_path):
    """The regression that matters most: extracting the critique round for
    the loop must not have changed what --mode report does."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert meta["iterations_run"] == 1
    assert "claim_states" not in meta
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "the guard is missing" in report


def test_a_deterministically_broken_friend_stops_being_dispatched(tmp_path):
    """§7.2's cost argument, end to end. `crash` fails identically every
    time; a loop at its defaults would otherwise redispatch it five
    iterations x three rounds, every call guaranteed useless."""
    result = _loop(
        tmp_path,
        "fake:judge_uphold_a",
        "fake:crash",
        extra=("--max-loop-iterations", "4"),
    )
    meta = _run_json(tmp_path)
    downgrades = " ".join(meta["downgrades"])
    assert "not be dispatched again" in downgrades, result.stderr

    # And it genuinely stopped: the broken friend appears in fewer rounds
    # than the working one.
    rounds_per_friend: dict[str, int] = {}
    for friend in meta["friends"]:
        rounds_per_friend[friend["name"]] = rounds_per_friend.get(friend["name"], 0) + 1
    assert rounds_per_friend["fake-crash-1"] < rounds_per_friend["fake-judge_uphold_a-0"]


def test_a_friend_that_recovers_is_not_disabled(tmp_path):
    """A friend that failed once and then worked told us the failure was
    transient. Disabling it would throw away a working reviewer.

    Both friends used to succeed in every round, so nothing was ever
    disabled and the assertion held whatever the tracker did -- a test that
    passed by construction, found by a crossexam of this file's caller.
    `judge_absent_once` fails in round 2 and judges normally afterwards, so
    the tracker is actually exercised: one failure must not disable it, and
    its later verdicts must still count."""
    result = _loop(
        tmp_path,
        "fake:judge_absent_once_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "4"),
    )
    assert result.returncode == 1, result.stderr
    meta = _run_json(tmp_path)
    downgrades = " ".join(meta["downgrades"])
    assert "not be dispatched again" not in downgrades
    assert "no longer counted as one of the judges" not in downgrades
    # It failed once, so the run is incomplete -- and it judged afterwards,
    # so its claim reached a settled state rather than sitting unproven.
    assert meta["incomplete"] is True
    assert "settled-upheld" in meta["claim_states"].values(), meta["claim_states"]


# --- What an iteration must not redo ---------------------------------------


def test_a_superseded_claim_is_not_re_judged_every_iteration(tmp_path):
    """Terminal is terminal (§7.2), across iterations too. Each iteration
    used to re-seed every claim `contested`, so a claim the last iteration
    had already superseded was judged and amended again -- and since
    `bump_claim_id` counts versions rather than records, every iteration
    wrote a successor under the SAME id. A three-iteration loop put
    `c-0002@2` in the ledger three times."""
    _loop(
        tmp_path,
        "fake:judge_amend_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )
    ids = [r["id"] for r in _ledger(tmp_path) if r["type"] == "claim"]
    assert len(ids) == len(set(ids)), ids
    states = _run_json(tmp_path)["claim_states"]
    assert "superseded" in states.values(), states


def test_a_claim_no_friend_can_judge_does_not_hold_the_loop_open(tmp_path):
    """An amended claim's successor inherits both the author's and the
    amenders' origins, which on a two-friend roster is the whole roster: no
    independent judge, `unproven` for good. Waiting for it to turn terminal
    ran every loop to its iteration ceiling -- the failure §7.3's H4
    correction exists to prevent, arriving through one more door."""
    result = _loop(
        tmp_path,
        "fake:judge_amend_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "3", "--max-loop-iterations", "6"),
    )
    meta = _run_json(tmp_path)
    # Six iterations of three rounds would reach round 16.
    assert meta["rounds_run"] < 16, meta["rounds_run"]
    assert not any("no round was left to judge" in d for d in meta["downgrades"]), meta
    assert result.returncode in (0, 1), result.stderr


# --- What a later iteration inherits ---------------------------------------


def test_a_claim_settled_in_an_earlier_iteration_still_quotes_its_judges(tmp_path):
    """Carrying only states meant a claim deadlocked in iteration 1 was
    printed under "Unsettled" with "No verdict was cast on this claim" --
    while both judges' reasoning sat in the ledger. §7.2 requires both sides
    quoted verbatim, and that is the whole reason the section exists."""
    result = _loop(
        tmp_path,
        "fake:judge_refute_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "3", "--max-loop-iterations", "3"),
    )
    assert result.returncode == 0, result.stderr
    states = _run_json(tmp_path)["claim_states"]
    assert "deadlocked" in states.values(), states
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "No verdict was cast" not in report, report


def test_a_failure_in_an_earlier_iteration_is_not_forgotten(tmp_path):
    """§7.2's M12 marks the RUN incomplete, not the iteration. Each block
    built a fresh outcome, so a friend that failed in iteration 1 and
    recovered in iteration 2 left a run reporting itself complete."""
    _loop(
        tmp_path,
        "fake:judge_absent_once_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )
    assert _run_json(tmp_path)["incomplete"] is True


def test_a_revised_artifact_reopens_what_the_earlier_text_settled(tmp_path):
    """A loop re-reads the artifact precisely to pick up a revision, and a
    claim settled against the old text is not settled against the new one --
    carried across the edit, the report goes on naming a defect the edit may
    have removed."""
    artifact = _artifact(tmp_path)
    result = run_af(
        tmp_path,
        artifact,
        "--friend",
        "fake:judge_edit_a",
        "--friend",
        "fake:judge_uphold_b",
        "--max-rounds",
        "2",
        "--max-loop-iterations",
        "2",
        mode="loop",
        env_extra={"AF_TEST_EDIT_ARTIFACT": str(artifact)},
    )
    assert result.returncode == 11, result.stderr
    assert "the guard was added." in artifact.read_text()
    meta = _run_json(tmp_path)
    downgrades = meta["downgrades"]
    assert any("the artifact changed before iteration" in d for d in downgrades), downgrades
    history = meta["snapshot_history"]
    assert len(history) == 2
    assert history[1]["predecessor"] == (history[0]["commit"] or history[0]["artifact_hash"])
    assert meta["snapshot"] == history[-1]


def test_a_successor_deferred_to_the_next_iteration_says_so(tmp_path):
    """A successor created at the last round of a non-final block is left
    `contested` for the next iteration to judge. The loop can stop first --
    a dry streak, or a ceiling -- and such a successor cannot even hold it
    open, so without a note the report says "judges disagreed" about a
    rewrite no judge has seen."""
    _loop(
        tmp_path,
        "fake:judge_amend_a",
        "fake:judge_uphold_b",
        extra=("--max-rounds", "2", "--max-loop-iterations", "3"),
    )
    downgrades = _run_json(tmp_path)["downgrades"]
    assert any("the last of this iteration" in d for d in downgrades), downgrades

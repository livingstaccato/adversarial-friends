"""End-to-end `afriend run --mode crossexam` (spec §7).

These run the real console script against scripted fake friends, so they
exercise the wiring the unit tests cannot: prompt files landing in round-2
directories, verdicts reaching claims.jsonl, states reaching report.md and
run.json, and the exit codes §7.6 specifies.

See e2e_helpers for why every subprocess here runs under a constructed PATH
and can never reach a real, metered agent CLI.
"""

import json

from e2e_helpers import run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n\nSome design text.\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


def _ledger(tmp_path):
    text = (_run_dir(tmp_path) / "claims.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _crossexam(tmp_path, *friends, extra=()):
    args = []
    for friend in friends:
        args += ["--friend", friend]
    return run_af(tmp_path, _artifact(tmp_path), *args, *extra, mode="crossexam")


# --- The happy path --------------------------------------------------------


def test_two_friends_judge_each_other_and_settle(tmp_path):
    """Both uphold, so both claims reach settled-upheld. The run is complete
    (every claim terminal) but exits 0 -- crossexam reports, it does not
    gate."""
    result = _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b")
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert set(meta["claim_states"].values()) == {"settled-upheld"}


def test_verdicts_reach_the_ledger(tmp_path):
    _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b")
    kinds = [r["type"] for r in _ledger(tmp_path)]
    assert "verdict" in kinds


def test_a_judge_never_judges_its_own_claim(tmp_path):
    """§7.1's core rule, end to end: every verdict's judge must differ from
    the judged claim's origin."""
    _crossexam(tmp_path, "fake:judge_uphold", "fake:judge_refute")
    records = _ledger(tmp_path)
    origins = {r["id"]: r["origin"] for r in records if r["type"] == "claim"}
    verdicts = [r for r in records if r["type"] == "verdict"]
    assert verdicts, "no verdicts were cast"
    for verdict in verdicts:
        assert verdict["judge"] not in origins[verdict["claim_id"]]


def test_the_judge_prompt_is_written_for_inspection(tmp_path):
    _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b")
    prompts = list((_run_dir(tmp_path) / "round-2").glob("*.prompt"))
    assert prompts
    text = prompts[0].read_text()
    assert "CLAIMS UNDER REVIEW" in text
    assert "out-of-scope" in text  # the forced vocabulary reached the judge


def test_the_slice_a_judge_received_is_blind(tmp_path):
    """§5.1 end to end. The friend that wrote a claim is `fake/judge_uphold`;
    that string must not appear in any judge's prompt."""
    _crossexam(tmp_path, "fake:judge_uphold", "fake:judge_refute")
    for prompt in (_run_dir(tmp_path) / "round-2").glob("*.prompt"):
        slice_text = prompt.read_text().split("CLAIMS UNDER REVIEW")[1]
        assert "fake/judge_uphold_a" not in slice_text
        assert "fake/judge_refute" not in slice_text


# --- Disagreement ----------------------------------------------------------


def test_a_disagreement_deadlocks_rather_than_being_decided(tmp_path):
    """Three friends: one upholds, one refutes. §7.2 forbids resolving that
    by majority, so it must end deadlocked with both sides on the record.

    The run still exits 0. `deadlocked` is a terminal state (§7.2) and
    crossexam does not gate -- §7.6 reserves exit 1 for a blocked gate or an
    incomplete run, and a deadlock is neither: it is a completed run whose
    answer happens to be "the friends disagree". Blocking on that is `gate`
    mode's job. What crossexam owes instead is making the disagreement
    impossible to miss, which the next test checks.
    """
    result = _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_refute", "fake:judge_uphold_b")
    assert result.returncode == 0, result.stderr
    states = set(_run_json(tmp_path)["claim_states"].values())
    assert "deadlocked" in states


def test_both_sides_of_a_deadlock_are_quoted_in_the_report(tmp_path):
    _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_refute", "fake:judge_uphold_b")
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "Unsettled" in report
    assert "scripted upheld" in report
    assert "scripted refuted" in report
    assert "already guards this" in report  # the counter-evidence survived


# --- §6.5 evidence symmetry, end to end ------------------------------------


def test_judges_that_could_not_check_the_evidence_settle_nothing(tmp_path):
    """Both judges say "refuted" but neither could verify. Left dispositive
    this would be a unanimous settled-refuted -- a claim dismissed on the
    strength of nobody having looked."""
    _crossexam(
        tmp_path,
        "fake:judge_unverifiable_a",
        "fake:judge_unverifiable_b",
        "fake:judge_unverifiable_c",
    )
    states = set(_run_json(tmp_path)["claim_states"].values())
    assert "settled-refuted" not in states
    assert states <= {"unproven", "discarded", "incomplete"}


# --- Amendments ------------------------------------------------------------


def test_a_unanimous_amendment_supersedes_and_creates_a_successor(tmp_path):
    result = _crossexam(
        tmp_path,
        "fake:judge_amend_a",
        "fake:judge_amend_b",
        "fake:judge_amend_c",
        extra=("--max-rounds", "4"),
    )
    assert result.returncode in (0, 1), result.stderr
    records = _ledger(tmp_path)
    successors = [r for r in records if r["type"] == "claim" and r["supersedes"]]
    assert successors, "no successor claim was written"
    successor = successors[0]
    assert successor["claim"] == "the guard is weak, not missing"
    assert successor["id"].endswith("@2")
    # §6.1: origin is the union of the prior version's origin and every
    # amender, so none of them may judge the rewrite.
    assert len(successor["origin"]) > 1


# --- Ceilings (§7.4, §7.6) -------------------------------------------------


def test_a_call_ceiling_exits_eleven(tmp_path):
    """§7.6: a ceiling outranks every other outcome, because a truncated run
    has not evaluated anything."""
    result = _crossexam(
        tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b", extra=("--max-calls", "2")
    )
    assert result.returncode == 11, (result.returncode, result.stderr)
    assert "budget-exhausted" in result.stderr


def test_the_ceiling_is_visible_in_the_report(tmp_path):
    _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b", extra=("--max-calls", "2"))
    report = (_run_dir(tmp_path) / "report.md").read_text()
    assert "budget-exhausted" in report


def test_an_unreachable_ceiling_is_warned_about_up_front(tmp_path):
    _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_uphold_b", extra=("--max-calls", "2"))
    assert any("cannot accommodate" in d for d in _run_json(tmp_path)["downgrades"])


def test_max_rounds_below_two_is_a_usage_error(tmp_path):
    """Round 1 is the critique round; judging starts at round 2. A crossexam
    capped at one round is a report with a misleading name."""
    result = _crossexam(tmp_path, "fake:judge_uphold_a", extra=("--max-rounds", "1"))
    assert result.returncode == 2
    assert "no judging round" in result.stderr


# --- Degenerate rosters ----------------------------------------------------


def test_a_single_friend_has_nobody_to_judge_it(tmp_path):
    """One friend originates every claim, so no judging round can run at
    all. That must be reported, not silently look like a clean crossexam."""
    result = _crossexam(tmp_path, "fake:judge_uphold_a")
    downgrades = " ".join(_run_json(tmp_path)["downgrades"])
    assert "no friend is independent" in downgrades
    assert result.returncode == 1  # nothing reached a terminal state


def test_a_failed_judge_marks_the_run_incomplete(tmp_path):
    """§7.2's M12. `judge_nothing` returns a well-formed but empty verdict
    set, which is a failure -- a judge is only dispatched when it has claims
    to judge."""
    result = _crossexam(tmp_path, "fake:judge_uphold_a", "fake:judge_nothing")
    assert _run_json(tmp_path)["incomplete"] is True
    assert result.returncode == 1


def test_report_mode_is_unaffected_by_any_of_this(tmp_path):
    """The regression that matters most: crossexam must not have changed
    what --mode report does."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    meta = _run_json(tmp_path)
    assert "claim_states" not in meta
    assert "Cross-examination" not in (_run_dir(tmp_path) / "report.md").read_text()


def test_a_judge_that_skips_claims_is_reported(tmp_path):
    """codex's finding, end to end. A judge is told to return one verdict per
    claim in its slice; one that silently returns fewer still passes
    validation, and the skipped claims would look merely `unproven` -- which
    the discard rule turns TERMINAL after two rounds. A claim nobody was
    willing to judge would be closed as though judges had looked and failed.
    """
    repo = tmp_path
    result = _crossexam(repo, "fake:judge_partial_a", "fake:judge_partial_b", "fake:judge_uphold_c")
    meta = _run_json(tmp_path)
    downgrades = " ".join(meta["downgrades"])
    assert "returned no verdict on" in downgrades, result.stderr


def test_skipped_claims_never_become_discarded(tmp_path):
    """The consequence that made it worth fixing: `incomplete` keeps them out
    of the discard rule, which only fires on `unproven`."""
    _crossexam(tmp_path, "fake:judge_partial_a", "fake:judge_partial_b", "fake:judge_uphold_c")
    states = set(_run_json(tmp_path)["claim_states"].values())
    assert "discarded" not in states


def test_a_disabled_judge_leaves_claims_incomplete_not_discarded(tmp_path):
    """Seen in a real run: two judges failed identically twice and were
    disabled, after which two more rounds ran with NOBODY dispatched and
    every claim ended `discarded` -- "judges looked twice and could not
    verify" -- when no judge had looked at all.

    The tracker's filter was local to dispatch_round, so an empty result set
    read as "nothing failed this round"; below-quorum claims then went
    `unproven`, and two identical empty rounds tripped the discard rule.
    """
    result = _crossexam(
        tmp_path, "fake:judge_uphold_a", "fake:judge_fail_b", extra=("--max-rounds", "6")
    )
    meta = _run_json(tmp_path)
    states = meta["claim_states"]
    assert "discarded" not in states.values(), states
    assert "incomplete" in states.values(), states
    assert meta["incomplete"] is True
    assert result.returncode == 1
    # Round 2 fails, round 3 fails again, round 4 finds no judge it may
    # dispatch. Anything past that is a fan-out that decides nothing.
    assert meta["rounds_run"] <= 4, meta["rounds_run"]


# --- M12 is per claim ------------------------------------------------------


def _claim_ids_by_origin(tmp_path) -> dict[str, list[str]]:
    by_origin: dict[str, list[str]] = {}
    for line in next(tmp_path.rglob("claims.jsonl")).read_text().splitlines():
        entry = json.loads(line)
        if entry.get("type") == "claim":
            for origin in entry["origin"]:
                by_origin.setdefault(origin, []).append(entry["id"])
    return by_origin


def test_an_unrelated_friends_failure_does_not_mark_other_claims_incomplete(tmp_path):
    """Raised by the judges of a real crossexam, reviewing the previous fix:
    `required_missing` was a run-level flag, so one friend failing marked
    every below-quorum claim in the run `incomplete` and reset its discard
    signature -- including claims whose own judges had all reported.

    Friend a fails in round 2. Its claim is judged by b and c, who both
    report (unverifiably): below quorum with nobody missing is `unproven`.
    b's and c's claims each had a in their judge set: `incomplete`.
    """
    _crossexam(
        tmp_path,
        "fake:judge_absent_once_a",
        "fake:judge_unverifiable_b",
        "fake:judge_unverifiable_c",
        extra=("--max-rounds", "2"),
    )
    meta = _run_json(tmp_path)
    states = meta["claim_states"]
    by_origin = _claim_ids_by_origin(tmp_path)
    assert {states[c] for c in by_origin["fake/judge_absent_once_a"]} == {"unproven"}, states
    assert {states[c] for c in by_origin["fake/judge_unverifiable_b"]} == {"incomplete"}, states
    assert {states[c] for c in by_origin["fake/judge_unverifiable_c"]} == {"incomplete"}, states
    assert meta["incomplete"] is True


# --- Amendments are rewrites, in any round ---------------------------------


def test_a_final_round_amendment_supersedes_and_leaves_the_successor_incomplete(tmp_path):
    """Seen on a real crossexam: both judges said a claim's headline was
    false and amended it in the final round; the rule this replaced rewrote
    their amendments to `upheld` and reported "judges unanimously agreed the
    claim stands". A successor created by the last round has nobody left to
    judge it, and the run says so instead of settling something."""
    result = _crossexam(
        tmp_path, "fake:judge_amend_a", "fake:judge_uphold_b", extra=("--max-rounds", "2")
    )
    meta = _run_json(tmp_path)
    states = meta["claim_states"]
    assert "superseded" in states.values(), states
    successors = [cid for cid in states if cid.endswith("@2")]
    assert successors and all(states[c] == "incomplete" for c in successors), states
    # NOT the run-level flag: that means "a required friend failed" (§7.2
    # M12) and the report says so in those words. No friend failed here.
    assert meta["incomplete"] is False
    assert any("no round was left to judge" in d for d in meta["downgrades"]), meta
    assert result.returncode == 1


def test_an_unverifiable_amendment_is_not_a_rewrite(tmp_path):
    """The evidence rule turns it into `unproven` (§6.5): a judge that could
    not check the evidence has not rewritten anything either."""
    _crossexam(
        tmp_path, "fake:judge_shaky_amend_a", "fake:judge_uphold_b", extra=("--max-rounds", "4")
    )
    states = _run_json(tmp_path)["claim_states"]
    assert "superseded" not in states.values(), states


# --- One identity per roster entry ----------------------------------------


def test_a_roster_entry_repeated_verbatim_is_refused(tmp_path):
    """Two entries with the same (cli, lens, model, effort) would be one
    ledger identity casting two verdicts; which one counted depended on
    flag order. Refused before anything is spent."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--friend",
        "fake:good",
        mode="crossexam",
    )
    assert result.returncode == 2, result.stderr
    assert "same friend" in result.stderr

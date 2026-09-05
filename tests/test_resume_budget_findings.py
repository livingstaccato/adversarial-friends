"""c-0001 / c-0007: a resumed run forgot everything it had already spent.

`Budget.calls` is an in-memory dataclass field, and `--resume` starts a new
process. `resume_round_one` used to charge back exactly `len(specs)` --
one round's cost -- to account for that, which is right for a run halting
on its very first round ever and silently wrong for every halt after it: a
`--mode loop --merge orchestrator` run halting once per iteration forgot
every EARLIER iteration's spend at every resume. A 5-iteration loop could
blow past `--max-calls` by a large multiple with the ceiling never firing,
because each resuming process believed only its own round 1 had ever run.

Fixed by persisting `budget.calls` into `run.json` at every halt and
restoring it -- the whole number, not a one-round guess -- on resume.
"""

import argparse
import json
import threading

from afriend import orchestrator
from afriend.ceilings import Budget
from afriend.commands.haltstate import write_halt
from afriend.commands.resume import resume_round_one
from afriend.failures import RepeatTracker
from afriend.reviewstate import ReviewState
from afriend.runstore import RunStore
from afriend.spawn import NormalizeResult, SpawnResult


def _store(tmp_path, name):
    store = RunStore(tmp_path, name)
    store.lock()
    return store


def _write_empty_merge_round(store, round_no):
    """The budget restore is what these tests exercise, not the merge
    application -- an empty `merges` list is the cheapest valid response
    that lets `resume_round_one` reach the code under test."""
    round_dir = store.round_dir(round_no)
    orchestrator.write_request(round_dir, store.run_id, round_no, [])
    orchestrator.response_path(round_dir).write_text(
        json.dumps({"version": orchestrator.SCHEMA_VERSION, "merges": []})
    )


def _args(mode="report", resume_meta=None):
    return argparse.Namespace(
        mode=mode,
        max_rounds=3,
        attributed=False,
        allow_unsandboxed_friend=False,
        _resume_meta=resume_meta or {},
    )


def _resume(store, base_round, budget, resume_meta):
    """mode="report" is not a JUDGING_MODE, so this returns right after
    applying/restoring -- these tests are about the restore, not judging."""
    return resume_round_one(
        _args(resume_meta=resume_meta),
        store,
        ReviewState.replay(store.ledger.records()),
        [],
        {},
        None,
        None,
        "",
        None,
        None,
        threading.Event(),
        budget,
        base_round,
        lambda _p: None,
    )


# --- write_halt persists the true spend -------------------------------------


def test_write_halt_records_the_budgets_true_spend(monkeypatch, tmp_path):
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-halt-spend")
    budget = Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0)
    budget.spend(37)

    write_halt(_args(), store, {}, ReviewState(), 1, 0, None, budget=budget)

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert meta["spent_calls"] == 37
    assert meta["lifecycle_state"] == "waiting-for-orchestrator"
    assert meta["started_at"].endswith("Z")
    assert "finished_at" not in meta
    assert "exit_code" not in meta


def test_write_halt_records_exact_checkpoint_counters_and_active_elapsed(monkeypatch, tmp_path):
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-halt-counters")
    budget = Budget(max_calls=100, max_wall_clock_s=3600.0, started=10.0)
    budget.spend(7)

    write_halt(
        _args(),
        store,
        {},
        ReviewState(),
        2,
        1,
        None,
        budget=budget,
        rounds_run=4,
        active_elapsed_s=12.5,
        successful_friend_ids=["fake-good-0"],
    )

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert meta["attempted_calls"] == meta["spent_calls"] == 7
    assert meta["iterations_run"] == 2
    assert meta["rounds_run"] == 4
    assert meta["resume_iteration"] == 2
    assert meta["active_elapsed_s"] == 12.5
    assert meta["successful_friend_ids"] == ["fake-good-0"]


def test_budget_composes_prior_active_elapsed_without_counting_inactive_wait():
    budget = Budget(
        max_calls=100,
        max_wall_clock_s=100.0,
        started=1_000.0,
        prior_elapsed_s=40.0,
    )

    assert budget.elapsed(1_020.0) == 60.0
    assert budget.seconds_left(1_020.0) == 40.0
    assert not budget.out_of_time(1_059.0)
    assert budget.out_of_time(1_060.0)


def test_write_halt_without_a_budget_omits_the_field(monkeypatch, tmp_path):
    """Backward compatible: existing callers that pass no budget must not
    write a field that claims a number nobody measured."""
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-halt-no-budget")

    write_halt(_args(), store, {}, ReviewState(), 1, 0, None)

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert "spent_calls" not in meta


# --- resume_round_one restores it, not a one-round guess --------------------


def test_a_resume_restores_the_full_prior_spend_not_one_rounds_worth(tmp_path):
    """The defect exactly. A run halted after TWO completed iterations plus
    the halting iteration's own round 1 -- 27 calls total, nothing to do
    with `len(specs)`, the one-round guess the old heuristic charged back."""
    store = _store(tmp_path, "run-restore")
    _write_empty_merge_round(store, 6)
    budget = Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0)

    _resume(store, 6, budget, {"spent_calls": 27})

    assert budget.calls == 27, "expected the persisted total, not len(specs)"


def test_a_resume_with_no_recorded_spend_undercounts_rather_than_crashes(tmp_path):
    """A run halted by a version predating this field has no `spent_calls`
    key at all. Absent is 0 -- an undercount by omission, not a crash, and
    the honest choice when the true number was simply never recorded."""
    store = _store(tmp_path, "run-restore-missing")
    _write_empty_merge_round(store, 2)
    budget = Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0)

    _resume(store, 2, budget, {})

    assert budget.calls == 0


def test_cumulative_spend_compounds_across_two_halts(monkeypatch, tmp_path):
    """The end-to-end shape: iteration 1 halts having spent 9, gets
    resumed and spends 5 more before iteration 2 halts -- the SECOND halt
    must persist 14, the running total, not 5."""
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-compound")
    _write_empty_merge_round(store, 1)

    first_halt_budget = Budget(max_calls=1000, max_wall_clock_s=3600.0, started=0.0)
    first_halt_budget.spend(9)
    write_halt(_args(), store, {}, ReviewState(), 1, 0, None, budget=first_halt_budget)

    # The resume: a FRESH Budget, as a new process would construct.
    resumed_budget = Budget(max_calls=1000, max_wall_clock_s=3600.0, started=0.0)
    _resume(store, 1, resumed_budget, {"spent_calls": 9})
    assert resumed_budget.calls == 9
    resumed_budget.spend(5)  # iteration 2's own round 1

    write_halt(_args(), store, {}, ReviewState(), 2, 0, None, budget=resumed_budget)

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert meta["spent_calls"] == 14


# --- c-0002: write_halt also persists the repeat tracker --------------------


def test_write_halt_persists_the_repeat_tracker(monkeypatch, tmp_path):
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-halt-tracker")
    tracker = RepeatTracker()
    failed = SpawnResult(
        argv=[],
        exit_code=1,
        stdout="",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(payload=None, errors=[], succeeded=False),
        failure_reason="exit 1",
        orphans_suspected=False,
    )
    tracker.record("codex-ops", failed)
    tracker.record("codex-ops", failed)
    assert tracker.is_disabled("codex-ops")

    write_halt(_args(), store, {}, ReviewState(), 1, 0, None, tracker=tracker)

    meta = json.loads((store.run_dir / "run.json").read_text())
    restored = RepeatTracker.restore(meta["repeat_tracker"])
    assert restored.is_disabled("codex-ops")


def test_write_halt_without_a_tracker_omits_the_field(monkeypatch, tmp_path):
    from afriend.commands import haltstate

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = _store(tmp_path, "run-halt-no-tracker")

    write_halt(_args(), store, {}, ReviewState(), 1, 0, None)

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert "repeat_tracker" not in meta

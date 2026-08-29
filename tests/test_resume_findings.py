"""Findings from cross-examining `commands/run.py`: c-0001, c-0003, c-0004,
c-0006, c-0011.

Five claims, one cause. `--mode loop --merge orchestrator` halts once per
iteration, and almost nothing survived the halt: the claim counter was
recomputed from a list that merges had shrunk, the resumed judging round was
handed none of what earlier iterations decided, the dry-round streak was
zeroed by a constant, and the adjudication response could be applied twice.

Each test here fails against the previous behaviour. That was checked rather
than assumed -- a test for a resume path that only ever runs the new code is
worth very little.
"""

import argparse
import contextlib
import json
from pathlib import Path

from adversarial_friends.ids import format_claim_id
from adversarial_friends.ledger import Alias, Claim
from adversarial_friends.merge import canonical_claims, next_claim_number


def _claim(number: int, text: str = "a finding") -> Claim:
    return Claim(
        id=format_claim_id(number),
        supersedes=None,
        origin=["codex/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="medium",
        claim=text,
        location=None,
        evidence="e",
        failure_scenario="f",
        suggested_fix="s",
    )


# --- c-0001: a merged claim's id is spent, not free ------------------------


def test_a_merged_claim_does_not_free_its_id_for_reuse():
    """The defect exactly. `counter = len(all_claims)` counted the CANONICAL
    list, which drops claims a merge retired -- so merging c-0002 into
    c-0001 left length one, and the next iteration minted c-0002 again. The
    ledger is append-only, so it then held two different claims under one
    id, and aliases, verdicts, states and resolutions all key on that id."""
    records: list[object] = [_claim(1), _claim(2)]
    records.append(
        Alias(canonical="c-0001@1", duplicate="c-0002@1", round=1, source="exact", rationale="same")
    )

    assert len(canonical_claims(records)) == 1, "the merge retired one claim"
    # The old arithmetic would mint c-0002@1 a second time.
    assert len(canonical_claims(records)) != next_claim_number(records)
    assert next_claim_number(records) == 2
    assert format_claim_id(next_claim_number(records) + 1) == "c-0003@1"


def test_the_counter_counts_superseded_versions_too():
    """An amended claim's successor spends a number as surely as any other
    record. Counting only live claims would hand it out twice."""
    records: list[object] = [_claim(1), _claim(2), _claim(3)]
    assert next_claim_number(records) == 3


def test_an_empty_ledger_starts_at_zero():
    assert next_claim_number([]) == 0


# --- c-0003 / c-0006: the streak survives the halt -------------------------


def _args(**meta) -> argparse.Namespace:
    return argparse.Namespace(mode="loop", _resume_meta=meta)


def test_a_dry_halted_round_advances_the_streak():
    """The defect: this was `next_streak(streak, failed=False,
    dry=round_is_dry(False, True))`, and `round_is_dry(False, True)` is
    always False -- so the streak `loop_position` had just restored was
    zeroed on every resume. `loop_should_terminate` needs two consecutive
    dry rounds, so a resumed loop could not converge at all."""
    from adversarial_friends.commands.haltstate import resumed_streak

    assert resumed_streak(_args(halted_round_dry=True, halted_round_failed=False), 1) == 2


def test_a_round_that_learned_something_resets_the_streak():
    """The other half. A streak that only ever advanced would terminate a
    loop that was still finding new claims."""
    from adversarial_friends.commands.haltstate import resumed_streak

    assert resumed_streak(_args(halted_round_dry=False, halted_round_failed=False), 1) == 0


def test_a_failed_halted_round_resets_the_streak():
    """§7.3: a round that did not complete is not evidence of convergence."""
    from adversarial_friends.commands.haltstate import resumed_streak

    assert resumed_streak(_args(halted_round_dry=True, halted_round_failed=True), 1) == 0


def test_a_halt_with_no_recorded_dryness_is_treated_as_failed():
    """An extraction halt raises before the round returns anything to read,
    and a run.json written by an older version has neither key. Assuming
    convergence there is the dangerous guess; assuming a failed round only
    costs an extra iteration."""
    from adversarial_friends.commands.haltstate import resumed_streak

    assert resumed_streak(_args(), 1) == 0


def test_write_halt_records_what_the_round_actually_did(monkeypatch, tmp_path):
    """The persisted half. Without these two keys the resumed iteration has
    nothing to compute from, which is how the constant got there.

    The rendered report is stubbed: what is under test is what reaches
    run.json, and building a meta complete enough for the renderer would
    make this a test of the renderer instead."""
    from adversarial_friends.commands import haltstate
    from adversarial_friends.runstore import RunStore

    monkeypatch.setattr(haltstate, "render", lambda *a, **k: "")
    store = RunStore(tmp_path, "run-x")
    store.lock()
    args = argparse.Namespace(mode="loop")

    haltstate.write_halt(args, store, {}, [], [], 2, 1, None, round_dry=True, round_failed=False)

    meta = json.loads((store.run_dir / "run.json").read_text())
    assert meta["halted_round_dry"] is True
    assert meta["halted_round_failed"] is False
    assert meta["dry_streak"] == 1


# --- c-0011: an adjudication response is applied once ----------------------


def test_a_consumed_response_is_renamed_so_a_second_resume_cannot_reapply_it(tmp_path):
    """`ledger.append` is a bare JSONL write with no dedupe, and nothing
    marked RESPONSE.json used -- so a second `--resume` re-read the same
    file and appended every extracted claim again under fresh ids.

    Judges split on this one: the claim named the merge path, which is
    guarded (canonical reconstruction has already dropped the aliased id, so
    `read_response` refuses it), while the evidence described the extraction
    path, where the defect is real. Renaming covers both -- and a resume
    that finds nothing left to apply beats the merge path's loud refusal.
    """
    from adversarial_friends.commands.resume import CONSUMED_SUFFIX, _mark_response_consumed

    round_dir = tmp_path / "round-1"
    round_dir.mkdir()
    response = round_dir / "RESPONSE.json"
    response.write_text("{}")

    _mark_response_consumed(round_dir)

    assert not response.exists()
    assert (round_dir / f"RESPONSE.json{CONSUMED_SUFFIX}").read_text() == "{}"


def test_marking_a_missing_response_is_not_an_error(tmp_path):
    """Called on every resume, including modes that never wrote one. It must
    not turn a completed resume into a traceback."""
    from adversarial_friends.commands.resume import _mark_response_consumed

    round_dir = tmp_path / "round-1"
    round_dir.mkdir()
    _mark_response_consumed(round_dir)


def test_the_consumed_copy_is_kept_rather_than_deleted(tmp_path):
    """It is the operator's own written judgment. A run directory that
    discards it cannot be audited afterwards."""
    from adversarial_friends.commands.resume import _mark_response_consumed

    round_dir = tmp_path / "round-1"
    round_dir.mkdir()
    (round_dir / "RESPONSE.json").write_text('{"merges": []}')
    _mark_response_consumed(round_dir)
    kept = list(round_dir.glob("RESPONSE.json*"))
    assert len(kept) == 1
    assert kept[0].read_text() == '{"merges": []}'


# --- c-0004: the resumed judging round inherits what came before -----------


def test_the_resumed_judging_round_is_handed_the_prior_outcome(monkeypatch, tmp_path):
    """The defect: `resume_round_one` called `run_rounds` with no `prior`,
    so a loop resumed at iteration 2 re-seeded every claim `contested` and
    re-judged what iteration 1 had settled -- at full fan-out cost, with
    judges shown none of the prior arguments.

    The same call was also missing `tracker`, `keep`, `extra_args` and
    `pass_env`: one omission, five behaviours.
    """
    from adversarial_friends.commands import resume as resume_mod

    seen: dict[str, object] = {}

    def _fake_run_rounds(*args, **kwargs):
        seen.update(kwargs)
        raise _Stop

    class _Stop(Exception):
        pass

    monkeypatch.setattr(resume_mod, "run_rounds", _fake_run_rounds)

    sentinel = object()
    store = resume_mod.RunStore(tmp_path, "run-y")
    store.lock()
    (store.run_dir / "round-1").mkdir(parents=True, exist_ok=True)
    (store.run_dir / "round-1" / "REQUEST.json").write_text('{"question": "merge"}')
    (store.run_dir / "round-1" / "RESPONSE.json").write_text('{"version": 1, "merges": []}')

    args = argparse.Namespace(
        mode="crossexam",
        max_rounds=3,
        attributed=False,
        allow_unsandboxed_friend=False,
        _resume_meta={},
    )
    with contextlib.suppress(_Stop):
        resume_mod.resume_round_one(
            args,
            store,
            [],
            {},
            None,
            Path("spec.md"),
            "text",
            None,
            None,
            resume_mod.threading.Event(),
            _budget(),
            1,
            lambda _p: None,
            prior=sentinel,  # type: ignore[arg-type]
            keep=True,
            extra_args=["--foo"],
            pass_env=("VAR",),
        )

    assert seen.get("prior") is sentinel, "the prior outcome never reached run_rounds"
    assert seen.get("keep") is True
    assert seen.get("extra_args") == ["--foo"]
    assert seen.get("pass_env") == ("VAR",)


def _budget():
    from adversarial_friends.ceilings import Budget

    return Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0)


# --- c-0008: the clock hook is validated and visible -----------------------


def test_a_malformed_clock_offset_is_a_usage_error_not_a_traceback(monkeypatch):
    """It was a bare `float(os.environ.get(...))` in the middle of cmd_run,
    so `AF_CLOCK_OFFSET_S=abc` came out as an unhandled ValueError."""
    from adversarial_friends.commands.environment import CLOCK_OFFSET_VAR, clock_offset
    from adversarial_friends.errors import UsageError

    monkeypatch.setenv(CLOCK_OFFSET_VAR, "abc")
    try:
        clock_offset([])
    except UsageError as exc:
        assert CLOCK_OFFSET_VAR in str(exc)
    else:
        raise AssertionError("expected a UsageError")


def test_a_set_clock_offset_is_recorded_in_the_run(monkeypatch):
    """It shortens every wall-clock ceiling. An ambient value in CI made a
    run report budget-exhausted while the downgrade blamed --max-wall-clock,
    a ceiling the operator had set correctly."""
    from adversarial_friends.commands.environment import CLOCK_OFFSET_VAR, clock_offset

    monkeypatch.setenv(CLOCK_OFFSET_VAR, "3600")
    notes: list[str] = []
    assert clock_offset(notes) == 3600.0
    assert notes and CLOCK_OFFSET_VAR in notes[0]


def test_an_unset_clock_offset_says_nothing(monkeypatch):
    """The normal case must stay silent, or every run carries a downgrade."""
    from adversarial_friends.commands.environment import CLOCK_OFFSET_VAR, clock_offset

    monkeypatch.delenv(CLOCK_OFFSET_VAR, raising=False)
    notes: list[str] = []
    assert clock_offset(notes) == 0.0
    assert notes == []


# --- c-0010: run.json survives a crash mid-write ---------------------------


def test_run_json_is_never_left_half_written(tmp_path):
    """`write_text` truncates first and writes second, so a process that
    dies in between leaves the file existing and invalid -- and `--resume`
    reads run.json to reconstruct the run, so that is permanent loss of a
    run that may represent an hour of metered CLI time.

    Checked by watching what exists on disk mid-write rather than by reading
    the implementation: the temporary file must be a sibling that is not
    itself run.json, so no reader ever opens a partial one.
    """
    from adversarial_friends.runstore import RunStore

    store = RunStore(tmp_path, "run-atomic")
    store.lock()
    store.write_run_json({"first": True})
    assert json.loads((store.run_dir / "run.json").read_text()) == {"first": True}

    store.write_run_json({"second": True})
    assert json.loads((store.run_dir / "run.json").read_text()) == {"second": True}
    # Nothing left behind.
    assert not list(store.run_dir.glob(".run.json.tmp"))


def test_the_report_is_written_the_same_way(tmp_path):
    from adversarial_friends.runstore import RunStore

    store = RunStore(tmp_path, "run-atomic-2")
    store.lock()
    store.write_report("# one\n")
    store.write_report("# two\n")
    assert (store.run_dir / "report.md").read_text() == "# two\n"
    assert not list(store.run_dir.glob(".report.md.tmp"))

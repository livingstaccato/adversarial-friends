"""What a `--mode loop --merge orchestrator` halt persists, and how a
resume rebuilds its starting position from it.

Split out of resume.py, which crossed the then-current line cap: that module
is about APPLYING an orchestrator response, this one is about the
loop-level bookkeeping around a halt -- where a resumed iteration re-enters,
what it inherits, and what a halt writes down so a NEW process (a
`--resume` is always a new process) can reconstruct state that would
otherwise live only in memory and vanish the moment the halted process
exits.

Everything here follows one rule: state that must survive a halt has to be
either already in the ledger (verdicts, aliases, claims -- read back by
whoever needs them) or explicitly written into `run.json` by `write_halt`
and explicitly read back by `loop_position`/`resumed_streak`. Several
defects here were exactly the second half of that being missing -- a value
computed and held in memory, never written, so every resume reconstructed
it as zero or as a guess.
"""

import argparse
from typing import Any

from .. import verdicts as vd
from ..ceilings import Budget
from ..failures import RepeatTracker
from ..report import render
from ..reviewstate import ReviewState
from ..runstore import RunStore
from .crossexam import CrossexamOutcome


def carried_outcome(review: ReviewState, meta: dict[str, Any]) -> "CrossexamOutcome | None":
    """The previous iteration's outcome, rebuilt from what the run recorded.

    A `loop` iteration inherits states, verdicts, notes and discard
    signatures (§7.3). None of that survives in memory across an
    orchestrator halt, and all of it survives on disk: states and notes in
    `run.json`, verdicts in the ledger, and signatures are a pure function
    of the verdicts. Rebuilt rather than re-derived by re-judging, which
    would spend a fan-out to recompute something already written down.

    Returns None when there is nothing to carry -- a halt in iteration 1,
    before any judging round ran.
    """
    states = meta.get("claim_states") or {}
    if not states:
        return None
    outcome = CrossexamOutcome()
    outcome.states = dict(states)
    outcome.notes = list(meta.get("amendment_notes") or [])
    outcome.incomplete = bool(meta.get("incomplete"))
    outcome.verdicts = list(review.verdicts)
    outcome.signatures = {
        claim_id: vd.verdict_set_signature(outcome.verdicts, claim_id)
        for claim_id, state in outcome.states.items()
        if state == vd.UNPROVEN
    }
    return outcome


def loop_position(
    args: argparse.Namespace, review: ReviewState, resuming: bool
) -> tuple[int, int, "CrossexamOutcome | None"]:
    """Where a resumed `loop` re-enters: iteration, dry-round streak, and
    what that iteration inherits.

    (1, 0, None) for a fresh run and for any mode that does not loop. An
    orchestrator halt happens once per iteration, so a resumed loop that
    started over at iteration 1 would repeat work already adjudicated, and
    one that treated itself as finished would silently drop the iterations
    the operator asked for.
    """
    if not resuming or args.mode != "loop":
        return 1, 0, None
    meta = getattr(args, "_resume_meta", {}) or {}
    return (
        int(getattr(args, "_resume_iteration", 1) or 1),
        int(getattr(args, "_resume_streak", 0) or 0),
        carried_outcome(review, meta),
    )


def write_halt(
    args: argparse.Namespace,
    store: RunStore,
    meta: dict[str, Any],
    review: ReviewState,
    iteration: int,
    streak: int,
    carry_over: "CrossexamOutcome | None",
    round_dry: bool = False,
    round_failed: bool = False,
    budget: Budget | None = None,
    tracker: RepeatTracker | None = None,
) -> None:
    """Leave behind a run directory a resume can actually continue from.

    A halt in a `loop` must record everything the resumed iteration
    inherits, or it re-enters knowing only that it was interrupted: which
    iteration it was in, the dry-round streak, and whatever earlier
    iterations already decided. The completion path writes these; the halt
    path did not, which is what made `--merge orchestrator` unusable with
    `--mode loop` and is why that combination was refused rather than
    supported.

    **`budget.calls` too, and not gated on `--mode loop`.** `Budget` is an
    in-memory dataclass, and `--resume` starts a new process with a fresh
    one at `calls=0` -- nothing else here restores it. Without persisting
    the true spend, `resume_round_one` fell back to charging back only the
    ONE round it was directly resuming, which is right for a run halting on
    its very first round ever and silently wrong for every halt after: a
    `--mode loop --merge orchestrator` run halting once per iteration forgot
    every earlier iteration's cost at every resume, so a 5-iteration loop
    could blow past `--max-calls` by a large multiple with the ceiling never
    firing -- each resuming process believed only its own round 1 had ever
    run.
    """
    if budget is not None:
        meta["spent_calls"] = budget.calls
    if tracker is not None:
        # Same failure as Budget.calls, same fix: a RepeatTracker also
        # lives only in the process that built it. Without this, a friend
        # disabled for repeated failure in an earlier iteration came back
        # after every resume.
        meta["repeat_tracker"] = tracker.snapshot()
    if args.mode == "loop":
        meta["iterations_run"] = iteration
        meta["dry_streak"] = streak
        # Whether the critique round that ran just before this halt learned
        # anything. Without these two, the resumed iteration had nothing to
        # compute dryness from and hard-coded `round_is_dry(False, True)` --
        # which is always False, so `next_streak` zeroed the very streak
        # this function had just persisted. `loop_should_terminate` needs
        # two consecutive dry rounds, so a resumed loop could not converge
        # at all: it ran to --max-loop-iterations, paying a full fan-out per
        # iteration on a run that had already stopped learning.
        meta["halted_round_dry"] = round_dry
        meta["halted_round_failed"] = round_failed
    if carry_over is not None:
        meta["claim_states"] = carry_over.states
        meta["amendment_notes"] = carry_over.notes
        meta["incomplete"] = carry_over.incomplete
    downgrades = meta.setdefault("downgrades", [])
    review.copy_transition_warnings(downgrades)
    store.write_run_json(meta)
    store.write_report(
        render(
            review,
            meta,
            # c-0006: `meta["claim_states"]` was set from `carry_over`
            # above, but never reached `render`, whose verdict sections
            # only draw from these two kwargs. A halt mid-loop showed raw
            # findings with none of the states or judges' reasoning earlier
            # iterations had already produced -- present in `carry_over`
            # and in the ledger the whole time, just never handed to the
            # renderer that displays them.
            states=carry_over.states if carry_over is not None else None,
        )
    )
    print(store.run_dir)


def resumed_streak(args: argparse.Namespace, streak: int) -> int:
    """The dry-round streak after a resumed iteration completes.

    Computed from what the halted round actually did, which `write_halt`
    persists. It used to be `next_streak(streak, failed=False,
    dry=round_is_dry(False, True))` -- and `round_is_dry(False, True)` is
    always False, so this zeroed the very streak `loop_position` had just
    restored, on every resume. `loop_should_terminate` needs two consecutive
    dry rounds, so a resumed loop could not converge at all: it ran to
    `--max-loop-iterations`, paying a full fan-out per iteration on a run
    that had already stopped learning anything.

    Absent keys read as "failed, not dry". That is the honest default for a
    run halted by claim extraction, where the round's output could not be
    parsed -- and for a halt written by an older version, where assuming
    convergence would be the dangerous guess.
    """
    meta = getattr(args, "_resume_meta", {}) or {}
    failed = bool(meta.get("halted_round_failed", True))
    dry = bool(meta.get("halted_round_dry", False))
    return vd.next_streak(streak, failed=failed, dry=vd.round_is_dry(dry, not failed))

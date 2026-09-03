"""Construct terminal run outcomes from completed review state."""

from collections.abc import Mapping
from typing import Any

from ..ceilings import BUDGET_EXHAUSTED, Budget
from ..outcomes import RunOutcome, terminal_outcome
from ..verdicts import TERMINAL_STATES


def finalize_meta(
    meta: dict[str, Any],
    *,
    budget: Budget,
    downgrades: list[str],
    cross: Any,
) -> dict[str, Any]:
    """Fold every mode's completed-run fields into metadata in place."""
    if budget.exhausted_by:
        reason = f"{BUDGET_EXHAUSTED}: {budget.exhausted_by}"
        if reason not in downgrades:
            downgrades.append(reason)
    if cross is not None:
        meta["claim_states"] = cross.states
        meta["amendment_notes"] = cross.notes
        meta["incomplete"] = cross.incomplete
    return meta


def build_terminal_outcome(
    *,
    mode: str,
    cross: Any,
    loop_converged: bool,
    loop_exhausted: bool,
    budget: Budget,
    any_success: bool,
    auth_abort: str | None,
    abort_signum: int | None,
    runtime_error: str | None,
    require_friends: int | None,
    succeeded_friends: int | None,
    blocking_ids: list[str],
    started_at: str,
    finished_at: str,
    active_elapsed_s: float,
    iterations_run: int,
    rounds_reached: int,
    streak: int,
    repeat_tracker: Mapping[str, object],
) -> tuple[RunOutcome, bool]:
    """Return the durable outcome and whether the requested quorum failed."""
    unresolved = bool(
        cross is not None
        and (
            cross.incomplete or any(state not in TERMINAL_STATES for state in cross.states.values())
        )
    )
    quorum_failed = bool(
        require_friends is not None
        and succeeded_friends is not None
        and succeeded_friends < require_friends
    )
    outcome = terminal_outcome(
        mode=mode,
        converged=(
            loop_converged
            if mode == "loop"
            else any_success
            and not unresolved
            and auth_abort is None
            and abort_signum is None
            and budget.exhausted_by is None
            and runtime_error is None
        ),
        loop_exhausted=loop_exhausted,
        budget_reason=budget.exhausted_by,
        blocking_ids=blocking_ids,
        any_success=any_success,
        unresolved=unresolved,
        auth_abort=auth_abort is not None,
        abort_signum=abort_signum,
        runtime_error=runtime_error is not None,
        quorum_failed=quorum_failed,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=active_elapsed_s,
        attempted_calls=budget.calls,
        spent_calls=budget.calls,
        iterations_run=iterations_run,
        rounds_run=max(rounds_reached, cross.rounds_run if cross is not None else 0),
        dry_streak=streak,
        repeat_tracker=repeat_tracker,
    )
    return outcome, quorum_failed


def _terminal_event_summary(stop_reason: str) -> tuple[str, str]:
    """Project terminal state into the intentionally small event vocabulary."""
    if stop_reason == "completed":
        return "completed", "inspect_report"
    if stop_reason == "gate-blocked":
        return "blocked", "resolve"
    if stop_reason in {"max-loop-iterations", "max-calls", "max-wall-clock", "interrupted"}:
        return "incomplete", "resume"
    if stop_reason == "auth-abort":
        return "halted", "fix_configuration"
    if stop_reason == "runtime-error":
        return "error", "inspect_report"
    return "incomplete", "inspect_report"

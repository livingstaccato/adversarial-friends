"""Run ceilings and the budget that enforces them -- spec §7.4.

Hitting any ceiling is `budget-exhausted`: neither success nor convergence,
and distinct from both. §7.6 puts it above every gate outcome in the exit
precedence, because a truncated run has not evaluated the gate -- a CI
wrapper can then treat exit 11 as "retry" and exit 1 as "block" without
having to guess which happened.

**`--max-calls` is derived, never a constant.** Version 2 of the design
hard-coded 60, which is exactly `4 x 3 x 5` -- so the default configuration
tripped its own ceiling mid-run the moment a fourth friend was present. A
ceiling that the tool's own defaults violate is worse than no ceiling: it
converts a normal run into a truncated one and reports budget exhaustion for
a budget nobody chose.
"""

import dataclasses
from dataclasses import dataclass, field
import math

from .adapters import FriendSpec
from .dispatch import KILL_GRACE_S

# §7.4's defaults.
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_LOOP_ITERATIONS = 5
DEFAULT_MAX_WALL_CLOCK_S = 7200
# How many friends may be dispatched at once. Every one of them costs a
# thread, a child process (or HTTP request), and an isolation directory --
# a `git worktree add` for a repo-scope friend. Unbounded, a large generated
# roster started all of them at once and could exhaust file descriptors,
# memory, or a provider's rate limit before repeat detection saw a single
# failure. Eight is deliberately conservative: a hand-written roster never
# reaches it, so the bound only ever engages where it is the difference
# between a slow round and a failed one.
DEFAULT_MAX_CONCURRENCY = 8

# The headroom factor in §7.4's derivation. Above 1.0 because a friend
# process invocation counts even when it is a re-invocation after a resume,
# and because a round may dispatch a successor claim's judges in addition to
# the friends that produced it.
CALL_HEADROOM = 1.5

BUDGET_EXHAUSTED = "budget-exhausted"


def derive_max_calls(
    friends: int,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    max_loop_iterations: int = DEFAULT_MAX_LOOP_ITERATIONS,
) -> int:
    """§7.4: `ceil(friends x max_rounds x max_loop_iterations x 1.5)`.

    `crossexam` passes `max_loop_iterations=1` -- it runs one iteration by
    definition, and budgeting it for five would make the ceiling
    unreachable, which is its own kind of useless.
    """
    return math.ceil(friends * max_rounds * max_loop_iterations * CALL_HEADROOM)


@dataclass
class Budget:
    """Tracks what a run has spent against its ceilings.

    Wall clock is checked against an injected `now` rather than read from the
    clock directly, so a test can exercise the ceiling without waiting two
    hours for it.
    """

    max_calls: int
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_wall_clock_s: float = DEFAULT_MAX_WALL_CLOCK_S
    calls: int = 0
    started: float = 0.0
    exhausted_by: str | None = field(default=None)

    def spend(self, calls: int) -> None:
        self.calls += calls

    def would_exceed_calls(self, calls: int) -> bool:
        """Checked BEFORE a round is dispatched, not after.

        Dispatching a round and then noticing the ceiling was crossed
        spends the very budget the ceiling exists to protect -- and for a
        metered agent CLI that is real money. A round that will not fit is
        not started.
        """
        return self.calls + calls > self.max_calls

    def out_of_time(self, now: float) -> bool:
        return now - self.started >= self.max_wall_clock_s

    def seconds_left(self, now: float) -> float:
        """What remains of the wall-clock ceiling, never below zero.

        The ceiling was sampled only between rounds, so a friend dispatched
        one second before it expired ran for its own full timeout -- 900
        seconds by default, plus the kill grace -- and a run that finished
        in that round reported no ceiling hit at all. Capping each friend's
        timeout at what is left makes the ceiling bound the run rather than
        the gaps between its rounds.
        """
        return max(0.0, self.started + self.max_wall_clock_s - now)

    def exhaust(self, reason: str) -> None:
        # First ceiling hit wins: it is the one that actually truncated the
        # run, and a later check crossing too says nothing new.
        if self.exhausted_by is None:
            self.exhausted_by = reason


def warn_if_unreachable(friends: int, max_rounds: int, max_calls: int) -> str | None:
    """§7.4: "the runner emits a startup warning when configured ceilings
    cannot accommodate the configured mode."

    Emitted at startup rather than discovered at round 3, so an operator who
    tightened `--max-calls` learns that the run cannot finish before paying
    for the part of it that can.
    """
    needed = friends * max_rounds
    if max_calls >= needed:
        return None
    return (
        f"--max-calls={max_calls} cannot accommodate {friends} friends over "
        f"{max_rounds} rounds ({needed} calls minimum); this run will stop at "
        "a ceiling before reaching its configured round limit."
    )


def within_deadline(specs: list[FriendSpec], seconds_left: float) -> list[FriendSpec]:
    """Every spec, with its timeout capped at what remains of the run's
    wall-clock ceiling. A friend dispatched just under the ceiling used to
    run for its own full timeout past it.

    The cap subtracts `KILL_GRACE_S`, because dispatch hands `run_process` a
    kill deadline of `spec.timeout + KILL_GRACE_S` -- a full extra minute. A
    cap that ignored it made the wall-clock ceiling a ceiling only for
    friends that behaved: a single hung friend overshot it by that minute,
    plus the group's own escalation windows. Reserving the grace up front
    costs a well-behaved friend some of its timeout and makes the ceiling
    mean what it says, which is the trade the ceiling exists to make.

    Below one whole second of usable time, nothing is dispatched at all.
    `int()` floors, so 0.6s remaining became a timeout of 0 -- a friend
    launched only to be killed the instant it started, which still spends a
    call from the budget and still reports as a failure that marks the run
    incomplete. Worse for an adapter with an `internal_timeout_flag`: agy is
    handed `--print-timeout 0s` and self-times-out before it reaches a model.
    There is no honest dispatch left in under a second. Returning nothing
    lets the caller say so plainly instead.

    **Lives here, not in `commands/judging.py`, because both round types need
    it.** It was written for judging rounds and the critique round kept an
    inline `min()` that did neither correction -- so the ceiling meant one
    thing for a judging round and another for the critique round that
    preceded it. Two independent friends raised that asymmetry as a defect
    from two different lenses, which is what an asymmetric fix looks like
    from outside.
    """
    remaining = int(seconds_left) - KILL_GRACE_S
    if remaining < 1:
        return []
    return [dataclasses.replace(s, timeout=min(s.timeout, remaining)) for s in specs]

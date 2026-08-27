"""§7.6's exit precedence, in one place.

When several conditions hold at once the first match wins, and the order is
not arbitrary: a ceiling outranks every gate outcome because a truncated run
has not evaluated the gate, which is what lets a CI wrapper treat 11 as
"retry" and 1 as "block" without ambiguity.

Split out of commands/run.py for the line cap. Keeping it whole also keeps
the precedence readable as a single ordered list rather than as branches
scattered through the end of a long function.
"""

import sys

from .. import verdicts as vd
from ..errors import CeilingError
from ..ledger import Claim
from .crossexam import CrossexamOutcome


def decide_exit(
    abort_signum: int | None,
    any_success: bool,
    mode: str,
    cross: CrossexamOutcome | None,
    blocking: list[Claim],
    ceiling_hit: str | None = None,
) -> int:
    if abort_signum is not None:
        # Distinct from both branches below: a run cancelled by signal
        # is neither "succeeded" (0) nor merely "incomplete because
        # every friend failed on its own" (1) -- it never got the
        # chance to finish at all. 128+signum is the conventional
        # shell convention for "killed by signal N" and does not
        # collide with any of this tool's other exit codes (2, 3, 10,
        # 11, 1, 0).
        print(f"afriend: aborted by signal {abort_signum}", file=sys.stderr)
        return 128 + abort_signum
    # §7.6's exit precedence. A ceiling outranks every outcome below it
    # because a truncated run has not evaluated anything: a CI wrapper
    # can then treat 11 as "retry" and 1 as "block" without ambiguity.
    # `ceiling_hit` is the run-level one: a budget exhausted in the
    # iteration loop, before any crossexam existed to record it. Read only
    # from the crossexam outcome, that run reported a plain exit 1 -- a
    # ceiling the operator set, hit, and never told about. Found while
    # writing the first test that actually reaches the wall-clock branch.
    hit = ceiling_hit or (cross.ceiling_hit if cross is not None else None)
    if hit is not None:
        print(f"afriend: {hit}", file=sys.stderr)
        return CeilingError.exit_code
    # A run where not one friend produced a usable result (every round
    # failed/timed out) is not a success -- exit 1 ("gate blocked or
    # incomplete") rather than 0, so a caller cannot mistake "we ran the
    # mechanism" for "we got a trustworthy critique". Distinct from
    # NoFriendsError's exit 3, which fires before any friend is even
    # dispatched.
    if not any_success:
        return 1
    if mode == "gate" and blocking:
        print(
            f"afriend: gate blocked -- {len(blocking)} claim(s) need a resolution: "
            + ", ".join(c.id for c in blocking),
            file=sys.stderr,
        )
        return 1
    if cross is not None:
        # A crossexam run that left claims undecided, or that lost a
        # required friend mid-round (§7.2's M12), is incomplete. Only a
        # run that actually reached terminal states for everything
        # reports success.
        unresolved = [s for s in cross.states.values() if s not in vd.TERMINAL_STATES]
        if cross.incomplete or unresolved:
            return 1
    return 0

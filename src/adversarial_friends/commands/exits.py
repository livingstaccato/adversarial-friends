"""Console compatibility glue for an already-decided terminal outcome."""

import sys

from ..outcomes import RunOutcome, StopReason


def decide_exit(outcome: RunOutcome, detail: str | None = None) -> int:
    """Print the outcome for humans and return its authoritative exit code."""
    if outcome.stop_reason is StopReason.INTERRUPTED:
        print(f"afriend: aborted by signal {outcome.exit_code - 128}", file=sys.stderr)
    elif detail:
        print(f"afriend: {detail}", file=sys.stderr)
    elif outcome.ceiling_hit is not None:
        print(f"afriend: {outcome.ceiling_hit}", file=sys.stderr)
    elif outcome.stop_reason is StopReason.GATE_BLOCKED:
        print(
            f"afriend: gate blocked -- {len(outcome.blocker_ids)} claim(s) "
            "need a resolution: " + ", ".join(outcome.blocker_ids),
            file=sys.stderr,
        )
    return outcome.exit_code

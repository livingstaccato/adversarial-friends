"""Where a run's repository is, and how a run is aborted cleanly.

Split out of commands/run.py to keep it under this repo's 500-line cap. Both
concerns are about the *environment* a run happens in rather than the run
loop itself.
"""

from collections.abc import Callable
import concurrent.futures
import contextlib
from pathlib import Path
import signal
import subprocess
import threading
from types import FrameType
from typing import Any

# The type signal.signal() both accepts and returns, per typeshed: a handler
# callable, a raw int (SIG_IGN/SIG_DFL's underlying value), or None.
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None


def _resolve_repo_root(artifact: Path) -> Path | None:
    """Return the git repository root enclosing `artifact`, or None if it
    is not inside a git repository at all.

    isolation.snapshot_commit requires a repository ROOT and raises AfError
    for a nested subdirectory (naming the real root). Resolving the root
    here -- via the artifact's own enclosing directory, not Path.cwd() --
    means snapshot_commit is only ever called with a value it will accept,
    regardless of how deeply nested the artifact is inside the repo, and
    regardless of what directory `afriend` itself happens to be invoked
    from.
    """
    result = subprocess.run(
        ["git", "-C", str(artifact.resolve().parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def install_abort_handlers(
    abort_event: threading.Event,
    abort_signum: dict[str, int | None],
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None],
) -> dict[int, _SignalHandler]:
    """Make a cancelled run tear its own isolation down.

    A cancelled or CI-killed run must not leave a metered agent CLI running
    unbounded, nor a stale `git worktree` registration in the repo under
    review. Neither signal would otherwise give cmd_run's `finally` blocks a
    chance to run: SIGTERM's default disposition kills the process outright
    with no Python-level unwinding, and SIGINT's KeyboardInterrupt, once it
    propagates out of the blocked `pool.map()`, immediately re-blocks inside
    ThreadPoolExecutor.__exit__'s own `shutdown(wait=True)` waiting on the
    same hung worker.

    So the handler sets the event AND shuts the pool down without waiting.
    Returns the handlers it actually replaced, so the caller restores exactly
    those -- a library-ish function should not permanently hijack
    process-wide signal disposition.

    signal.signal() only works on the main thread of the main interpreter and
    raises ValueError anywhere else. A non-main-thread caller is a real
    audience here, so that degrades rather than crashing; the caller reports
    the reduced guarantee as a downgrade.
    """

    aborting = [False]

    def _handle_abort(signum: int, frame: FrameType | None) -> None:
        # Re-entrancy guard, and it has to come before anything that takes a
        # lock. A second signal pending while this handler's first
        # invocation is inside `abort_event.set()` -- which holds the
        # Event's plain, non-reentrant Lock -- runs the handler again at the
        # next eval-breaker point, nested, on the same thread; the nested
        # `set()` then blocks on the lock its own caller holds, forever.
        # Found as a two-day-old process with five invocations nested on the
        # main thread, and reproduced with three back-to-back SIGTERMs; GNU
        # coreutils `timeout` alone sends two (to the pid, then the group).
        # Later signals are dropped rather than escalated: teardown is
        # already under way, and SIGTERM's default disposition would end the
        # process before it finished. tests/abort_reentry_probe.py forces
        # the interleaving.
        if aborting[0]:
            return
        aborting[0] = True
        abort_signum["value"] = signum
        abort_event.set()
        pool = active_pool[0]
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    installed: dict[int, _SignalHandler] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):
            installed[sig] = signal.signal(sig, _handle_abort)
    return installed

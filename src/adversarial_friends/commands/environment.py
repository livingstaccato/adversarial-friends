"""Where a run's repository is, and how a run is aborted cleanly.

Split out of commands/run.py to keep it under the then-current line cap. Both
concerns are about the *environment* a run happens in rather than the run
loop itself.
"""

from collections.abc import Callable
import concurrent.futures
import contextlib
import dataclasses
import os
from pathlib import Path
import signal
import subprocess
import threading
from types import FrameType
from typing import Any

from ..adapters import FriendSpec
from ..errors import UsageError
from ..runstore import RunStore
from ..snapshots import SnapshotIdentity

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
    # `artifact.parent.resolve()`, not `artifact.resolve().parent`: the
    # directory the user named, with the final artifact symlink NOT
    # followed. Following it decided the repository from the symlink's
    # target, so `repo-A/docs/spec.md -> repo-B/spec.md` snapshotted repo-B
    # and repo-scope friends read a codebase the operator never named --
    # and a link to a file outside any repository silently downgraded the
    # whole run to doc scope. The invocation path picks the context; the
    # link's target supplies only the bytes.
    result = subprocess.run(
        ["git", "-C", str(artifact.parent.resolve()), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def reconcile_snapshot_scope(
    artifact: Path,
    identity: SnapshotIdentity,
    specs: list[FriendSpec],
    downgrades: list[str],
) -> tuple[Path | None, list[FriendSpec]]:
    """Make dispatch scope agree with the identity that was actually captured.

    The invocation path can appear to be in a repository while its final
    symlink target is outside it. Snapshot creation correctly refuses to
    claim a repository identity in that case; this is the corresponding
    dispatch policy. It is also applied to loop successors, because a link
    can be retargeted between iterations.
    """
    if identity.repo_root is not None:
        return identity.repo_root, specs
    note = (
        f"{artifact.name} is not inside a git repository; every friend was "
        "downgraded to doc scope (no repository to snapshot or read)."
    )
    if note not in downgrades:
        downgrades.append(note)
    return None, [dataclasses.replace(spec, scope="doc") for spec in specs]


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


@dataclasses.dataclass(frozen=True)
class Revision:
    """The exact bytes an iteration reviews, and the commit behind them."""

    frozen: Path
    digest: str
    text: str
    identity: SnapshotIdentity
    downgrade: str | None

    @property
    def snapshot_sha(self) -> str | None:
        """Compatibility view for the dispatch APIs that still take a SHA."""
        return self.identity.commit


def freeze_revision(
    store: "RunStore",
    artifact: Path,
    frozen: Path,
    digest: str,
    resuming: bool,
    last_digest: str | None,
    identity: SnapshotIdentity,
    iteration: int,
) -> Revision:
    """Freeze what this iteration will review, and say if it changed.

    §4.1 lists "the frozen artifact" among a friend's three inputs, and
    `artifact_hash` in run.json attests to exactly those bytes. Reading the
    live path at dispatch instead made the frozen copy dead weight: an
    artifact edited between a halt and a resume was judged while run.json
    still reported the original hash, and `afriend resolve` compared named
    locations against a copy nobody had reviewed.

    A `loop` still picks up a revision (§7.3) -- it re-freezes, so the copy,
    the hash and what friends read stay the same bytes. A resumed run reads
    the copy its ledger was written against and re-freezes nothing.

    When the bytes changed, two things follow. Terminal states decided
    against the old text are not decisions about the new one, so the caller
    drops what it was carrying. And the repository has to move with the
    text: the snapshot was taken once before the loop, so re-reading the
    artifact each iteration asked friends to judge NEW wording while
    repo-scope friends were checked out at the OLD commit -- claim and
    evidence from two revisions, in one verdict.
    """
    if not resuming:
        frozen, digest = store.artifact_copy(artifact)
    text = frozen.read_text(encoding="utf-8")
    comparison_digest = last_digest if last_digest is not None else identity.artifact_hash
    if digest == comparison_digest:
        return Revision(frozen, digest, text, identity, None)
    predecessor = identity.commit or identity.artifact_hash
    identity = SnapshotIdentity.create(
        identity.repo_root,
        frozen,
        digest,
        predecessor=predecessor,
        source_artifact=artifact,
    )
    return Revision(
        frozen,
        digest,
        text,
        identity,
        f"the artifact changed before iteration {iteration}; claims settled against "
        "the earlier text were re-opened and judged again, since a revision can "
        "decide them differently, and the repository was re-snapshotted so friends "
        "read the same revision the prompt quotes.",
    )


CLOCK_OFFSET_VAR = "AF_CLOCK_OFFSET_S"


def clock_offset(downgrades: list[str]) -> float:
    """Seconds to add to every clock reading this run takes.

    An injection point so an end-to-end test can reach the wall-clock branch
    without waiting two hours. The arithmetic under test is the same
    arithmetic; unset, `now()` is `time.monotonic` exactly.

    Two things this used to get wrong, both from being a bare `float(...)`
    on an environment variable in the middle of `cmd_run`:

    * **A malformed value raised a bare `ValueError`** out of the command as
      a traceback rather than a `UsageError` with a message.
    * **A set value was invisible.** It shortens the wall-clock ceiling, so
      an ambient value in CI -- a leaked export from a test job, a name
      collision -- made a run report `budget-exhausted` and exit 11 while
      the downgrade blamed `--max-wall-clock`, a ceiling the operator had
      set correctly. Nothing anywhere said the clock had been moved.

    So it is still read unconditionally, because gating a test hook behind a
    flag only moves the problem, but a run it affects now says so in its own
    report.
    """
    raw = os.environ.get(CLOCK_OFFSET_VAR, "") or ""
    if not raw:
        return 0.0
    try:
        offset = float(raw)
    except ValueError:
        raise UsageError(
            f"{CLOCK_OFFSET_VAR}={raw!r} is not a number. It is a test hook that "
            "shifts this run's clock; unset it unless you meant to set it."
        ) from None
    downgrades.append(
        f"{CLOCK_OFFSET_VAR}={offset} shifted this run's clock forward, so every "
        "wall-clock ceiling was that much shorter than it appears. This is a test "
        "hook -- unset it unless you meant to set it."
    )
    return offset

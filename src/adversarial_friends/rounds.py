"""One round of fan-out: isolate every friend, dispatch in parallel, collect.

Extracted from commands/run.py when cross-examination arrived. A judging
round needs exactly the same machinery a critique round does -- a private
worktree or doc directory per friend, a thread per friend, teardown that runs
whatever happens, and an abort path that does not wait out a hung friend's
full timeout -- and the alternative was a second copy of it in commands/
diverging from the first.

Nothing here knows what a friend was asked. It takes a prompt file per friend
and returns a SpawnResult per friend; whether that prompt held a critique
contract or a verdict contract is the caller's business.
"""

from collections.abc import Callable, Iterator
import concurrent.futures
import contextlib
from pathlib import Path
import tempfile
import threading
from typing import Any

from . import isolation
from .adapters import Adapter, Capability, FriendSpec
from .ceilings import DEFAULT_MAX_CONCURRENCY
from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .dispatch import _UNKNOWN_CAPABILITY, _dispatch, _exception_outcome, _stderr_tail
from .errors import AfError
from .failures import AUTH, RepeatTracker, auth_abort_message, classify
from .runstore import RunStore
from .spawn import SpawnResult

RoundResult = tuple[FriendSpec, Capability, SpawnResult]


@contextlib.contextmanager
def _isolation_root(store: RunStore, round_no: int, keep: bool) -> Iterator[Path]:
    if keep:
        root = store.run_dir / "isolation" / f"round-{round_no}"
        root.mkdir(parents=True, exist_ok=True)
        yield root
        return
    with tempfile.TemporaryDirectory(prefix=f"af-isolation-r{round_no}-") as path:
        yield Path(path)


def dispatch_round(
    specs: list[FriendSpec],
    round_no: int,
    prompt_for: dict[str, Path],
    store: RunStore,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    schema_file: Path,
    artifact: Path,
    repo_root: Path | None,
    snapshot_sha: str | None,
    abort_event: threading.Event,
    on_pool: Callable[[concurrent.futures.ThreadPoolExecutor | None], None] = lambda _pool: None,
    contract: PayloadContract = CLAIM_CONTRACT,
    allow_unsandboxed: bool = False,
    tracker: RepeatTracker | None = None,
    downgrades: list[str] | None = None,
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> list[RoundResult]:
    """Run every friend in `specs` concurrently and return their outcomes.

    Every friend gets its own private working directory, torn down at the end
    regardless of how dispatch finishes (including on a raised exception, or
    an abort mid-setup). Repo-scope friends run inside their own git worktree
    checked out from one shared snapshot commit; every other friend runs
    inside its own bare doc_scope_dir holding only a copy of the artifact.

    Giving every repo-scope friend its own worktree (rather than one shared
    checkout) is a deliberately stricter reading of "every friend that lacks
    a readonly capability gets its own private worktree": it trivially
    satisfies that bar and removes any question of two friends racing each
    other inside one worktree, at the cost of one `git worktree add` per
    friend. The run directory itself is never nested inside any of these.

    `on_pool` hands the live executor to the caller's signal handler and is
    called again with None on the way out. A handler that only sets
    `abort_event` is not enough on its own: the main thread is blocked inside
    pool.map() waiting on the same hung worker, so the handler must also be
    able to shut the pool down without waiting.

    `round_no` selects which round directory this round's isolation belongs
    to conceptually, but no file is written here -- see persist_result.
    """
    # §14/§7.2. Both rules live here because this is the one place every
    # round type dispatches through -- a critique round and a judging round
    # would otherwise need separate, drifting copies.
    if tracker is not None:
        skipped = [s for s in specs if tracker.is_disabled(s.name)]
        for spec in skipped:
            note = tracker.note(spec.name)
            if downgrades is not None and note not in downgrades:
                downgrades.append(note)
        specs = [s for s in specs if not tracker.is_disabled(s.name)]
        if not specs:
            return []

    results: list[RoundResult] = []
    # §12.4: isolation is torn down at run end unless --keep. A
    # TemporaryDirectory would remove the tree regardless, leaving a "kept"
    # worktree registered at a path that no longer exists -- worse than not
    # keeping it. So --keep puts isolation inside the run directory, which
    # persists, and leaves the git worktrees registered for `git worktree
    # list` to find and `afriend doctor --gc` to clean up later.
    with _isolation_root(store, round_no, keep) as iso_root:
        cwd_for: dict[str, Path] = {}
        try:
            for spec in specs:
                if abort_event.is_set():
                    break
                dest = iso_root / spec.name
                if spec.scope == "repo":
                    # A spec only reaches scope="repo" when repo_root was not
                    # None (the caller downgrades to "doc" otherwise), and
                    # any repo-scope spec is exactly what makes the caller
                    # take a snapshot -- both are non-None by construction.
                    assert repo_root is not None
                    assert snapshot_sha is not None
                    isolation.add_worktree(repo_root, snapshot_sha, dest)
                else:
                    isolation.doc_scope_dir(dest, artifact)
                cwd_for[spec.name] = dest

            def _run_one(spec: FriendSpec) -> RoundResult:
                # spawn.run_process already turns most process-launch failures
                # (missing binary, E2BIG, ENOEXEC, ...) into a SpawnResult
                # rather than raising. This is the second, broader layer:
                # ANYTHING else that goes wrong for one friend must not end
                # the whole round. pool.map collects one return value per
                # future; an exception escaping here would propagate out of
                # pool.map entirely, losing every other friend's
                # (possibly already-succeeded) result along with it. A
                # deliberate AfError (e.g. check_denied_values refusing a
                # dangerous flag) is a real stop condition with its own exit
                # code -- that still propagates.
                try:
                    return _dispatch(
                        spec,
                        cwd_for[spec.name],
                        registry,
                        fake_cmd,
                        prompt_for[spec.name],
                        schema_file,
                        abort_event,
                        contract,
                        allow_unsandboxed,
                        extra_args,
                        pass_env,
                    )
                except AfError:
                    raise
                except Exception as exc:
                    return spec, _UNKNOWN_CAPABILITY, _exception_outcome([], exc)

            # Only specs that actually got an isolation directory are
            # dispatched -- _run_one would otherwise KeyError looking up
            # cwd_for for a spec whose setup never happened.
            dispatch_specs = [s for s in specs if s.name in cwd_for]
            if not dispatch_specs:
                return []
            # Bounded: see ceilings.DEFAULT_MAX_CONCURRENCY. `pool.map`
            # still returns results in `dispatch_specs` order, so a round's
            # aggregation is unchanged -- only how many friends are in
            # flight at once.
            workers = max(1, min(max_concurrency, len(dispatch_specs)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                on_pool(pool)
                try:
                    results = list(pool.map(_run_one, dispatch_specs))
                finally:
                    on_pool(None)
        finally:
            if not keep:
                for spec in specs:
                    if spec.scope == "repo" and spec.name in cwd_for:
                        assert repo_root is not None
                        isolation.remove_worktree(repo_root, cwd_for[spec.name])
            # doc_scope_dir entries need no explicit cleanup: they all live
            # under iso_root, which the TemporaryDirectory context manager
            # removes on exit independent of whether dispatch raised.

    if tracker is not None:
        for spec, _capability, outcome in results:
            tracker.record(spec.name, outcome)
            # §7.2: an auth failure is deterministic, so every remaining
            # round and iteration would fail identically. Stop now rather
            # than spending them. Raised only on a DECLARED marker -- an
            # unrecognised failure is never guessed into an abort, because
            # a false auth classification ends the whole run.
            if classify(outcome, registry.get(spec.cli)) == AUTH:
                raise AfError(auth_abort_message(spec.name, registry.get(spec.cli)))
    return results


def persist_result(
    store: RunStore,
    round_no: int,
    spec: FriendSpec,
    capability: Capability,
    outcome: SpawnResult,
) -> dict[str, Any]:
    """Write one friend's raw output, stderr and metadata; return its row.

    stderr is persisted unconditionally, even when empty -- a stable,
    always-present file beats one that only sometimes exists -- with a short
    tail folded into the status column for a failed friend so the diagnosis
    is visible without opening a second file. Before this, an unauthenticated
    friend showed up as "failed: exit 1" with a 0-byte .raw and no diagnosis
    anywhere.
    """
    raw_path, _json_path, meta_path = store.friend_paths(round_no, spec.name)
    raw_path.write_text(outcome.stdout, encoding="utf-8")
    meta_path.write_text(
        f"argv={outcome.argv}\nexit={outcome.exit_code}\n"
        f"duration_s={outcome.duration_s:.2f}\ntimed_out={outcome.timed_out}\n"
        f"orphans_suspected={outcome.orphans_suspected}\n"
        f"stopped_after_answer={outcome.stopped_after_answer}\n"
        # Its justification is that a reader comparing a short stdout against
        # a long duration cannot otherwise tell truncation from a friend that
        # said little. That only holds if the reader can SEE it -- and it was
        # recorded on the result and written nowhere, unlike every sibling
        # flag on this line.
        f"output_truncated={outcome.output_truncated}\n",
        encoding="utf-8",
    )
    err_path = store.friend_err_path(round_no, spec.name)
    err_path.write_text(outcome.stderr, encoding="utf-8")

    status = "ok" if outcome.failure_reason is None else f"failed: {outcome.failure_reason}"
    if outcome.failure_reason is not None and outcome.stderr.strip():
        status += (
            f" (stderr: {_stderr_tail(outcome.stderr)}; "
            f"full text in round-{round_no}/{spec.name}.err)"
        )
    if outcome.orphans_suspected:
        # A leaked descendant must not look identical to a clean run --
        # surfaced in the same status column readers already check for
        # "failed", rather than a silent field only run.json carries.
        status += " [orphans suspected]"
    return {
        "name": spec.name,
        "model": spec.model,
        "effort": spec.effort,
        "readonly": capability.readonly,
        "scope": spec.scope,
        "round": round_no,
        "status": status,
    }

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

from collections.abc import Callable, Iterator, Sequence
import concurrent.futures
import contextlib
from dataclasses import dataclass
from pathlib import Path
import tempfile
import threading
from typing import Any

from . import isolation
from .adapters import Adapter, Capability, FriendSpec
from .authority import ExternalToolPolicy
from .ceilings import DEFAULT_MAX_CONCURRENCY
from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .dispatch import _UNKNOWN_CAPABILITY, _dispatch, _exception_outcome, _stderr_tail
from .errors import AfError
from .failures import AUTH, RepeatTracker, auth_abort_message, classify
from .progress import Progress, disabled
from .runstore import RunStore
from .spawn import SpawnResult

RoundResult = tuple[FriendSpec, Capability, SpawnResult]


@dataclass(frozen=True)
class SkippedFriend:
    spec: FriendSpec
    reason: str


def partition_dispatchable(
    specs: Sequence[FriendSpec], tracker: RepeatTracker | None
) -> tuple[list[FriendSpec], list[SkippedFriend]]:
    """Separate friends that may run from repeat-disabled audit events."""
    if tracker is None:
        return list(specs), []
    ready: list[FriendSpec] = []
    skipped: list[SkippedFriend] = []
    for spec in specs:
        if tracker.is_disabled(spec.name):
            skipped.append(SkippedFriend(spec, _stderr_tail(tracker.note(spec.name))))
        else:
            ready.append(spec)
    return ready, skipped


def persist_skip(store: RunStore, round_no: int, skipped: SkippedFriend) -> dict[str, Any]:
    """Persist one deliberate non-dispatch as a first-class audit row."""
    _, _, meta_path = store.friend_paths(round_no, skipped.spec.name)
    meta_path.write_text(
        f"status=skipped\nreason={skipped.reason}\n",
        encoding="utf-8",
    )
    return {
        "name": skipped.spec.name,
        "model": skipped.spec.model,
        "effort": skipped.spec.effort,
        "transport": "not-dispatched",
        "write_protected": False,
        "declared_scope": skipped.spec.scope,
        "os_confined": False,
        "external_tools": "not-dispatched",
        "external_tool_policy": "not-dispatched",
        "external_tool_sources": [],
        "deny_external_tools_argv": [],
        "readonly": False,
        "scope": skipped.spec.scope,
        "round": round_no,
        "status": f"skipped: {skipped.reason}",
    }


def _outcome_word(outcome: SpawnResult, contract: PayloadContract) -> str:
    """How one friend's round ended, in a few words, for the progress line.

    Deliberately not the failure text: `_stderr_tail` already puts that in
    the report, and a diagnostic wrapped across a terminal is exactly the
    noise that makes a progress stream unreadable. This says which of the
    handful of shapes happened, plus the one number a reader is waiting for
    -- how many claims or verdicts came back -- because a friend that
    answered with nothing and a friend that answered well are otherwise
    reported identically.
    """
    if outcome.timed_out:
        return "timed out"
    if not outcome.result.succeeded or outcome.result.payload is None:
        return f"failed: {outcome.failure_reason or 'no usable answer'}"
    items = outcome.result.payload.get(contract.container_key)
    count = len(items) if isinstance(items, list) else 0
    # `contract.name` is already plural ("claims", "verdicts").
    noun = contract.name[:-1] if count == 1 and contract.name.endswith("s") else contract.name
    suffix = " (truncated)" if outcome.output_truncated else ""
    return f"answered with {count} {noun}{suffix}"


def _round_summary(results: list[RoundResult], contract: PayloadContract) -> str:
    """The one line a reader wants after a round: how many friends answered,
    and how much they produced between them.

    Counts answers rather than successes-minus-failures because a round with
    one dead friend is a normal, reportable outcome here -- the run
    continues, and the report says so. Presenting it as "2 failed" would
    make a routine state read like an error.
    """
    if not results:
        return "no friends dispatched"
    answered = [r for r in results if r[2].result.succeeded and r[2].result.payload is not None]
    total = 0
    for _spec, _capability, outcome in answered:
        assert outcome.result.payload is not None
        items = outcome.result.payload.get(contract.container_key)
        total += len(items) if isinstance(items, list) else 0
    return f"{len(answered)}/{len(results)} friends answered, {total} {contract.name}"


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
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    reporter: Progress | None = None,
    kind: str = "critique",
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY,
) -> tuple[list[RoundResult], str | None]:
    """Run every friend in `specs` concurrently and return their outcomes.

    The second return value is an auth-abort message, or None. It is
    returned rather than raised: raising here, before the caller has a
    chance to call `persist_result` on anything, used to discard the whole
    round's output -- including from friends that succeeded -- the moment
    ANY friend in the round hit a deterministic auth failure. The caller
    persists and merges every result exactly as it would for a normal
    round, then decides what an auth abort means for it.

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
    report = reporter if reporter is not None else disabled()
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
                report.friend_dispatched(spec.name, spec.timeout)
                try:
                    result = _dispatch(
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
                        external_tool_policy,
                    )
                except AfError:
                    # A deliberate stop, not this friend's outcome. Cleared
                    # from the in-flight set so the heartbeat stops naming
                    # it, but not reported as a result -- it did not produce
                    # one, and the error itself is about to be printed.
                    report.friend_forgotten(spec.name)
                    raise
                except Exception as exc:
                    report.friend_finished(spec.name, f"failed: {exc.__class__.__name__}")
                    return spec, _UNKNOWN_CAPABILITY, _exception_outcome([], exc)
                report.friend_finished(spec.name, _outcome_word(result[2], contract))
                return result

            # Only specs that actually got an isolation directory are
            # dispatched -- _run_one would otherwise KeyError looking up
            # cwd_for for a spec whose setup never happened.
            dispatch_specs = [s for s in specs if s.name in cwd_for]
            if not dispatch_specs:
                return [], None
            # Bounded: see ceilings.DEFAULT_MAX_CONCURRENCY. `pool.map`
            # still returns results in `dispatch_specs` order, so a round's
            # aggregation is unchanged -- only how many friends are in
            # flight at once.
            workers = max(1, min(max_concurrency, len(dispatch_specs)))
            # Announced here rather than on entry: the friends named are the
            # ones that actually got an isolation directory, and a repeat-
            # disabled friend has already been partitioned by the caller. A header
            # listing friends that are not going to run would be the first
            # thing a reader had to learn to discount.
            report.round_started(round_no, kind, [s.name for s in dispatch_specs])
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            on_pool(pool)
            try:
                results = list(pool.map(_run_one, dispatch_specs))
                pool.shutdown(wait=True)
            except BaseException:
                # A deliberate stop from ONE friend must not wait out every
                # other friend's full timeout. `pool.map` raises as soon as
                # a worker does, but `ThreadPoolExecutor.__exit__` then
                # calls `shutdown(wait=True)` and joins the workers still
                # running -- so a flag validation error raised in the first
                # second surfaced only after the remaining friends had each
                # spent up to `--timeout`. With eight friends on the default
                # 900s that is fifteen minutes of a CLI that is already
                # certain to fail.
                #
                # `abort_event` is what actually shortens it: the friends
                # still running poll it and stop, the same way they do for
                # Ctrl-C. `cancel_futures` drops the ones that never
                # started, and `wait=False` means this does not block on
                # either. Correct precisely because an AfError ends the run
                # -- it reaches cli.py's handler and exits.
                #
                # BaseException, not Exception: KeyboardInterrupt arrives
                # here too, and it is the case that most needs the pool not
                # to block.
                abort_event.set()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            finally:
                on_pool(None)
                # In a `finally` because the heartbeat thread must stop even
                # when the round raises. A background thread left naming
                # friends that are no longer running would interleave with
                # the error being reported.
                report.round_finished(round_no, _round_summary(results, contract))
        finally:
            if not keep:
                for spec in specs:
                    if spec.scope == "repo" and spec.name in cwd_for:
                        assert repo_root is not None
                        isolation.remove_worktree(repo_root, cwd_for[spec.name])
            # doc_scope_dir entries need no explicit cleanup: they all live
            # under iso_root, which the TemporaryDirectory context manager
            # removes on exit independent of whether dispatch raised.

    auth_abort: str | None = None
    if tracker is not None:
        for spec, _capability, outcome in results:
            tracker.record(spec.name, outcome)
            # §7.2: an auth failure is deterministic, so every remaining
            # round and iteration would fail identically -- the caller
            # should stop scheduling more of them. Every result in this
            # round is still recorded and returned, though: this loop used
            # to `raise` on the first AUTH hit, which both discarded every
            # OTHER friend's result in this round and skipped `tracker
            # .record` for every spec after it in iteration order. Only the
            # first auth message is kept -- one is enough to tell the
            # operator what to fix. Raised only on a DECLARED marker -- an
            # unrecognised failure is never guessed into an abort, because
            # a false auth classification ends the whole run.
            if auth_abort is None and classify(outcome, registry.get(spec.cli)) == AUTH:
                auth_abort = auth_abort_message(spec.name, registry.get(spec.cli))
    return results, auth_abort


def persist_result(
    store: RunStore,
    round_no: int,
    spec: FriendSpec,
    capability: Capability,
    outcome: SpawnResult,
    transport: str,
    external_tool_policy: ExternalToolPolicy,
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
        f"output_truncated={outcome.output_truncated}\n"
        f"transport={transport}\nos_confined={outcome.os_confined}\n"
        f"external_tool_policy={external_tool_policy.value}\n"
        f"external_tools={capability.external_tools}\n"
        f"external_tool_sources={list(capability.external_tool_sources)}\n"
        f"deny_external_tools_argv={list(capability.deny_external_tools_argv)}\n",
        encoding="utf-8",
    )
    err_path = store.friend_err_path(round_no, spec.name)
    err_path.write_text(outcome.stderr, encoding="utf-8")

    diagnostics = _stderr_tail(outcome.stderr) if outcome.stderr.strip() else ""
    diagnostics_path = f"round-{round_no}/{spec.name}.err"
    status = "ok" if outcome.failure_reason is None else f"failed: {outcome.failure_reason}"
    if outcome.failure_reason is None and diagnostics:
        status += f" (diagnostics: {diagnostics}; full text in {diagnostics_path})"
    elif outcome.failure_reason is not None and diagnostics:
        status += f" (stderr: {diagnostics}; full text in {diagnostics_path})"
    if outcome.orphans_suspected:
        # A leaked descendant must not look identical to a clean run --
        # surfaced in the same status column readers already check for
        # "failed", rather than a silent field only run.json carries.
        status += " [orphans suspected]"
    return {
        "name": spec.name,
        "model": spec.model,
        "effort": spec.effort,
        "transport": transport,
        "write_protected": capability.readonly,
        "declared_scope": spec.scope,
        "os_confined": outcome.os_confined,
        "external_tools": capability.external_tools,
        "external_tool_policy": external_tool_policy.value,
        "external_tool_sources": list(capability.external_tool_sources),
        "deny_external_tools_argv": list(capability.deny_external_tools_argv),
        # Compatibility keys for consumers of the pre-0.2 run.json shape.
        "readonly": capability.readonly,
        "scope": spec.scope,
        "round": round_no,
        "status": status,
        "diagnostics": diagnostics,
        "diagnostics_path": diagnostics_path,
    }

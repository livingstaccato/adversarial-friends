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
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from typing import Any

from . import isolation
from .adapters import Adapter, Capability, FriendSpec, capability_from_authority
from .authority import DENY_ALL, AuthorityPolicy, ExternalToolPolicy, enforce
from .ceilings import DEFAULT_MAX_CONCURRENCY
from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .dispatch import (
    _UNKNOWN_CAPABILITY,
    _dispatch,
    _exception_outcome,
    _stderr_tail,
    failure_summary,
)
from .errors import AfError, UsageError
from .failures import AUTH, RepeatTracker, auth_abort_message, classify
from .progress import Progress, disabled
from .runstore import RunStore
from .spawn import SpawnResult
from .workspaceassets import (
    WorkspaceAssetAudit,
    WorkspaceAssetStagingError,
    stage_workspace_assets,
)

RoundResult = tuple[FriendSpec, Capability, SpawnResult, ExternalToolPolicy | None]


@dataclass(frozen=True)
class DispatchRoundOutcome:
    """Auditable results plus a stop that occurred after dispatch began."""

    results: list[RoundResult]
    auth_abort: str | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _DispatchAttempt:
    result: RoundResult
    error: BaseException | None = None


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
    store.write_sensitive(
        meta_path,
        f"status=skipped\nreason={skipped.reason}\n",
    )
    return {
        "name": skipped.spec.name,
        "independent": skipped.spec.independent,
        "host_self_review": skipped.spec.host_self_review,
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


def prune_undispatched_prompts(
    specs: Sequence[FriendSpec],
    prompt_for: dict[str, Path],
    results: Sequence[RoundResult],
    store: RunStore,
) -> None:
    """Keep prompt artifacts only for dispatch attempts that returned an auditable row."""
    dispatched = {spec.name for spec, _capability, _outcome, _policy in results}
    for spec in specs:
        if spec.name not in dispatched:
            store.unlink_owned(prompt_for[spec.name], missing_ok=True)


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
    for _spec, _capability, outcome, _policy in answered:
        assert outcome.result.payload is not None
        items = outcome.result.payload.get(contract.container_key)
        total += len(items) if isinstance(items, list) else 0
    return f"{len(answered)}/{len(results)} friends answered, {total} {contract.name}"


@contextlib.contextmanager
def _isolation_root(store: RunStore, round_no: int, keep: bool) -> Iterator[Path]:
    if keep:
        root = store.run_dir / "isolation" / f"round-{round_no}"
        from .secureio import secure_mkdir

        secure_mkdir(root, parents=True, exist_ok=True, root=store.root)
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
    authority_policy: AuthorityPolicy = DENY_ALL,
) -> DispatchRoundOutcome:
    """Run every friend in `specs` concurrently and return their outcomes.

    An auth-abort message or mid-dispatch error is returned rather than
    raised: raising here, before the caller has a
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
    called again with None on the way out. Signal handlers set `abort_event`,
    which every transport polls so in-flight workers can drain promptly.

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

            def _run_one(spec: FriendSpec) -> _DispatchAttempt | None:
                # spawn.run_process already turns most process-launch failures
                # (missing binary, E2BIG, ENOEXEC, ...) into a SpawnResult
                # rather than raising. This is the second, broader layer:
                # ANYTHING else that goes wrong for one friend must produce
                # an auditable attempt instead of losing every other friend's
                # (possibly already-succeeded) result. A deliberate AfError
                # is also returned separately as a stop condition.
                # Futures are submitted together, but submission is not a
                # dispatch attempt. A stop from an earlier worker may reach
                # this queued future before it starts; returning no result
                # lets the caller prune the prompt as genuinely unused.
                if abort_event.is_set():
                    return None
                report.friend_dispatched(
                    spec.name,
                    spec.timeout,
                    provider=spec.cli,
                    lens=spec.lens,
                    round_no=round_no,
                )
                result: RoundResult
                try:
                    asset_audit: tuple[WorkspaceAssetAudit, ...] = ()
                    if spec.cli != "fake" and registry[spec.cli].workspace_assets:
                        adapter = registry[spec.cli]
                        provider_policy = authority_policy.for_provider(spec.cli)
                        authority = enforce(adapter, provider_policy)
                        capability = capability_from_authority(adapter, authority)
                        try:
                            asset_audit = stage_workspace_assets(
                                adapter.workspace_assets, cwd_for[spec.name]
                            )
                        except WorkspaceAssetStagingError as exc:
                            capability = replace(capability, workspace_assets=exc.audits)
                            outcome = replace(_exception_outcome([], exc), failure_reason=str(exc))
                            result = spec, capability, outcome, provider_policy
                            report.friend_finished(
                                spec.name, _outcome_word(outcome, contract), succeeded=False
                            )
                            return _DispatchAttempt(result)
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
                        authority_policy,
                    )
                    if asset_audit:
                        result = (
                            result[0],
                            replace(result[1], workspace_assets=asset_audit),
                            result[2],
                            result[3],
                        )
                except AfError as exc:
                    # The process may have been refused before spawn, but
                    # this worker crossed the reliable dispatch boundary and
                    # consumed its prompt. Because _dispatch raised instead
                    # of returning its adapter-local decision, the synthetic
                    # row carries None and audits the policy as unknown rather
                    # than deriving a potentially different value here.
                    # Return both that failed row and the deliberate stop so
                    # callers can persist partial evidence before terminalizing.
                    abort_event.set()
                    result = (
                        spec,
                        replace(_UNKNOWN_CAPABILITY, workspace_assets=asset_audit),
                        replace(_exception_outcome([], exc), failure_reason=str(exc)),
                        None,
                    )
                    report.friend_finished(
                        spec.name, _outcome_word(result[2], contract), succeeded=False
                    )
                    return _DispatchAttempt(result, exc)
                except Exception as exc:
                    report.friend_finished(
                        spec.name, f"failed: {exc.__class__.__name__}", succeeded=False
                    )
                    return _DispatchAttempt(
                        (
                            spec,
                            replace(_UNKNOWN_CAPABILITY, workspace_assets=asset_audit),
                            _exception_outcome([], exc),
                            None,
                        )
                    )
                except BaseException as exc:
                    # KeyboardInterrupt can be delivered while a worker is
                    # evaluating adapter/setup code. It has the same audit
                    # requirement as AfError: retain the attempted prompt and
                    # a friend row, stop peers, and surface the interruption.
                    abort_event.set()
                    result = (
                        spec,
                        replace(_UNKNOWN_CAPABILITY, workspace_assets=asset_audit),
                        _exception_outcome([], exc),
                        None,
                    )
                    report.friend_finished(
                        spec.name, _outcome_word(result[2], contract), succeeded=False
                    )
                    return _DispatchAttempt(result, exc)
                report.friend_finished(
                    spec.name,
                    _outcome_word(result[2], contract),
                    succeeded=(
                        result[2].failure_reason is None
                        and result[2].result.succeeded
                        and result[2].result.payload is not None
                    ),
                )
                return _DispatchAttempt(result)

            # Only specs that actually got an isolation directory are
            # dispatched -- _run_one would otherwise KeyError looking up
            # cwd_for for a spec whose setup never happened.
            dispatch_specs = [s for s in specs if s.name in cwd_for]
            if not dispatch_specs:
                return DispatchRoundOutcome([])
            # Bounded: see ceilings.DEFAULT_MAX_CONCURRENCY. Futures are read
            # in `dispatch_specs` order, so aggregation remains stable.
            workers = max(1, min(max_concurrency, len(dispatch_specs)))
            # Announced here rather than on entry: the friends named are the
            # ones that actually got an isolation directory, and a repeat-
            # disabled friend has already been partitioned by the caller. A header
            # listing friends that are not going to run would be the first
            # thing a reader had to learn to discount.
            report.round_started(round_no, kind, [s.name for s in dispatch_specs])
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            on_pool(pool)
            futures: list[concurrent.futures.Future[_DispatchAttempt | None]] = []
            round_error: BaseException | None = None
            try:
                # Append immediately after each successful submit. If the
                # next submit itself is interrupted, already-started futures
                # remain reachable for the recovery path below.
                for spec in dispatch_specs:
                    futures.append(pool.submit(_run_one, spec))
                for future in futures:
                    attempt = future.result()
                    if attempt is None:
                        continue
                    results.append(attempt.result)
                    if round_error is None and attempt.error is not None:
                        round_error = attempt.error
                pool.shutdown(wait=True)
            except BaseException as exc:
                # A main-thread interruption can arrive between future
                # results. Stop queued work and let abort-aware in-flight
                # transports drain before recovering their audit records.
                abort_event.set()
                round_error = round_error or exc
                pool.shutdown(wait=True, cancel_futures=True)
                # A main-thread interruption can occur after workers have
                # already completed. Recover every finished attempt once the
                # abort has drained in-flight transports; prompt existence is
                # never used as a guess for whether dispatch happened.
                recorded = {spec.name for spec, _capability, _outcome, _policy in results}
                for future in futures:
                    if future.cancelled() or not future.done():
                        continue
                    try:
                        attempt = future.result()
                    except BaseException:
                        continue
                    if attempt is not None and attempt.result[0].name not in recorded:
                        results.append(attempt.result)
                        recorded.add(attempt.result[0].name)
            finally:
                on_pool(None)
                # In a `finally` because the heartbeat thread must stop even
                # when the round raises. A background thread left naming
                # friends that are no longer running would interleave with
                # the error being reported.
                report.round_finished(
                    round_no,
                    _round_summary(results, contract),
                    status="interrupted" if round_error is not None else "completed",
                )
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
        for spec, _capability, outcome, _policy in results:
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
            if (
                auth_abort is None
                and spec.independent
                and classify(outcome, registry.get(spec.cli)) == AUTH
            ):
                auth_abort = auth_abort_message(spec.name, registry.get(spec.cli))
    return DispatchRoundOutcome(results, auth_abort, round_error)


def persist_result(
    store: RunStore,
    round_no: int,
    spec: FriendSpec,
    capability: Capability,
    outcome: SpawnResult,
    transport: str,
    external_tool_policy: ExternalToolPolicy | None,
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
    store.write_sensitive(raw_path, outcome.stdout)
    policy_value = external_tool_policy.value if external_tool_policy is not None else "unknown"
    meta_text = (
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
        f"external_tool_policy={policy_value}\n"
        f"external_tools={capability.external_tools}\n"
        f"external_tool_sources={list(capability.external_tool_sources)}\n"
        f"deny_external_tools_argv={list(capability.deny_external_tools_argv)}\n"
    )
    if capability.workspace_assets:
        meta_text += (
            f"workspace_assets={[asset.as_dict() for asset in capability.workspace_assets]}\n"
        )
    store.write_sensitive(meta_path, meta_text)
    err_path = store.friend_err_path(round_no, spec.name)
    store.write_sensitive(err_path, outcome.stderr)

    diagnostics = _stderr_tail(outcome.stderr) if outcome.stderr.strip() else ""
    diagnostics_path = f"round-{round_no}/{spec.name}.err"
    failure_reason = failure_summary(outcome.failure_reason) if outcome.failure_reason else None
    status = "ok" if failure_reason is None else f"failed: {failure_reason or 'unusable output'}"
    if outcome.failure_reason is None and diagnostics:
        status += f" (diagnostics: {diagnostics}; full text in {diagnostics_path})"
    elif outcome.failure_reason is not None and diagnostics:
        status += f" (stderr: {diagnostics}; full text in {diagnostics_path})"
    if outcome.orphans_suspected:
        # A leaked descendant must not look identical to a clean run --
        # surfaced in the same status column readers already check for
        # "failed", rather than a silent field only run.json carries.
        status += " [orphans suspected]"
    row = {
        "name": spec.name,
        "independent": spec.independent,
        "host_self_review": spec.host_self_review,
        "model": spec.model,
        "effort": spec.effort,
        "transport": transport,
        "write_protected": capability.readonly,
        "declared_scope": spec.scope,
        "os_confined": outcome.os_confined,
        "external_tools": capability.external_tools,
        "external_tool_policy": policy_value,
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
    if capability.workspace_assets:
        row["workspace_assets"] = [asset.as_dict() for asset in capability.workspace_assets]
    captures: dict[str, str | None] = {}
    capture_paths = {
        "prompt": store.friend_prompt_path(round_no, spec.name),
        "raw": raw_path,
        "meta": meta_path,
        "err": err_path,
    }
    for label, path in capture_paths.items():
        if store.owned_regular_exists(path):
            payload = store.read_owned_bytes(path, max_bytes=32 * 1024 * 1024)
            captures[label] = "sha256:" + hashlib.sha256(payload).hexdigest()
        else:
            captures[label] = None
    store.write_sensitive_atomic(
        store.friend_audit_path(round_no, spec.name),
        json.dumps(
            {"version": 1, "round": round_no, "name": spec.name, "row": row, "captures": captures},
            sort_keys=True,
        ),
    )
    return row


def recover_result_audit(store: RunStore, round_no: int, spec: FriendSpec) -> dict[str, Any]:
    """Authenticate a persisted result row before exposing a replayed verdict."""
    path = store.friend_audit_path(round_no, spec.name)
    if not store.owned_regular_exists(path):
        return {
            "name": spec.name,
            "independent": spec.independent,
            "host_self_review": spec.host_self_review,
            "model": spec.model,
            "effort": spec.effort,
            "transport": "legacy-unknown",
            "write_protected": False,
            "declared_scope": spec.scope,
            "os_confined": False,
            "external_tools": "legacy-unknown",
            "external_tool_policy": "legacy-unknown",
            "external_tool_sources": [],
            "deny_external_tools_argv": [],
            "readonly": False,
            "scope": spec.scope,
            "round": round_no,
            "status": "ok",
            "diagnostics": "",
            "diagnostics_path": f"round-{round_no}/{spec.name}.err",
        }
    from .commands.checkpoint import normalize_friend_rows
    from .jsonio import MAX_JSON_FILE_BYTES, decode_json_object

    payload = store.read_owned_bytes(path, max_bytes=MAX_JSON_FILE_BYTES)
    data = decode_json_object(payload, path=path, label="persisted friend audit")
    expected_keys = {"version", "round", "name", "row", "captures"}
    if data.get("version") == 2:
        expected_keys.add("judging")
    if set(data) != expected_keys:
        raise UsageError("persisted friend audit has an invalid shape")
    if data["version"] not in {1, 2} or data["round"] != round_no or data["name"] != spec.name:
        raise UsageError("persisted friend audit has the wrong identity")
    rows = normalize_friend_rows(
        [data["row"]],
        {spec.name},
        {spec.name: (spec.independent, spec.host_self_review)},
    )
    captures = data["captures"]
    capture_names = {"prompt", "raw", "meta", "err"}
    if data["version"] == 2:
        capture_names.add("parsed")
    if type(captures) is not dict or set(captures) != capture_names:
        raise UsageError("persisted friend audit has invalid capture bindings")
    paths = {
        "prompt": store.friend_prompt_path(round_no, spec.name),
        "raw": store.friend_paths(round_no, spec.name)[0],
        "meta": store.friend_paths(round_no, spec.name)[2],
        "err": store.friend_err_path(round_no, spec.name),
    }
    if data["version"] == 2:
        paths["parsed"] = store.friend_paths(round_no, spec.name)[1]
    for label, capture_path in paths.items():
        expected = captures[label]
        if expected is None:
            if label == "prompt":
                raise UsageError("persisted judging audit is not bound to its prompt")
            continue
        if type(expected) is not str or not expected.startswith("sha256:"):
            raise UsageError("persisted friend audit has an invalid digest")
        actual = store.read_owned_bytes(capture_path, max_bytes=32 * 1024 * 1024)
        if "sha256:" + hashlib.sha256(actual).hexdigest() != expected:
            raise UsageError(f"persisted friend audit {label} capture was modified")
    return rows[0]

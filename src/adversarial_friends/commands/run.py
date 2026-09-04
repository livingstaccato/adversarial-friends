"""`afriend run --mode report`: dispatch an artifact to every resolved
friend in parallel and merge their claims into one report.

Split out of cli.py.
"""

import argparse
import concurrent.futures
from datetime import UTC, datetime
from pathlib import Path
import shutil
import signal
import sys
import time
from typing import Any
import uuid

from ..adapters import independent_friend_keys
from ..ceilings import (
    Budget,
    derive_max_calls,
    warn_if_unreachable,
    within_deadline,
)
from ..claimschema import schema_path
from ..dispatch import failure_summary
from ..errors import AfError, UsageError
from ..failures import RepeatTracker
from ..ledger import Claim
from ..orchestrator import (
    NeedsOrchestrator,
    write_request,
)
from ..reviewstate import ReviewState
from ..rounds import partition_dispatchable
from ..runstore import RunStore, default_root
from ..snapshots import (
    SnapshotIdentity,
    history_from_meta,
    record_snapshot,
    resume_frozen_artifact,
    select_snapshot,
)
from ..themes import ThemeProposal
from ..verdicts import next_streak, round_is_dry
from ..verdictschema import schema_path as verdict_schema_path
from .checkpoint import any_friend_succeeded
from .critique import run_critique
from .crossexam import run_rounds
from .environment import (
    clock_offset,
    freeze_revision,
    reconcile_snapshot_scope,
    resolve_run_repo,
    snapshot_scope_downgrade_note,
)
from .haltstate import loop_position, write_halt
from .resume import resume_iteration
from .runmeta import JUDGING_MODES, _base_meta, finish_run, loop_is_done, validate_run_args
from .setup import prepare_run

EXPLICIT_REPOSITORY_SCOPE_AUDIT = (
    "repository scope selected explicitly; frozen artifact independently "
    "bound (not Git-blob-bound)."
)


def _read_artifact_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"artifact must be valid UTF-8: {path}") from exc
    except OSError as exc:
        raise UsageError(f"cannot read artifact {path}: {exc}") from exc


def _dispatch_error_detail(error: BaseException) -> str:
    """One bounded representation for fresh and resumed dispatch stops."""
    return f"{type(error).__name__}: {failure_summary(str(error))}"


def cmd_run(args: argparse.Namespace) -> int:
    args, artifact = validate_run_args(args)
    resume_dir = getattr(args, "_resume_dir", None)
    resume_meta = getattr(args, "_resume_meta", None) if resume_dir is not None else None
    if resume_dir is None:
        # Decode before RunStore creates anything. A malformed artifact is a
        # usage refusal, not a runtime-error run with an unexplained partial
        # directory that cannot be resumed.
        _read_artifact_text(artifact)
    # Deliberately NOT resolved here: resolving would follow a symlinked
    # artifact to its target's own name, so a review of `link_spec.md ->
    # real_spec.md` would report and store the artifact as "real_spec.md"
    # -- surprising given the user passed the link's name. `artifact` is
    # used as-is everywhere below (shutil.copy2 and doc_scope_dir both
    # follow symlinks transparently when reading its content);
    # _resolve_repo_root resolves its own local copy internally, so
    # nothing here needs an absolute/resolved path to work correctly.

    setup = prepare_run(args)
    registry = setup.registry
    fake_cmd = setup.fake_cmd
    saved_downgrades = list(getattr(args, "_resume_downgrades", []))
    seen_downgrades: set[str] = set()
    downgrades: list[str] = []
    for note in [*saved_downgrades, *setup.downgrades]:
        if note not in seen_downgrades:
            seen_downgrades.add(note)
            downgrades.append(note)
    extra_args = setup.extra_args
    resolved, specs = setup.resolved, setup.specs
    env_withheld = setup.env_withheld
    abort_event = setup.abort_event
    abort_signum = setup.abort_signum
    active_pool = setup.active_pool
    installed_handlers = setup.installed_handlers
    reporter = setup.reporter
    authority_policy = setup.authority_policy
    store: RunStore | None = None
    try:
        if resume_meta is not None:
            # The recorded repository is part of the immutable identity. A
            # resume must not silently follow a moved live artifact into a
            # different repository and bless a replacement snapshot there.
            repo_root = SnapshotIdentity.from_meta(resume_meta).repo_root
            explicit_repo = False
            saved_audit = resume_meta.get("repository_scope_audit")
            repository_scope_audit = saved_audit if isinstance(saved_audit, str) else None
        else:
            repo_root, explicit_repo = resolve_run_repo(artifact, args.repo)
            repository_scope_audit = EXPLICIT_REPOSITORY_SCOPE_AUDIT if explicit_repo else None
        offset = clock_offset(downgrades)

        def now() -> float:
            return time.monotonic() + offset

        # Raw, deliberately: the offset represents time that has ALREADY
        # passed, so it must not be added to the start as well or it would
        # cancel out and the ceiling would never be reached.
        run_started = time.monotonic()
        invocation_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if resume_meta is not None and isinstance(resume_meta.get("started_at"), str):
            invocation_started_at = str(resume_meta["started_at"])
        if resume_dir is not None:
            run_id = resume_dir.name
            store = RunStore(resume_dir.parent, run_id, resume=True)
            frozen = resume_frozen_artifact(resume_dir)
            # Resume selection below takes the hash from the validated saved
            # identity. This placeholder is never trusted or persisted.
            digest = ""
        else:
            run_id = f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
            store = RunStore(Path(args.out) if args.out else default_root(), run_id)
            frozen, digest = store.artifact_copy(artifact)
        # One writer per run directory (see RunStore.lock). Taken as early
        # as the two branches above allow -- both must construct the
        # RunStore first, and the fresh-run branch also copies the artifact
        # in, which `run_dir.mkdir(parents=True)` already makes exclusive
        # (runstore.py). A losing resumer raises RunLocked before it uses
        # anything it read. The comment here used to claim the lock was held
        # before ANYTHING was read or written, which is not true of either
        # branch -- and a maintainer trusting it would add a ledger read or
        # a run.json write into that window against a directory another
        # resumer may be mid-write on.
        store.lock()
        reporter.event_writer = store.events_writer()
        snapshot = select_snapshot(
            repo_root,
            frozen,
            digest,
            resume_meta,
            source_artifact=artifact if resume_meta is None and not explicit_repo else None,
        )
        if (
            resume_meta is None
            and not explicit_repo
            and snapshot.repo_root is not None
            and not snapshot.artifact_bound_to_snapshot
        ):
            # An artifact reached through a link whose final target lies
            # outside its invocation repository has no Git blob to bind.
            # Preserve automatic doc-scope selection; only an explicit
            # `--repo` can intentionally create an unbound repo snapshot.
            snapshot = select_snapshot(None, frozen, digest, None)
        digest = snapshot.artifact_hash
        snapshot_history = (
            history_from_meta(resume_meta, snapshot) if resume_meta is not None else [snapshot]
        )
        if resume_meta is not None:
            # Legacy identities have no tree. Verification above derives it;
            # keep that migration in memory until the normal halt/completion
            # metadata write. A later read-only resume validation (ledger,
            # response, roster, grants) may still refuse the run, and no
            # refusal may rewrite the saved state it was asked to inspect.
            migrated_meta = dict(resume_meta)
            record_snapshot(migrated_meta, snapshot, snapshot_history)
            if migrated_meta != resume_meta:
                resume_meta = migrated_meta
                args._resume_meta = migrated_meta
        review = ReviewState.replay(store.ledger.records())
        review.copy_transition_warnings(downgrades)
        schema_file = schema_path(store.run_dir)
        artifact_text = _read_artifact_text(frozen)

        warning_seen = False

        def _warn_doc_scope() -> None:
            nonlocal warning_seen
            if warning_seen:
                return
            note = snapshot_scope_downgrade_note(artifact.name)
            if note in downgrades:
                print(
                    "afriend: warning: doc scope only -- no repository was detected for "
                    f"the artifact '{artifact.name}'. Friends can only read the artifact "
                    "text, not repository code. Place the artifact file inside the "
                    "repository you want reviewed to get full scope.",
                    file=sys.stderr,
                    flush=True,
                )
                warning_seen = True

        repo_root, specs = reconcile_snapshot_scope(artifact, snapshot, specs, downgrades)
        reporter.run_started(
            args.mode,
            str(getattr(args, "profile", "legacy") or "legacy"),
            "repo" if any(spec.scope == "repo" for spec in specs) else "doc",
        )
        _warn_doc_scope()
        # The snapshot serves two independent purposes, and taking it only
        # for the first one was a bug: repo-scope friends are checked out
        # from it, AND `afriend resolve` compares a resolution's location
        # against it (§6.4). Those needs do not coincide -- an all-ollama
        # roster is entirely doc-scope, so no friend needed a snapshot, and
        # every later resolution came back `unverifiable` even for a file
        # sitting in the repository. Worse, that silently downgraded the one
        # check the runner can actually make: a `fixed` disposition naming an
        # unchanged location was accepted rather than refused.
        #
        # Fresh runs take it whenever a repository exists because `resolve`
        # accepts every mode. Resumes reuse the verified saved commit above:
        # process restart is not an input revision and must never mint one.
        snapshot_sha = snapshot.commit

        def run_meta() -> dict[str, Any]:
            # Built the same way whether the run finishes or halts: a halted
            # directory a resume cannot read is worse than no halt at all.
            meta = _base_meta(
                args,
                artifact,
                digest,
                friends_meta,
                downgrades,
                specs,
                snapshot,
                snapshot_history,
                authority_policy,
                preset=resolved.preset,
                roster_source=resolved.source,
                env_withheld=env_withheld,
                started_at=invocation_started_at,
                theme_proposals=theme_proposals,
                produced_new_themes=produced_new_themes,
                prior_external_tool_policy=(resume_meta or {}).get("external_tool_policy"),
                detected_host=resolved.detected_host,
                effective_include_self=resolved.effective_include_self,
            )
            if repository_scope_audit is not None:
                meta["repository_scope_audit"] = repository_scope_audit
            return meta

        def _track_pool(pool: concurrent.futures.ThreadPoolExecutor | None) -> None:
            active_pool[0] = pool

        # §7.4's ceilings, shared across every iteration of a `loop`: the
        # budget is what a run may spend in total, not per iteration.
        max_iterations = args.max_loop_iterations if args.mode == "loop" else 1
        budget = Budget(
            max_calls=(
                args.max_calls
                if args.max_calls is not None
                else derive_max_calls(len(specs), args.max_rounds, max_iterations)
            ),
            max_rounds=args.max_rounds,
            max_wall_clock_s=args.max_wall_clock,
            calls=int(getattr(args, "_resume_spent_calls", 0)),
            started=run_started,
            prior_elapsed_s=float(getattr(args, "_resume_active_elapsed_s", 0.0)),
        )

        # The same `max_iterations` the derived default uses, so the
        # warning and the default cannot disagree about what a run costs.
        unreachable = warn_if_unreachable(
            len(specs), args.max_rounds, budget.max_calls, max_iterations
        )
        if unreachable:
            downgrades.append(unreachable)

        # One tracker for the whole run: a friend that failed identically in
        # iteration 1 must stay disabled in iteration 2, or a loop would
        # rediscover the same broken friend five times.
        #
        # Restored on `--resume`, not built fresh: a RepeatTracker lives
        # only in the process that built it, so a friend disabled in an
        # earlier iteration was silently un-disabled the moment that
        # process exited for its orchestrator halt.
        tracker = (
            RepeatTracker.restore(getattr(args, "_resume_meta", {}).get("repeat_tracker") or {})
            if resume_dir is not None
            else RepeatTracker()
        )

        all_claims: list[Claim] = []
        friends_meta: list[dict[str, Any]] = []
        counter = 0
        successful_friend_ids = list(getattr(args, "_resume_successful_friend_ids", []))
        theme_proposals: list[ThemeProposal] = list(getattr(args, "_resume_theme_proposals", []))
        produced_new_themes = bool(getattr(args, "_resume_produced_new_themes", False))
        any_success = bool(getattr(args, "_resume_any_success", False))
        # None: no fresh critique round yet -- decide_exit's
        # --require-friends check fails open on None rather than guess.
        succeeded_friends: int | None = (
            len(successful_friend_ids) if resume_dir is not None else None
        )
        # Set once any round hits a deterministic auth failure; only stops
        # further scheduling -- the round that found it is already persisted.
        auth_abort: str | None = None
        dispatch_error: str | None = None
        cross = None
        # What the next loop iteration inherits: states, verdicts, notes and
        # discard signatures. None means "judge everything fresh" -- the
        # first iteration, and any iteration whose artifact changed.
        carry_over = None
        last_digest: str | None = None
        streak = 0
        iterations_run = int(getattr(args, "_resume_iterations_run", 0))
        # Carried into write_halt so a resumed iteration can compute the
        # streak from what actually happened. The defaults describe an
        # EXTRACTION halt, where run_critique raises before returning
        # anything to read -- a round whose output could not be parsed is
        # not evidence of convergence, so "failed, not dry" is the honest
        # reading rather than a placeholder.
        halted_dry, halted_failed = False, True
        # Where a resumed loop re-enters, and what it inherits.
        first_iteration, streak, carry_over = loop_position(args, review, resume_dir is not None)
        announced_skips = {
            str(row["name"])
            for row in (resume_meta or {}).get("friends", [])
            if isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and str(row.get("status", "")).startswith("skipped: ")
        }
        if carry_over is not None:
            announced_skips.update(carry_over.dropped)
        # The highest round number the run reached, across every loop
        # iteration. Not the last iteration's own count: once a loop stops
        # re-judging what an earlier iteration already settled, its final
        # iteration can run no judging round at all, and reporting that
        # iteration's count said "Rounds run: 1" for a run that had just
        # spent eight.
        rounds_reached = int(getattr(args, "_resume_rounds_run", 0))
        loop_converged = False
        loop_exhausted = False

        # Any halt for the orchestrator must leave a resumable run behind.
        # A resumed run rebuilds its whole configuration from run.json, so
        # raising before writing one produces a directory that can never be
        # continued -- which is how the extraction halt first shipped, and
        # why this is caught here rather than at each raise site.
        try:
            for iteration in range(first_iteration, max_iterations + 1):
                if abort_event.is_set():
                    break
                # Each iteration owns a distinct block of round numbers, so a
                # loop's rounds never collide in the run directory or the ledger:
                # iteration 1 critiques in round 1 and judges in 2..max_rounds,
                # iteration 2 critiques in round max_rounds+1, and so on.
                base_round = (iteration - 1) * args.max_rounds + 1
                dispatchable, _skipped = partition_dispatchable(specs, tracker)
                resuming_iteration = resume_dir is not None and iteration == first_iteration
                if not resuming_iteration and budget.would_exceed_calls(len(dispatchable)):
                    budget.exhaust(
                        f"--max-calls={budget.max_calls} reached before iteration "
                        f"{iteration}'s critique round"
                    )
                    break
                if not resuming_iteration and budget.out_of_time(now()):
                    budget.exhaust(f"--max-wall-clock reached before iteration {iteration}")
                    break
                # §7.4: a friend may not outlive the ceiling it was
                # dispatched under. Without this the ceiling only bounded
                # the gaps between rounds, and a friend started a second
                # before it expired ran its own full timeout past it.
                #
                # The SAME helper the judging round uses. This was an inline
                # `min()` doing neither of that helper's two corrections, so
                # the ceiling meant one thing for a judging round and
                # another for the critique round immediately before it: a
                # critique friend dispatched with 20s left got a real kill
                # deadline of 80s, and with 0.6s left got a timeout of 0 --
                # which agy turns into `--print-timeout 0s` and dies
                # instantly, having spent a call and marked the run
                # incomplete. Raised from two lenses independently, which is
                # what a fix applied to one of two paths looks like from
                # outside.
                round_specs = (
                    specs
                    if resuming_iteration
                    else within_deadline(specs, budget.seconds_left(now()))
                )
                if not round_specs:
                    # Same shape crossexam uses when the helper returns
                    # nothing: say so and stop, rather than dispatching
                    # friends that cannot honestly run.
                    budget.exhaust(
                        f"--max-wall-clock leaves no usable time for iteration {iteration}"
                    )
                    break

                revision = freeze_revision(
                    store,
                    artifact,
                    frozen,
                    digest,
                    resume_dir is not None,
                    last_digest,
                    snapshot,
                    iteration,
                    artifact_bound_to_snapshot=not explicit_repo,
                )
                frozen, digest = revision.frozen, revision.digest
                artifact_text = revision.text
                if revision.identity != snapshot:
                    snapshot = revision.identity
                    snapshot_history.append(snapshot)
                repo_root, specs = reconcile_snapshot_scope(artifact, snapshot, specs, downgrades)
                _, round_specs = reconcile_snapshot_scope(
                    artifact, snapshot, round_specs, downgrades
                )
                _warn_doc_scope()
                snapshot_sha = snapshot.commit
                if revision.downgrade is not None:
                    downgrades.append(revision.downgrade)
                    carry_over = None
                last_digest = revision.digest

                if resume_dir is not None and iteration == first_iteration:
                    step = resume_iteration(
                        args,
                        store,
                        review,
                        round_specs,
                        registry,
                        fake_cmd,
                        frozen,
                        artifact_text,
                        repo_root,
                        snapshot_sha,
                        abort_event,
                        budget,
                        base_round,
                        _track_pool,
                        streak,
                        prior=carry_over,
                        tracker=tracker,
                        keep=args.keep,
                        extra_args=extra_args,
                        pass_env=tuple(args.pass_env),
                        reporter=reporter,
                        # Same rule the non-resumed call below uses: only
                        # the run's actual last block may mark an unjudged
                        # amendment `incomplete` rather than leaving it for
                        # the next iteration.
                        final_block=(args.mode != "loop" or iteration == max_iterations),
                        authority_policy=authority_policy,
                        announced_skips=announced_skips,
                    )
                    resumed = step.resumed
                    all_claims = resumed.claims
                    friends_meta.extend(resumed.friends_meta)
                    downgrades.extend(resumed.downgrades)
                    cross = resumed.cross or carry_over
                    carry_over = cross
                    # From the ledger, not from len(all_claims): canonical
                    # reconstruction drops claims a merge retired, so
                    # counting the live set re-issues ids already spent.
                    counter = resumed.counter
                    any_success = any_success or bool(successful_friend_ids)
                    succeeded_friends = len(successful_friend_ids)
                    iterations_run = iteration
                    rounds_reached = max(rounds_reached, base_round)
                    streak = step.streak
                    if resumed.cross is not None and resumed.cross.dispatch_error is not None:
                        dispatch_error = _dispatch_error_detail(resumed.cross.dispatch_error)
                        break
                    if resumed.cross is not None and resumed.cross.auth_abort is not None:
                        auth_abort = resumed.cross.auth_abort
                        break
                    if budget.out_of_time(now()):
                        budget.exhaust(
                            f"--max-wall-clock reached after resuming iteration {iteration}"
                        )
                        break
                    if step.done or loop_is_done(
                        streak,
                        all_claims,
                        cross,
                        independent_friend_keys(partition_dispatchable(specs, tracker)[0]),
                    ):
                        loop_converged = True
                        break
                    resume_dir = None
                    continue

                critique, all_claims, counter = run_critique(
                    round_specs,
                    base_round,
                    all_claims,
                    counter,
                    artifact_text,
                    store,
                    review,
                    registry,
                    fake_cmd,
                    schema_file,
                    frozen,
                    repo_root,
                    snapshot_sha,
                    abort_event,
                    on_pool=_track_pool,
                    allow_unsandboxed=args.allow_unsandboxed_friend,
                    tracker=tracker,
                    keep=args.keep,
                    extra_args=extra_args,
                    pass_env=tuple(args.pass_env),
                    merge=args.merge,
                    run_id=run_id,
                    reporter=reporter,
                    authority_policy=authority_policy,
                    announced_skips=announced_skips,
                )
                budget.spend(critique.calls)
                iterations_run = iteration
                rounds_reached = max(rounds_reached, base_round)
                friends_meta.extend(critique.friends_meta)
                downgrades.extend(critique.downgrades)
                any_success = any_success or critique.any_success
                # The most recent fresh critique round's count, not a
                # running total: --require-friends asks "did the review
                # that just ran have enough friends", not "across every
                # iteration of a loop, how many ever succeeded".
                succeeded_friends = critique.succeeded_friends
                successful_friend_ids = list(critique.successful_friend_ids)
                theme_proposals.extend(critique.theme_proposals)
                produced_new_themes = critique.produced_new_themes
                halted_dry = round_is_dry(
                    not critique.produced_new_independent_themes,
                    critique.independent_any_success and not critique.independent_any_failed,
                )
                halted_failed = (
                    critique.independent_any_failed or not critique.independent_any_success
                )

                if critique.dispatch_error is not None:
                    dispatch_error = _dispatch_error_detail(critique.dispatch_error)
                    break

                if critique.auth_abort is not None:
                    # Deterministic (§7.2): stop rather than ask for
                    # orchestrator adjudication or judge with a broken roster.
                    auth_abort = critique.auth_abort
                    break

                if args.merge == "orchestrator" and all_claims:
                    # §4.2. Stop and ask for judgment the runner cannot make.
                    # Raised rather than returned so it takes the same path
                    # §14.2's extraction halt does -- one place writes the
                    # run.json a resume needs, so neither halt can ship a
                    # directory that cannot be continued.
                    request = write_request(
                        store.round_dir(base_round),
                        run_id,
                        base_round,
                        all_claims,
                        store=store,
                    )
                    raise NeedsOrchestrator(
                        f"waiting for merge adjudication. Fill in {request}, save "
                        "it as RESPONSE.json beside it, then re-run with --resume."
                    )

                if args.mode in JUDGING_MODES and all_claims:
                    # Only worth entering with claims in hand: with none there is
                    # nothing to judge, and a judging round would cost a full
                    # fan-out to decide nothing. A critique report is the honest
                    # result.
                    cross = run_rounds(
                        round_specs,
                        all_claims,
                        store,
                        review,
                        registry,
                        fake_cmd,
                        verdict_schema_path(store.run_dir),
                        frozen,
                        artifact_text,
                        repo_root,
                        snapshot_sha,
                        abort_event,
                        budget,
                        base_round + args.max_rounds - 1,
                        attributed=args.attributed,
                        on_pool=_track_pool,
                        first_round=base_round + 1,
                        allow_unsandboxed=args.allow_unsandboxed_friend,
                        tracker=tracker,
                        keep=args.keep,
                        extra_args=extra_args,
                        pass_env=tuple(args.pass_env),
                        prior=carry_over,
                        final_block=(args.mode != "loop" or iteration == max_iterations),
                        reporter=reporter,
                        authority_policy=authority_policy,
                        announced_skips=announced_skips,
                    )
                    all_claims = cross.claims
                    carry_over = cross
                    rounds_reached = max(rounds_reached, cross.rounds_run)
                    friends_meta.extend(cross.friends_meta)
                    downgrades.extend(cross.downgrades)
                    if cross.dispatch_error is not None:
                        dispatch_error = _dispatch_error_detail(cross.dispatch_error)
                        break
                    if cross.auth_abort is not None:
                        auth_abort = cross.auth_abort
                        break

                if args.mode != "loop":
                    break

                # §7.3's streak arithmetic. A failed round resets rather than
                # counting: a round that did not complete is not evidence of
                # convergence.
                dry = halted_dry
                streak = next_streak(streak, failed=halted_failed, dry=dry)
                active_specs, _policy_skips = partition_dispatchable(specs, tracker)
                if loop_is_done(streak, all_claims, cross, independent_friend_keys(active_specs)):
                    loop_converged = True
                    break
                if budget.exhausted_by:
                    break
            else:
                # A `for`-range ending is an observed fact distinct from
                # reaching the same iteration count through resume. Only
                # this path means the configured loop iteration ceiling was
                # naturally exhausted.
                loop_exhausted = (
                    args.mode == "loop"
                    and not abort_event.is_set()
                    and auth_abort is None
                    and budget.exhausted_by is None
                )

        except NeedsOrchestrator as halt:
            extraction_halt = halt.calls > 0
            if extraction_halt:
                budget.spend(halt.calls)
                rounds_reached = max(rounds_reached, base_round)
                friends_meta.extend(halt.friends_meta)
                downgrades.extend(halt.downgrades)
                successful_friend_ids = list(halt.successful_friend_ids)
                theme_proposals.extend(halt.theme_proposals)
                produced_new_themes = halt.produced_new_themes
                any_success = any_success or any_friend_succeeded(halt.friends_meta)
                succeeded_friends = len(successful_friend_ids)
            write_halt(
                args,
                store,
                run_meta(),
                review,
                iteration,
                streak,
                carry_over,
                round_dry=halted_dry,
                round_failed=halted_failed,
                budget=budget,
                tracker=tracker,
                rounds_run=rounds_reached,
                active_elapsed_s=budget.elapsed(now()),
                successful_friend_ids=successful_friend_ids,
                iteration_completed=not extraction_halt,
            )
            reporter.run_finished("halted", "resume", duration_s=budget.elapsed(now()))
            raise
        except Exception as exc:
            if isinstance(exc, AfError):
                raise
            try:
                finish_run(
                    args,
                    store,
                    run_meta(),
                    cross,
                    abort_signum["value"],
                    any_success,
                    succeeded_friends,
                    successful_friend_ids,
                    iterations_run,
                    streak,
                    downgrades,
                    budget,
                    rounds_reached,
                    tracker,
                    loop_converged,
                    loop_exhausted,
                    budget.elapsed(now()),
                    auth_abort=auth_abort,
                    runtime_error=f"{type(exc).__name__}: {exc}",
                    reporter=reporter,
                )
            except Exception as persistence_error:
                exc.add_note(f"terminal persistence also failed: {persistence_error}")
            raise

        return finish_run(
            args,
            store,
            run_meta(),
            cross,
            abort_signum["value"],
            any_success,
            succeeded_friends,
            successful_friend_ids,
            iterations_run,
            streak,
            downgrades,
            budget,
            rounds_reached,
            tracker,
            loop_converged,
            loop_exhausted,
            budget.elapsed(now()),
            auth_abort=auth_abort,
            runtime_error=dispatch_error,
            reporter=reporter,
        )
    except Exception:
        # Initialization failures before a durable run.json exists must not
        # leave a fresh directory that looks resumable but explains nothing.
        # A resumed directory predates this process and is never removed.
        if (
            store is not None
            and resume_dir is None
            and not store.owned_regular_exists(store.run_dir / "run.json")
        ):
            shutil.rmtree(store.run_dir, ignore_errors=True)
        raise
    finally:
        # Stops the heartbeat thread. In the same `finally` as the signal
        # handlers because both are process-level state this command
        # installed, and a Ctrl-C that skipped this would leave a thread
        # narrating friends that are no longer running.
        reporter.close()
        # Handlers are restored unconditionally: a library-ish function
        # should not leave process-wide signal disposition changed.
        for restored_sig, previous in installed_handlers.items():
            signal.signal(restored_sig, previous)

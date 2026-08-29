"""`afriend run --mode report`: dispatch an artifact to every resolved
friend in parallel and merge their claims into one report.

Split out of cli.py.
"""

import argparse
import concurrent.futures
import dataclasses
from pathlib import Path
import signal
import time
from typing import Any
import uuid

from .. import isolation
from ..adapters import friend_key
from ..ceilings import (
    Budget,
    derive_max_calls,
    warn_if_unreachable,
    within_deadline,
)
from ..claimschema import schema_path
from ..failures import RepeatTracker
from ..ledger import Claim
from ..merge import ledger_aliases
from ..orchestrator import (
    NeedsOrchestrator,
    write_request,
)
from ..runstore import RunStore, default_root
from ..verdicts import next_streak, round_is_dry
from ..verdictschema import schema_path as verdict_schema_path
from .critique import run_critique
from .crossexam import run_rounds
from .environment import _resolve_repo_root, clock_offset, freeze_revision
from .haltstate import loop_position, write_halt
from .resume import resume_iteration
from .runmeta import JUDGING_MODES, _base_meta, finish_run, loop_is_done, validate_run_args
from .setup import prepare_run


def cmd_run(args: argparse.Namespace) -> int:
    args, artifact = validate_run_args(args)
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
    downgrades = setup.downgrades
    extra_args = setup.extra_args
    resolved, specs = setup.resolved, setup.specs
    env_withheld = setup.env_withheld
    abort_event = setup.abort_event
    abort_signum = setup.abort_signum
    active_pool = setup.active_pool
    installed_handlers = setup.installed_handlers
    reporter = setup.reporter
    try:
        repo_root = _resolve_repo_root(artifact)
        if repo_root is None:
            downgrades.append(
                f"{artifact.name} is not inside a git repository; every friend was "
                "downgraded to doc scope (no repository to snapshot or read)."
            )
            specs = [dataclasses.replace(s, scope="doc") for s in specs]

        offset = clock_offset(downgrades)

        def now() -> float:
            return time.monotonic() + offset

        # Raw, deliberately: the offset represents time that has ALREADY
        # passed, so it must not be added to the start as well or it would
        # cancel out and the ceiling would never be reached.
        run_started = time.monotonic()
        resume_dir = getattr(args, "_resume_dir", None)
        if resume_dir is not None:
            run_id = resume_dir.name
            store = RunStore(resume_dir.parent, run_id, resume=True)
            frozen = next(iter((resume_dir / "artifact").iterdir()))
            digest = getattr(args, "_resume_meta", {}).get("artifact_hash", "")
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
        schema_file = schema_path(store.run_dir)
        artifact_text = frozen.read_text(encoding="utf-8")

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
        # Taken for `gate` regardless of scope, since that is the only mode
        # whose resolutions need it. It is a commit object built from the
        # index, with no worktree and no checkout, so the cost is small and
        # confined to the mode that benefits.
        snapshot_sha = None
        # Taken whenever there is a repository, not only for repo-scope
        # friends or `gate`. `afriend resolve` accepts any run directory and
        # never reads the mode, so a doc-scope crossexam with no snapshot
        # made every location `unverifiable` -- and an `unverifiable` check
        # does not refuse a `fixed` disposition, so the ledger recorded a
        # verified-looking fix for a file nobody had touched. The cost is a
        # commit object built from the index: no worktree, no checkout.
        if repo_root is not None:
            snapshot_sha = isolation.snapshot_commit(repo_root)

        def run_meta() -> dict[str, Any]:
            # Built the same way whether the run finishes or halts: a halted
            # directory a resume cannot read is worse than no halt at all.
            return _base_meta(
                args,
                artifact,
                digest,
                friends_meta,
                downgrades,
                specs,
                repo_root,
                snapshot_sha,
                preset=resolved.preset,
                roster_source=resolved.source,
                env_withheld=env_withheld,
            )

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
            started=run_started,
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
        any_success = False
        # None: no fresh critique round yet -- decide_exit's
        # --require-friends check fails open on None rather than guess.
        succeeded_friends: int | None = None
        cross = None
        # What the next loop iteration inherits: states, verdicts, notes and
        # discard signatures. None means "judge everything fresh" -- the
        # first iteration, and any iteration whose artifact changed.
        carry_over = None
        last_digest: str | None = None
        streak = 0
        iterations_run = 0
        # Carried into write_halt so a resumed iteration can compute the
        # streak from what actually happened. The defaults describe an
        # EXTRACTION halt, where run_critique raises before returning
        # anything to read -- a round whose output could not be parsed is
        # not evidence of convergence, so "failed, not dry" is the honest
        # reading rather than a placeholder.
        halted_dry, halted_failed = False, True
        # Where a resumed loop re-enters, and what it inherits.
        first_iteration, streak, carry_over = loop_position(args, store, resume_dir is not None)
        # The highest round number the run reached, across every loop
        # iteration. Not the last iteration's own count: once a loop stops
        # re-judging what an earlier iteration already settled, its final
        # iteration can run no judging round at all, and reporting that
        # iteration's count said "Rounds run: 1" for a run that had just
        # spent eight.
        rounds_reached = 0

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
                if budget.would_exceed_calls(len(specs)):
                    budget.exhaust(
                        f"--max-calls={budget.max_calls} reached before iteration "
                        f"{iteration}'s critique round"
                    )
                    break
                if budget.out_of_time(now()):
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
                round_specs = within_deadline(specs, budget.seconds_left(now()))
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
                    repo_root,
                    snapshot_sha,
                    iteration,
                )
                frozen, digest = revision.frozen, revision.digest
                artifact_text, snapshot_sha = revision.text, revision.snapshot_sha
                if revision.downgrade is not None:
                    downgrades.append(revision.downgrade)
                    carry_over = None
                last_digest = revision.digest

                if resume_dir is not None and iteration == first_iteration:
                    step = resume_iteration(
                        args,
                        store,
                        round_specs,
                        registry,
                        fake_cmd,
                        artifact,
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
                    any_success = True
                    iterations_run = iteration
                    rounds_reached = max(rounds_reached, base_round)
                    streak = step.streak
                    if step.done or loop_is_done(
                        streak, all_claims, cross, [friend_key(s) for s in specs]
                    ):
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
                    registry,
                    fake_cmd,
                    schema_file,
                    artifact,
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

                if args.merge == "orchestrator" and all_claims:
                    # §4.2. Stop and ask for judgment the runner cannot make.
                    # Raised rather than returned so it takes the same path
                    # §14.2's extraction halt does -- one place writes the
                    # run.json a resume needs, so neither halt can ship a
                    # directory that cannot be continued.
                    request = write_request(
                        store.round_dir(base_round), run_id, base_round, all_claims
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
                        registry,
                        fake_cmd,
                        verdict_schema_path(store.run_dir),
                        artifact,
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
                    )
                    all_claims = cross.claims
                    carry_over = cross
                    rounds_reached = max(rounds_reached, cross.rounds_run)
                    friends_meta.extend(cross.friends_meta)
                    downgrades.extend(cross.downgrades)

                if args.mode != "loop":
                    break

                # §7.3's streak arithmetic. A failed round resets rather than
                # counting: a round that did not complete is not evidence of
                # convergence.
                dry = round_is_dry(critique.produced_only_aliases, not critique.any_failed)
                streak = next_streak(streak, failed=critique.any_failed, dry=dry)
                halted_dry, halted_failed = dry, critique.any_failed
                if loop_is_done(streak, all_claims, cross, [friend_key(s) for s in specs]):
                    break
                if budget.exhausted_by:
                    break

        except NeedsOrchestrator:
            write_halt(
                args,
                store,
                run_meta(),
                all_claims,
                # From the ledger, not a process-local accumulator: see
                # merge.ledger_aliases. A halt exits the process, and the
                # next resume's accumulator restarts at `[]`.
                ledger_aliases(list(store.ledger.records())),
                iteration,
                streak,
                carry_over,
                round_dry=halted_dry,
                round_failed=halted_failed,
                budget=budget,
                tracker=tracker,
            )
            raise

        return finish_run(
            args,
            store,
            run_meta(),
            all_claims,
            cross,
            abort_signum["value"],
            any_success,
            succeeded_friends,
            iterations_run,
            streak,
            downgrades,
            budget,
            rounds_reached,
        )
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

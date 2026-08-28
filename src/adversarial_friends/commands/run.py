"""`afriend run --mode report`: dispatch an artifact to every resolved
friend in parallel and merge their claims into one report.

Split out of cli.py.
"""

import argparse
import concurrent.futures
import dataclasses
import json
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any
import uuid

from .. import isolation
from ..adapters import friend_key, load_adapters
from ..ceilings import BUDGET_EXHAUSTED, Budget, derive_max_calls, warn_if_unreachable
from ..claimschema import schema_path
from ..failures import RepeatTracker
from ..ledger import Alias, Claim
from ..orchestrator import (
    NeedsOrchestrator,
    write_request,
)
from ..paths import ADAPTER_DIR
from ..report import render
from ..resolutions import blocking_claims
from ..runstore import RunStore, default_root
from ..trust import parse_unsafe_extra_args
from ..verdicts import next_streak, round_is_dry
from ..verdictschema import schema_path as verdict_schema_path
from .confinement import confinement_downgrades
from .critique import run_critique
from .crossexam import run_rounds
from .environment import _resolve_repo_root, freeze_revision, install_abort_handlers
from .exits import decide_exit
from .friends import roster_for_run
from .resume import loop_position, resume_round_one, write_halt
from .runmeta import JUDGING_MODES, _base_meta, loop_is_done, validate_run_args


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

    registry = load_adapters(ADAPTER_DIR)
    # AF_FAKE_FRIEND keeps the end-to-end tests off real CLIs and, critically,
    # off any metered provider. `--friend fake:<mode>` runs
    # `$AF_FAKE_FRIEND <mode>` directly (see dispatch._dispatch); the mode
    # travels in the lens slot of the cli:lens flag syntax.
    fake_env = os.environ.get("AF_FAKE_FRIEND")
    fake_cmd = fake_env.split() if fake_env else None
    downgrades: list[str] = []
    # §13's escape hatch. Parsed early so a bad value fails before any
    # dispatch, and recorded as a downgrade because a run carrying
    # unvalidated flags has weaker guarantees than its friend table implies.
    extra_args = parse_unsafe_extra_args(args.unsafe_extra_args, args.i_accept_unsandboxed)
    if extra_args:
        downgrades.append(
            f"--unsafe-extra-args passed {extra_args} to every friend. These "
            "flags are not validated, so read-only is reported as False for "
            "every friend regardless of what its adapter emitted."
        )

    resolved, specs = roster_for_run(args, registry, fake_cmd, downgrades)
    # §12.2: every friend that will run without OS confinement is named in
    # the report, whether that is because the operator overrode the refusal
    # or because the CLI has no read-only mode and one was available. A
    # weakened guarantee has to be visible in the artifact a human reads,
    # not only in the code that decided it.
    env_withheld = confinement_downgrades(args, specs, registry, downgrades)

    # Signal handling: a cancelled or CI-killed run must not leave a
    # metered agent CLI process running unbounded, nor a stale
    # `git worktree` registration behind in the repo under review. Neither
    # SIGTERM nor (reliably, once the main thread is blocked deep inside a
    # C-level wait) SIGINT would otherwise give this function's own
    # `finally` blocks a chance to run at all: SIGTERM's default
    # disposition kills the process immediately, with no Python-level
    # unwinding whatsoever; SIGINT's default handler does raise
    # KeyboardInterrupt, but that exception, once it propagates out of the
    # blocked `pool.map()` call below, immediately re-blocks inside
    # `ThreadPoolExecutor.__exit__`'s own `shutdown(wait=True)`, which
    # waits for the same still-hung worker -- so cleanup never actually
    # runs within any reasonable time either way. Installing explicit
    # handlers for both signals, which only ever set `abort_event` and
    # shut the active pool down without waiting, is what makes the
    # `finally` blocks below reachable promptly. Handlers are restored
    # unconditionally in the outer `finally` -- a library-ish function
    # should not permanently hijack process-wide signal disposition.
    abort_event = threading.Event()
    abort_signum: dict[str, int | None] = {"value": None}
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None] = [None]
    installed_handlers = install_abort_handlers(abort_event, abort_signum, active_pool)
    if len(installed_handlers) < 2:
        downgrades.append(
            "signal-based abort handling is unavailable in this context "
            "(cmd_run was not called from the main thread); Ctrl-C/SIGTERM "
            "cannot cleanly abort this run -- isolation teardown on a kill "
            "signal is not guaranteed."
        )
    try:
        repo_root = _resolve_repo_root(artifact)
        if repo_root is None:
            downgrades.append(
                f"{artifact.name} is not inside a git repository; every friend was "
                "downgraded to doc scope (no repository to snapshot or read)."
            )
            specs = [dataclasses.replace(s, scope="doc") for s in specs]

        # Injectable so an end-to-end test can reach the wall-clock branch
        # without hanging a worker for two hours: AF_CLOCK_OFFSET_S is added
        # to every reading this run takes. Nothing else changes -- the
        # arithmetic under test is the same arithmetic -- and with the
        # variable unset `now()` is `time.monotonic` exactly.
        offset = float(os.environ.get("AF_CLOCK_OFFSET_S", "0") or 0)

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
        # One writer per run directory (see RunStore.lock). Taken before
        # anything is read from it or written to it.
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
        unreachable = warn_if_unreachable(len(specs), args.max_rounds, budget.max_calls)
        if unreachable:
            downgrades.append(unreachable)

        # One tracker for the whole run: a friend that failed identically in
        # iteration 1 must stay disabled in iteration 2, or a loop would
        # rediscover the same broken friend five times.
        tracker = RepeatTracker()

        all_claims: list[Claim] = []
        all_aliases: list[Alias] = []
        friends_meta: list[dict[str, Any]] = []
        counter = 0
        any_success = False
        cross = None
        # What the next loop iteration inherits: states, verdicts, notes and
        # discard signatures. None means "judge everything fresh" -- the
        # first iteration, and any iteration whose artifact changed.
        carry_over = None
        last_digest: str | None = None
        streak = 0
        iterations_run = 0
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
                round_specs = [
                    dataclasses.replace(s, timeout=min(s.timeout, int(budget.seconds_left(now()))))
                    for s in specs
                ]

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
                    resumed = resume_round_one(
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
                    )
                    all_claims = resumed.claims
                    all_aliases.extend(resumed.aliases)
                    friends_meta.extend(resumed.friends_meta)
                    downgrades.extend(resumed.downgrades)
                    cross = resumed.cross or carry_over
                    carry_over = cross
                    counter = len(all_claims)
                    any_success = True
                    iterations_run = iteration
                    rounds_reached = max(rounds_reached, base_round)
                    if args.mode != "loop":
                        break
                    # The resumed iteration is now complete. Fall through to
                    # the same streak arithmetic every other iteration runs,
                    # and let the loop decide whether another is due; the
                    # next one will halt for its own adjudication.
                    streak = next_streak(streak, failed=False, dry=round_is_dry(False, True))
                    if loop_is_done(streak, all_claims, cross, [friend_key(s) for s in specs]):
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
                )
                budget.spend(critique.calls)
                iterations_run = iteration
                rounds_reached = max(rounds_reached, base_round)
                friends_meta.extend(critique.friends_meta)
                downgrades.extend(critique.downgrades)
                all_aliases.extend(critique.aliases)
                any_success = any_success or critique.any_success

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
                all_aliases,
                iteration,
                streak,
                carry_over,
            )
            raise

        # §7.5's gate. Evaluated against the run's own (empty) resolution
        # set, so a fresh gate run always reports what needs attention rather
        # than passing: resolutions are recorded afterwards, by `afriend
        # resolve`, which re-evaluates this same rule.
        blocking: list[Claim] = []
        if args.mode == "gate":
            blocking = blocking_claims(all_claims, cross.states if cross else {}, [])

        meta: dict[str, Any] = run_meta()
        if args.mode == "loop":
            meta["iterations_run"] = iterations_run
            meta["dry_streak"] = streak
        if args.mode == "gate":
            meta["gate_blocked"] = bool(blocking)
            meta["gate_blocking_claims"] = [c.id for c in blocking]
        if budget.exhausted_by and not meta.get("ceiling_hit"):
            # Same spelling crossexam uses: the label names the ceiling, the
            # downgrade says which one and when.
            meta["ceiling_hit"] = BUDGET_EXHAUSTED
            reason = f"{BUDGET_EXHAUSTED}: {budget.exhausted_by}"
            if reason not in downgrades:
                downgrades.append(reason)
        if cross is not None:
            meta["rounds_run"] = max(rounds_reached, cross.rounds_run)
            meta["claim_states"] = cross.states
            meta["amendment_notes"] = cross.notes
            meta["ceiling_hit"] = cross.ceiling_hit or (
                BUDGET_EXHAUSTED if budget.exhausted_by else None
            )
            meta["incomplete"] = cross.incomplete
        store.write_run_json(meta)
        store.write_report(
            render(
                all_claims,
                all_aliases,
                meta,
                verdicts=cross.verdicts if cross else None,
                states=cross.states if cross else None,
            )
        )
        if args.json:
            # The path is still what a shell pipeline wants; --json is for a
            # caller that would otherwise have to read run.json itself.
            print(json.dumps(meta, indent=2, sort_keys=True))
        else:
            print(store.run_dir)

        return decide_exit(
            abort_signum["value"],
            any_success,
            args.mode,
            cross,
            blocking,
            ceiling_hit=BUDGET_EXHAUSTED if budget.exhausted_by else None,
        )
    finally:
        # A distinct loop variable name from the `for sig in (...)` loop
        # above: reusing `sig` here would bind it to a different type
        # (installed_handlers' int keys vs. that loop's Signals values)
        # in the same function scope.
        for restored_sig, previous in installed_handlers.items():
            signal.signal(restored_sig, previous)

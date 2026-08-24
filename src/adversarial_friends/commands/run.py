"""`afriend run --mode report`: dispatch an artifact to every resolved
friend in parallel and merge their claims into one report.

Split out of cli.py.
"""

import argparse
from collections.abc import Callable
import concurrent.futures
import contextlib
import dataclasses
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import FrameType
from typing import Any
import uuid

from .. import isolation, verdicts as vd
from ..adapters import load_adapters
from ..ceilings import Budget, derive_max_calls, warn_if_unreachable
from ..claimschema import schema_path
from ..cliargs import _specs_from_flags
from ..errors import CeilingError, NoFriendsError, UsageError
from ..ids import validate_friend_name
from ..ledger import Alias, Claim
from ..paths import ADAPTER_DIR
from ..prompt import available_lenses
from ..report import render
from ..resolutions import blocking_claims
from ..roster import resolve
from ..runstore import RunStore, default_root
from ..verdicts import loop_should_terminate, next_streak, round_is_dry
from ..verdictschema import schema_path as verdict_schema_path
from .critique import run_critique
from .crossexam import run_rounds

# Every mode that judges claims after critiquing them. `report` stops at the
# critique round; the rest all run cross-examination and differ only in what
# they do with its result.
JUDGING_MODES = frozenset({"crossexam", "gate", "loop"})
IMPLEMENTED_MODES = frozenset({"report", *JUDGING_MODES})

# The type signal.signal() both accepts and returns, per typeshed: a
# handler callable, a raw int (SIG_IGN/SIG_DFL's underlying value), or None.
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


def cmd_run(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact)
    if not artifact.is_file():
        raise UsageError(f"artifact not found: {artifact}")
    if args.mode not in IMPLEMENTED_MODES:
        raise UsageError(
            f"mode {args.mode!r} is not implemented yet; "
            f"available: {', '.join(sorted(IMPLEMENTED_MODES))}"
        )
    if args.max_rounds < 2 and args.mode in JUDGING_MODES:
        # Round 1 is the critique round; judging starts at round 2. A
        # crossexam capped at one round is a report with a misleading name.
        raise UsageError(
            f"--max-rounds={args.max_rounds} leaves no judging round for "
            f"--mode {args.mode} (round 1 is the critique round; judging "
            "starts at round 2). Use --mode report, or --max-rounds 2 or more."
        )
    if args.preset != "inherit":
        # --preset is accepted and printed in the report header, but nothing
        # reads it: no code path varies model/effort/timeout selection by
        # preset name. Rejecting explicitly (same pattern as the --mode
        # check just above) rather than silently accepting and doing
        # nothing -- a report that says "preset: thorough" while running
        # exactly like "inherit" would misrepresent what actually happened.
        raise UsageError(
            f"preset {args.preset!r} is not implemented yet; only 'inherit' is available"
        )
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

    specs = (
        _specs_from_flags(args.friend, args.timeout, registry, bool(fake_cmd))
        if args.friend
        else resolve(
            registry,
            available_lenses(),
            os.environ,
            shutil.which,
            include_self=args.include_self,
            timeout=args.timeout,
        )
    )
    if not specs:
        raise NoFriendsError("no usable friends for mode 'report'")
    for spec in specs:
        validate_friend_name(spec.name)

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
    downgrades: list[str] = []
    if len(specs) == 1:
        # --friend REPLACES the roster rather than augmenting discovery (see
        # cliargs._specs_from_flags above), so a single --friend flag -- or,
        # per design doc §8.3, discovery itself resolving to just one friend
        # -- produces a run that cannot cross-examine anything: it is one
        # reviewer's opinion, not disagreement between several. That
        # reduced guarantee must be visible in run.json/report.md rather
        # than a single-reviewer report quietly looking like the real
        # thing -- the same rule already applied to every other downgrade
        # this function records.
        downgrades.append(
            f"only one friend ({specs[0].name}) resolved for this run; "
            "cross-examination needs at least two independent friends, so "
            "this report reflects a single reviewer's opinion, not "
            "disagreement between several."
        )
    abort_event = threading.Event()
    abort_signum: dict[str, int | None] = {"value": None}
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None] = [None]

    def _handle_abort(signum: int, frame: FrameType | None) -> None:
        abort_signum["value"] = signum
        abort_event.set()
        # spawn.run_process (via dispatch._dispatch's abort_event) already
        # notices this on its own next poll and terminates its process
        # group promptly -- but the main thread here may be blocked inside
        # pool.map()'s wait for that same worker future. Shutting the pool
        # down without waiting means this handler itself never blocks, and
        # the main thread's wait resolves as soon as the worker's own
        # abort-triggered return lands, not whenever `with pool:`'s
        # implicit wait=True would otherwise have unblocked it.
        pool = active_pool[0]
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # signal.signal() only works from the main thread of the main
    # interpreter -- called from anywhere else (a caller's own
    # threading.Thread invoking cmd_run directly, e.g.) it raises
    # ValueError. cmd_run is "library-ish" (the same premise behind
    # restoring handlers unconditionally below), so a non-main-thread
    # caller is a real, contemplated audience, not a hypothetical one --
    # this must degrade, not crash before the try/finally below even
    # starts. installed_handlers records exactly which signals were
    # actually captured so the finally below restores only those, and the
    # degradation itself is recorded in `downgrades` (the same place an
    # artifact-outside-a-repo downgrade goes) so a run that cannot be
    # signal-aborted is visible in run.json rather than looking identical
    # to one that can be.
    installed_handlers: dict[int, _SignalHandler] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):
            installed_handlers[sig] = signal.signal(sig, _handle_abort)
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

        run_started = time.monotonic()
        run_id = f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        store = RunStore(Path(args.out) if args.out else default_root(), run_id)
        frozen, digest = store.artifact_copy(artifact)
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
        needs_snapshot = any(s.scope == "repo" for s in specs) or args.mode == "gate"
        if repo_root is not None and needs_snapshot:
            snapshot_sha = isolation.snapshot_commit(repo_root)

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

        all_claims: list[Claim] = []
        all_aliases: list[Alias] = []
        friends_meta: list[dict[str, Any]] = []
        counter = 0
        any_success = False
        cross = None
        streak = 0
        iterations_run = 0

        for iteration in range(1, max_iterations + 1):
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
            if budget.out_of_time(time.monotonic()):
                budget.exhaust(f"--max-wall-clock reached before iteration {iteration}")
                break

            # Re-read on every iteration. The runner never edits an artifact
            # (§7.5), so a `loop` picks up a revision only if something else
            # made one between iterations; when nothing did, the identical
            # artifact produces identical claims, they all alias, and the
            # dry-round streak is what ends the loop.
            artifact_text = artifact.read_text(encoding="utf-8")

            critique, all_claims, counter = run_critique(
                specs,
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
            )
            budget.spend(critique.calls)
            iterations_run = iteration
            friends_meta.extend(critique.friends_meta)
            downgrades.extend(critique.downgrades)
            all_aliases.extend(critique.aliases)
            any_success = any_success or critique.any_success

            if args.mode in JUDGING_MODES and all_claims:
                # Only worth entering with claims in hand: with none there is
                # nothing to judge, and a judging round would cost a full
                # fan-out to decide nothing. A critique report is the honest
                # result.
                cross = run_rounds(
                    specs,
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
                )
                all_claims = cross.claims
                friends_meta.extend(cross.friends_meta)
                downgrades.extend(cross.downgrades)

            if args.mode != "loop":
                break

            # §7.3's streak arithmetic. A failed round resets rather than
            # counting: a round that did not complete is not evidence of
            # convergence.
            dry = round_is_dry(critique.produced_only_aliases, not critique.any_failed)
            streak = next_streak(streak, failed=critique.any_failed, dry=dry)
            states = list(cross.states.values()) if cross else []
            if loop_should_terminate(streak, states):
                break
            if budget.exhausted_by:
                break

        # §7.5's gate. Evaluated against the run's own (empty) resolution
        # set, so a fresh gate run always reports what needs attention rather
        # than passing: resolutions are recorded afterwards, by `afriend
        # resolve`, which re-evaluates this same rule.
        blocking: list[Claim] = []
        if args.mode == "gate":
            blocking = blocking_claims(all_claims, cross.states if cross else {}, [])

        meta: dict[str, Any] = {
            "mode": args.mode,
            "preset": args.preset,
            "artifact": artifact.name,
            "artifact_hash": digest,
            # Persisted so `afriend resolve` can verify a location against
            # how this run first saw it (§6.4). Without them a resolution
            # could only ever be `unverifiable`, since the snapshot commit
            # exists but nothing would remember which one it was.
            "repo_root": str(repo_root) if repo_root else None,
            "snapshot_sha": snapshot_sha,
            "friends": friends_meta,
            "downgrades": downgrades,
        }
        if args.mode == "loop":
            meta["iterations_run"] = iterations_run
            meta["dry_streak"] = streak
        if args.mode == "gate":
            meta["gate_blocked"] = bool(blocking)
            meta["gate_blocking_claims"] = [c.id for c in blocking]
        if cross is not None:
            meta["rounds_run"] = cross.rounds_run
            meta["claim_states"] = cross.states
            meta["amendment_notes"] = cross.notes
            meta["ceiling_hit"] = cross.ceiling_hit
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
        print(store.run_dir)

        if abort_signum["value"] is not None:
            # Distinct from both branches below: a run cancelled by signal
            # is neither "succeeded" (0) nor merely "incomplete because
            # every friend failed on its own" (1) -- it never got the
            # chance to finish at all. 128+signum is the conventional
            # shell convention for "killed by signal N" and does not
            # collide with any of this tool's other exit codes (2, 3, 10,
            # 11, 1, 0).
            print(f"afriend: aborted by signal {abort_signum['value']}", file=sys.stderr)
            return 128 + abort_signum["value"]
        # §7.6's exit precedence. A ceiling outranks every outcome below it
        # because a truncated run has not evaluated anything: a CI wrapper
        # can then treat 11 as "retry" and 1 as "block" without ambiguity.
        if cross is not None and cross.ceiling_hit is not None:
            print(f"afriend: {cross.ceiling_hit}", file=sys.stderr)
            return CeilingError.exit_code
        # A run where not one friend produced a usable result (every round
        # failed/timed out) is not a success -- exit 1 ("gate blocked or
        # incomplete") rather than 0, so a caller cannot mistake "we ran the
        # mechanism" for "we got a trustworthy critique". Distinct from
        # NoFriendsError's exit 3, which fires before any friend is even
        # dispatched.
        if not any_success:
            return 1
        if args.mode == "gate" and blocking:
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
    finally:
        # A distinct loop variable name from the `for sig in (...)` loop
        # above: reusing `sig` here would bind it to a different type
        # (installed_handlers' int keys vs. that loop's Signals values)
        # in the same function scope.
        for restored_sig, previous in installed_handlers.items():
            signal.signal(restored_sig, previous)

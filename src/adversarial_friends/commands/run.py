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
from ..dispatch import PROMPT_ARGV_WARN_BYTES
from ..errors import CeilingError, NoFriendsError, UsageError
from ..ids import format_claim_id, validate_friend_name
from ..ledger import Claim
from ..merge import exact_merge
from ..paths import ADAPTER_DIR
from ..prompt import _build_friend_prompt, available_lenses
from ..report import render
from ..roster import resolve
from ..rounds import dispatch_round, persist_result
from ..runstore import RunStore, default_root
from ..verdictschema import schema_path as verdict_schema_path
from .crossexam import run_rounds

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
    if args.mode not in ("report", "crossexam"):
        raise UsageError(
            f"mode {args.mode!r} is not implemented yet; 'report' and 'crossexam' are available"
        )
    if args.max_rounds < 2 and args.mode == "crossexam":
        # Round 1 is the critique round; judging starts at round 2. A
        # crossexam capped at one round is a report with a misleading name.
        raise UsageError(
            f"--max-rounds={args.max_rounds} leaves no judging round for "
            "--mode crossexam (round 1 is the critique round; judging starts "
            "at round 2). Use --mode report, or --max-rounds 2 or more."
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

        # Every friend gets its OWN prompt, built from its own lens -- not a
        # single prompt.txt shared byte-for-byte across every friend
        # regardless of --friend cli:lens (that was the bug: the lens name
        # was recorded for bookkeeping but its prose never reached the
        # friend, so the only diversity in a run was model diversity).
        # Written to round-1/<name>.prompt next to that friend's .raw/.meta
        # so a human can see exactly what each friend was asked. A missing
        # lens file downgrades that one friend to the generic prompt rather
        # than failing the run -- see prompt._build_friend_prompt.
        prompt_for: dict[str, Path] = {}
        advisory_for: dict[str, bool] = {}
        for spec in specs:
            prompt_text, advisory, lens_downgrade = _build_friend_prompt(spec, artifact_text)
            if lens_downgrade:
                downgrades.append(lens_downgrade)
            # claude, opencode, and agy all place the WHOLE prompt in one
            # argv element (prompt_mode "trailing-arg"/"flag-value"); Linux
            # commonly caps a single argument near 128KB (the limit varies
            # by OS -- this runner is not always run on Linux), so a large
            # artifact can make Popen() fail with E2BIG ("Argument list too
            # long"). This is detected, not solved -- switching prompt
            # modes is a design change, out of scope here (see
            # spawn.run_process's OSError handling for what happens if it
            # fires anyway). Recording the risk up front means an E2BIG
            # failure is already explained by the time it's read, not a
            # surprise raw exit code.
            if spec.cli != "fake":
                adapter = registry[spec.cli]
                if adapter.prompt_mode != "stdin":
                    prompt_bytes = len(prompt_text.encode("utf-8"))
                    if prompt_bytes > PROMPT_ARGV_WARN_BYTES:
                        downgrades.append(
                            f"{spec.name}: prompt is {prompt_bytes} bytes and "
                            f"{adapter.name} passes it as a single argv element "
                            f"(prompt_mode={adapter.prompt_mode!r}); Linux commonly "
                            "caps a single argument near 128KB (the limit varies by "
                            "OS), so this friend's dispatch may fail with 'Argument "
                            "list too long' (E2BIG)."
                        )
            prompt_path = store.friend_prompt_path(1, spec.name)
            prompt_path.write_text(prompt_text, encoding="utf-8")
            prompt_for[spec.name] = prompt_path
            advisory_for[spec.name] = advisory

        # Isolation: every friend gets its own private working directory, torn
        # down at the end regardless of how dispatch finishes (including on a
        # raised exception, or an abort mid-setup -- see the `if
        # abort_event.is_set(): break` below). Repo-scope friends -- those
        # whose adapter declared readonly_argv and were not downgraded above
        # -- run inside their own git worktree checked out from one shared
        # snapshot commit; every other friend runs inside its own bare
        # doc_scope_dir holding only a copy of the artifact. Giving every
        # friend (not just non-readonly ones) a private worktree is a
        # deliberately stricter simplification of "every friend that lacks a
        # readonly capability gets its own private worktree": it trivially
        # satisfies that bar and removes any question of whether two friends
        # sharing one worktree could race each other, at the cost of one
        # `git worktree add` per repo-scope friend instead of one shared
        # checkout. The run directory itself (`store.run_dir`) is never
        # nested inside any of these -- it always lives under `--out` or
        # default_root(), never under the isolation tempdir below.
        snapshot_sha = None
        if repo_root is not None and any(s.scope == "repo" for s in specs):
            snapshot_sha = isolation.snapshot_commit(repo_root)

        def _track_pool(pool: concurrent.futures.ThreadPoolExecutor | None) -> None:
            active_pool[0] = pool

        results = dispatch_round(
            specs,
            1,
            prompt_for,
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

        counter = 0
        all_claims: list[Claim] = []
        all_aliases = []
        friends_meta = []
        any_success = False
        for spec, capability, outcome in results:
            friends_meta.append(persist_result(store, 1, spec, capability, outcome))
            if outcome.failure_reason is not None:
                continue
            any_success = True
            incoming = []
            for finding in (outcome.result.payload or {}).get("findings", []):
                counter += 1
                incoming.append(
                    Claim(
                        id=format_claim_id(counter),
                        supersedes=None,
                        origin=[f"{spec.cli}/{spec.lens}"],
                        lens=spec.lens,
                        round=1,
                        advisory=advisory_for[spec.name],
                        severity=finding["severity"],
                        claim=finding["claim"],
                        location=finding.get("location"),
                        evidence=finding["evidence"],
                        failure_scenario=finding["failure_scenario"],
                        suggested_fix=finding["suggested_fix"],
                    )
                )
            kept, aliases, updated_existing = exact_merge(all_claims, incoming, round_no=1)
            # Every incoming claim is written to the ledger, not just the
            # ones exact_merge kept: an Alias record's `duplicate` id must
            # resolve to a real `claim` record, or claims.jsonl has a
            # dangling reference -- a reader following canonical<-duplicate
            # links (the only way to recover full corroboration from the
            # ledger alone; see merge.exact_merge's docstring) hits a dead
            # end. `incoming` already IS the superset of `kept` plus every
            # claim that became an alias, so writing it once here replaces
            # writing `kept` alone.
            for record in incoming:
                store.ledger.append(record)
            for alias in aliases:
                store.ledger.append(alias)
            if updated_existing:
                # A canonical claim from an EARLIER friend just gained this
                # friend's origin too (it aliased one of that friend's
                # claims). The ledger keeps its original, immutable record
                # as first written -- Alias + the duplicate's own claim
                # record (written above) already let a reader reconstruct
                # the same corroboration from claims.jsonl alone -- but the
                # in-memory `all_claims` this run still uses (for the NEXT
                # friend's dedup pass, and for the final report) must
                # reflect the grown origin, or report.md would undercount
                # how many friends actually agreed.
                updated_by_id = {c.id: c for c in updated_existing}
                all_claims = [updated_by_id.get(c.id, c) for c in all_claims]
            all_claims.extend(kept)
            all_aliases.extend(aliases)

        cross = None
        if args.mode == "crossexam" and all_claims:
            # Only worth entering with claims in hand: with none there is
            # nothing to judge, and a judging round would cost a full fan-out
            # to decide nothing. A round-1 report is the honest result.
            budget = Budget(
                max_calls=(
                    args.max_calls
                    if args.max_calls is not None
                    else derive_max_calls(len(specs), args.max_rounds, max_loop_iterations=1)
                ),
                max_rounds=args.max_rounds,
                max_wall_clock_s=args.max_wall_clock,
                started=run_started,
            )
            budget.spend(len(results))
            unreachable = warn_if_unreachable(len(specs), args.max_rounds, budget.max_calls)
            if unreachable:
                downgrades.append(unreachable)
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
                args.max_rounds,
                attributed=args.attributed,
                on_pool=_track_pool,
            )
            all_claims = cross.claims
            friends_meta.extend(cross.friends_meta)
            downgrades.extend(cross.downgrades)

        meta: dict[str, Any] = {
            "mode": args.mode,
            "preset": args.preset,
            "artifact": artifact.name,
            "artifact_hash": digest,
            "friends": friends_meta,
            "downgrades": downgrades,
        }
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

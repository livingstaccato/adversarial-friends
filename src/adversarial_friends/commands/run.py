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
import tempfile
import threading
import time
from types import FrameType
from typing import Any
import uuid

from .. import isolation
from ..adapters import Capability, FriendSpec, load_adapters
from ..claimschema import schema_path
from ..cliargs import _specs_from_flags
from ..dispatch import (
    _UNKNOWN_CAPABILITY,
    PROMPT_ARGV_WARN_BYTES,
    _dispatch,
    _exception_outcome,
    _stderr_tail,
)
from ..errors import AfError, NoFriendsError, UsageError
from ..ids import format_claim_id, validate_friend_name
from ..ledger import Claim
from ..merge import exact_merge
from ..paths import ADAPTER_DIR
from ..prompt import _build_friend_prompt, available_lenses
from ..report import render
from ..roster import resolve
from ..runstore import RunStore, default_root
from ..spawn import SpawnResult

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
    if args.mode != "report":
        raise UsageError(f"mode {args.mode!r} is not implemented yet; only 'report' is available")
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

        results = []
        with tempfile.TemporaryDirectory(prefix="af-isolation-") as iso_root_str:
            iso_root = Path(iso_root_str)
            cwd_for: dict[str, Path] = {}
            try:
                for spec in specs:
                    if abort_event.is_set():
                        break
                    dest = iso_root / spec.name
                    if spec.scope == "repo":
                        # A spec only reaches scope="repo" when repo_root was
                        # not None (see the downgrade-to-"doc" branch above),
                        # and any(s.scope == "repo" for s in specs) is exactly
                        # what triggers snapshot_sha's assignment just above --
                        # both are non-None by construction whenever this
                        # branch runs.
                        assert repo_root is not None
                        assert snapshot_sha is not None
                        isolation.add_worktree(repo_root, snapshot_sha, dest)
                    else:
                        isolation.doc_scope_dir(dest, artifact)
                    cwd_for[spec.name] = dest

                def _run_one(spec: FriendSpec) -> tuple[FriendSpec, Capability, SpawnResult]:
                    # spawn.run_process already turns most process-launch
                    # failures (missing binary, E2BIG, ENOEXEC, ...) into a
                    # SpawnResult rather than raising. This is the second,
                    # broader layer: ANYTHING else that goes wrong for one
                    # friend -- a bug in adapter wiring, an OSError that
                    # still somehow escaped Popen(), anything unforeseen --
                    # must not end the whole run. pool.map (below) collects
                    # this function's return values one per future; letting
                    # an exception escape here would propagate out of
                    # pool.map entirely, losing every other friend's
                    # (possibly already-succeeded) result along with it. A
                    # deliberate AfError (e.g. check_denied_values inside
                    # dispatch._dispatch refusing a dangerous flag) is a
                    # real, intentional stop condition with its own exit
                    # code -- that still propagates, unlike a genuinely
                    # unexpected exception.
                    try:
                        return _dispatch(
                            spec,
                            cwd_for[spec.name],
                            registry,
                            fake_cmd,
                            prompt_for[spec.name],
                            schema_file,
                            abort_event,
                        )
                    except AfError:
                        raise
                    except Exception as exc:
                        return spec, _UNKNOWN_CAPABILITY, _exception_outcome([], exc)

                # Only specs that actually got an isolation directory (i.e.
                # every one of them, unless the loop above broke early on
                # abort) are dispatched -- _run_one would otherwise KeyError
                # looking up cwd_for for a spec whose setup never happened.
                dispatch_specs = [s for s in specs if s.name in cwd_for]
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, len(dispatch_specs))
                ) as pool:
                    active_pool[0] = pool
                    try:
                        results = list(pool.map(_run_one, dispatch_specs))
                    finally:
                        active_pool[0] = None
            finally:
                for spec in specs:
                    if spec.scope == "repo" and spec.name in cwd_for:
                        # Same invariant as the add_worktree call above:
                        # scope == "repo" implies repo_root is not None.
                        assert repo_root is not None
                        isolation.remove_worktree(repo_root, cwd_for[spec.name])
                # doc_scope_dir entries are cleaned up automatically: they all
                # live under iso_root, and the TemporaryDirectory context
                # manager removes it (and everything under it) on exit below,
                # independent of whether dispatch raised.

        counter = 0
        all_claims: list[Claim] = []
        all_aliases = []
        friends_meta = []
        any_success = False
        for spec, capability, outcome in results:
            raw_path, _json_path, meta_path = store.friend_paths(1, spec.name)
            raw_path.write_text(outcome.stdout, encoding="utf-8")
            meta_path.write_text(
                f"argv={outcome.argv}\nexit={outcome.exit_code}\n"
                f"duration_s={outcome.duration_s:.2f}\ntimed_out={outcome.timed_out}\n"
                f"orphans_suspected={outcome.orphans_suspected}\n",
                encoding="utf-8",
            )
            # stderr is captured by spawn.run_process on every friend
            # (SpawnResult.stderr) but was previously written nowhere and
            # referenced nowhere -- an unauthenticated friend showed up as
            # "failed: exit 1" with a 0-byte .raw and no diagnosis anywhere.
            # Persisted unconditionally (even "" -- a stable, always-present
            # file beats one that only sometimes exists), with a short tail
            # folded into the status column for a failed friend so the
            # diagnosis is visible without opening a second file.
            err_path = store.friend_err_path(1, spec.name)
            err_path.write_text(outcome.stderr, encoding="utf-8")
            status = "ok" if outcome.failure_reason is None else f"failed: {outcome.failure_reason}"
            if outcome.failure_reason is not None and outcome.stderr.strip():
                status += (
                    f" (stderr: {_stderr_tail(outcome.stderr)}; "
                    f"full text in round-1/{spec.name}.err)"
                )
            if outcome.orphans_suspected:
                # A leaked descendant must not look identical to a clean run --
                # surfaced in the same status column readers already check for
                # "failed", rather than a silent field only run.json carries.
                status += " [orphans suspected]"
            friends_meta.append(
                {
                    "name": spec.name,
                    "model": spec.model,
                    "effort": spec.effort,
                    "readonly": capability.readonly,
                    "scope": spec.scope,
                    "status": status,
                }
            )
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

        meta = {
            "mode": args.mode,
            "preset": args.preset,
            "artifact": artifact.name,
            "artifact_hash": digest,
            "friends": friends_meta,
            "downgrades": downgrades,
        }
        store.write_run_json(meta)
        store.write_report(render(all_claims, all_aliases, meta))
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
        # "report" mode never gates on individual claims, but a run where not
        # one friend produced a usable result (every round failed/timed out) is
        # not a success either -- exit 1 ("gate blocked or incomplete") rather
        # than 0, so a caller cannot mistake "we ran the mechanism" for "we got
        # a trustworthy critique". Distinct from NoFriendsError's exit 3, which
        # fires before any friend is even dispatched.
        return 0 if any_success else 1
    finally:
        # A distinct loop variable name from the `for sig in (...)` loop
        # above: reusing `sig` here would bind it to a different type
        # (installed_handlers' int keys vs. that loop's Signals values)
        # in the same function scope.
        for restored_sig, previous in installed_handlers.items():
            signal.signal(restored_sig, previous)

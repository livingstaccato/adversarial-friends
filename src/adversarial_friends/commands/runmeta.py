"""run.json's shape, and rebuilding a halted run's configuration from it.

Split out of commands/run.py when --resume arrived: cmd_run crossed the
then-current line cap, and the metadata contract is a separate concern from
the run loop that produces it.

**A resumed run takes its configuration from the run directory, never from
the resuming command line.** §4.2 requires that the same response produce
the same run; a flag that changed between halt and resume would quietly
break that, and the failure would look like nondeterminism rather than
operator error.
"""

import argparse
import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapters import FriendSpec
from ..ceilings import BUDGET_EXHAUSTED, Budget
from ..errors import UsageError
from ..ledger import Claim
from ..report import render
from ..reviewstate import ReviewState
from ..runstore import RunStore, default_root
from ..trust import MODEL_RE
from ..verdicts import judges_for, loop_should_terminate
from .exits import decide_exit

if TYPE_CHECKING:
    from .crossexam import CrossexamOutcome

# Everything a resumed run must restore rather than re-read from a second
# command line. §4.2 requires that the same response produce the same run;
# taking any of these from the resuming invocation would let a flag change
# between halt and resume and silently alter the outcome.
_RESUMABLE_ARGS = (
    "mode",
    "preset",
    "merge",
    "timeout",
    "attributed",
    "include_self",
    "host_provider",
    "enable_provider",
    "disable_provider",
    "allow_unsandboxed_friend",
    "max_rounds",
    "max_calls",
    "max_wall_clock",
    "max_loop_iterations",
    # The ledger identity is (cli, lens, model, effort) (§8.1), so these
    # decide what a claim's `origin` says. Left out, a run resumed without
    # `--model` re-resolved its friends under different identities than the
    # ones frozen in the ledger -- and a claim's own author, no longer
    # matching its origin, was handed its own claim to judge.
    "model",
    "effort",
    "roster",
    "lens",
    # Everything else that changes what is dispatched or how. Left out, a
    # resume silently ran a different roster under different rules than the
    # halted run recorded -- fewer or more friends, a dropped --pass-env,
    # unvalidated flags appearing or vanishing.
    "max_friends",
    "require_friends",
    "pass_env",
    "unsafe_extra_args",
    "i_accept_unsandboxed",
    "keep",
)


def _find_run_dir(run_id: str, out: str | None) -> Path:
    """Accept a run id or the directory path `afriend run` printed."""
    candidate = Path(run_id)
    if candidate.is_dir():
        return candidate
    root = Path(out) if out else default_root()
    resolved = root / run_id
    if not resolved.is_dir():
        raise UsageError(f"cannot resume: no such run: {run_id!r} (looked in {root})")
    return resolved


def _base_meta(
    args: argparse.Namespace,
    artifact: Path,
    digest: str,
    friends_meta: list[dict[str, Any]],
    downgrades: list[str],
    specs: list[FriendSpec],
    repo_root: Path | None = None,
    snapshot_sha: str | None = None,
    preset: str = "inherit",
    roster_source: str | None = None,
    env_withheld: list[str] | None = None,
) -> dict[str, Any]:
    """run.json's common fields.

    `invocation` and `roster` exist for --resume: a resumed run rebuilds its
    whole configuration from here rather than from a second command line,
    because §4.2 requires the same response to produce the same run and a
    flag that changed between halt and resume would quietly break that.
    """
    return {
        "mode": args.mode,
        # The preset ACTUALLY used, not the flag: it defaults per mode (gate
        # defaults to thorough, §7), so printing the flag would report
        # `None` for a run that emitted high-effort flags everywhere.
        "preset": preset,
        "roster_source": roster_source,
        "merge": args.merge,
        "artifact": artifact.name,
        "artifact_path": str(stable_artifact_path(artifact)),
        "artifact_hash": digest,
        # Persisted so `afriend resolve` can verify a location against how
        # this run first saw it (§6.4). Without them a resolution could only
        # ever be `unverifiable`, since the snapshot commit exists but
        # nothing would remember which one it was.
        "repo_root": str(repo_root) if repo_root else None,
        "snapshot_sha": snapshot_sha,
        "friends": friends_meta,
        "downgrades": downgrades,
        "invocation": {
            "artifact": str(artifact),
            "friend": list(args.friend),
            **{name: getattr(args, name, None) for name in _RESUMABLE_ARGS},
        },
        "roster": [dataclasses.asdict(s) for s in specs],
        # Names of environment variables withheld from confined friends.
        # NAMES ONLY -- a run directory that recorded the values to prove
        # they were protected would be the leak it exists to prevent.
        "env_withheld": env_withheld or [],
    }


def _restore_args(args: argparse.Namespace) -> argparse.Namespace:
    """Rebuild the original invocation's settings from its run directory."""
    run_dir = _find_run_dir(args.resume, args.out)
    meta_path = run_dir / "run.json"
    if not meta_path.is_file():
        raise UsageError(f"cannot resume: {run_dir} has no run.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    saved = meta.get("invocation")
    if not isinstance(saved, dict):
        raise UsageError(
            f"cannot resume: {meta_path} predates resume support and does not "
            "record how the run was invoked."
        )
    restored = argparse.Namespace(**vars(args))
    for name in _RESUMABLE_ARGS:
        if name in saved:
            setattr(restored, name, saved[name])
    restored.artifact = saved.get("artifact")
    restored.friend = saved.get("friend", [])
    # The concrete roster the halted run resolved, not the inputs that
    # produced it. §4.2 requires the same response to produce the same run,
    # and re-resolving cannot promise that: a roster file can be edited and
    # discovery re-reads whatever CLIs are installed now, so a resume could
    # change quorum, or hand a claim's author a new identity under which it
    # judges its own claim.
    restored._resume_roster = [
        FriendSpec(**entry) for entry in meta.get("roster", []) if isinstance(entry, dict)
    ]
    restored.out = str(run_dir.parent)
    restored._resume_dir = run_dir
    restored._resume_meta = meta
    # Where in a `loop` this run stopped. An orchestrator halt happens once
    # per iteration, so a resumed loop has to re-enter the iteration it
    # halted in and then carry on -- iteration 1 of 5 resuming as though it
    # were the whole run would silently drop four.
    restored._resume_iteration = int(meta.get("iterations_run", 1) or 1)
    restored._resume_streak = int(meta.get("dry_streak", 0) or 0)
    return restored


IMPLEMENTED_MODES = frozenset({"report", "crossexam", "gate", "loop"})
# Every mode that judges claims after critiquing them. `report` stops at the
# critique round; the rest all run cross-examination and differ only in what
# they do with its result. (The explanation lived in commands/run.py, above
# the import, after the constant itself moved here.)
JUDGING_MODES = frozenset({"crossexam", "gate", "loop"})


def stable_artifact_path(artifact: Path) -> Path:
    """Return an absolute invocation path without following the final symlink."""
    return artifact.parent.resolve() / artifact.name


def _validate_positive(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        option = name.replace("_", "-")
        raise UsageError(f"--{option}={value!r}: expected a positive integer")


def validate_run_args(args: argparse.Namespace) -> tuple[argparse.Namespace, Path]:
    """Everything that can be refused before a single friend is dispatched.

    Grouped here so the refusals read as one list. Each exists because the
    alternative is a run that looks like it worked: a crossexam with no
    judging round, a loop that halts per iteration into state this build
    cannot reconstruct, a mode nothing implements.
    """
    if args.resume:
        # A resumed run takes its whole configuration from the run directory
        # rather than from this invocation. §4.2 requires that the same
        # response produce the same run, and re-reading flags from a second
        # command line is exactly how that stops being true.
        args = _restore_args(args)
    for name in (
        "timeout",
        "max_friends",
        "max_calls",
        "max_wall_clock",
        "max_loop_iterations",
        "require_friends",
    ):
        value = getattr(args, name, None)
        if value is not None:
            _validate_positive(name, value)
    if args.max_rounds < 1:
        raise UsageError("--max-rounds must be at least 1 (a positive integer)")
    if args.model is not None and MODEL_RE.fullmatch(args.model) is None:
        raise UsageError(f"invalid model {args.model!r}: must match {MODEL_RE.pattern!r}")
    if not args.artifact:
        raise UsageError("an artifact path is required (or --resume RUN_ID)")
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
    return args, artifact


def unresolved_loop_states(claims: list[Any], cross: Any, roster: list[str]) -> list[str]:
    """Claim states §7.3 actually terminates on.

    Two kinds of claim are excluded, both for the same reason: a further
    iteration cannot change them, so waiting on them forces every loop to
    its ceiling -- the failure §7.3's H4 correction exists to prevent,
    arriving through two more doors.

    Advisory claims, because their lens deliberately does not demand a
    failure scenario. And claims with no independent judge on the roster,
    which stay `unproven` however many iterations run.
    """
    if cross is None:
        return []
    advisory = {c.id for c in claims if c.advisory}
    by_id = {c.id: c for c in claims}
    states = []
    for cid, state in cross.states.items():
        if cid in advisory:
            continue
        claim = by_id.get(cid)
        # A claim every friend co-authored has no independent judge, so no
        # further iteration can move it off `unproven` -- waiting for it is
        # waiting forever, and the loop ran to its iteration ceiling doing
        # exactly that. It is still reported, and still blocks a gate; it
        # just cannot be what a loop is waiting for. (An amended claim's
        # successor inherits both the author's and the amenders' origins,
        # which on a two-friend roster is the whole roster.)
        if claim is not None and not judges_for(claim, roster):
            continue
        states.append(state)
    return states


def loop_is_done(streak: int, claims: list[Any], cross: Any, roster: list[str]) -> bool:
    """§7.3's termination test, asked the same way from both places a loop
    iteration can end -- a normal one, and one resumed after an orchestrator
    halt. They drifted apart while there was a copy in each."""
    return loop_should_terminate(streak, unresolved_loop_states(claims, cross, roster))


def finalize_meta(
    meta: dict[str, Any],
    mode: str,
    *,
    iterations_run: int,
    streak: int,
    blocking: list[Claim],
    budget: Budget,
    downgrades: list[str],
    cross: "CrossexamOutcome | None",
    rounds_reached: int,
) -> dict[str, Any]:
    """Fold every mode's end-of-run fields into `meta`, in place.

    Extracted from cmd_run for the same reason the rest of this module was:
    the run loop crossed the then-current line cap again, and this block is data
    assembly with no control flow of its own -- it reads finished state and
    writes keys. Keeping it beside the rest of run.json's shape puts every
    field that reaches that file in one place, which is where a reader looks
    when a key is missing.

    Mutates and returns the same dict rather than building a new one: the
    caller passes the base metadata and expects its keys to survive, and a
    copy here would silently drop anything added between the two points.
    """
    if mode == "loop":
        meta["iterations_run"] = iterations_run
        meta["dry_streak"] = streak
    if mode == "gate":
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
    return meta


def finish_run(
    args: argparse.Namespace,
    store: RunStore,
    base_meta: dict[str, Any],
    cross: "CrossexamOutcome | None",
    abort_signum: int | None,
    any_success: bool,
    succeeded_friends: int | None,
    iterations_run: int,
    streak: int,
    downgrades: list[str],
    budget: Budget,
    rounds_reached: int,
    auth_abort: str | None = None,
) -> int:
    """Wrap up a completed run: the gate's blocking claims, the finalized
    meta, run.json and report.md on disk, the printed path, and the exit
    code.

    Split out of cmd_run's tail for the same reason `finalize_meta` was:
    the function crossed the then-current line cap, and finishing a run is
    a self-contained concern separate from the loop that produced
    everything it wraps up.
    """
    # Reconstruct once from the durable ledger, then use this exact state for
    # both the gate decision and the report. Process-local accumulators must
    # not be able to disagree with what a resumed reader will observe.
    review = ReviewState.replay(store.ledger.records())
    review.copy_transition_warnings(downgrades)
    blocking: list[Claim] = []
    if args.mode == "gate":
        blocking = review.blocking(cross.states if cross else {})

    meta = finalize_meta(
        base_meta,
        args.mode,
        iterations_run=iterations_run,
        streak=streak,
        blocking=blocking,
        budget=budget,
        downgrades=downgrades,
        cross=cross,
        rounds_reached=rounds_reached,
    )
    store.write_run_json(meta)
    store.write_report(
        render(
            review,
            meta,
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
        abort_signum,
        any_success,
        args.mode,
        cross,
        blocking,
        ceiling_hit=BUDGET_EXHAUSTED if budget.exhausted_by else None,
        succeeded_friends=succeeded_friends,
        require_friends=args.require_friends,
        auth_abort=auth_abort,
    )

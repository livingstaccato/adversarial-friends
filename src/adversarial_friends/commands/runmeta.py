"""run.json's shape, and rebuilding a halted run's configuration from it.

Split from commands/run.py because its metadata contract is a separate concern.

**A resumed run restores deterministic configuration from the run directory.**
Security grants are the deliberate exception: metadata records them but can never confer
them, so the resuming command line must repeat each prior grant exactly.
"""

import argparse
import dataclasses
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapters import FriendSpec, validate_roster_entry_uniqueness
from ..authority import AuthorityPolicy
from ..ceilings import BUDGET_EXHAUSTED, Budget
from ..cliargs import MERGE_CHOICES, RUN_MODES
from ..errors import UsageError
from ..failures import RepeatTracker
from ..jsonio import load_json_object
from ..ledger import Claim
from ..outcomes import MAX_JSON_SAFE_INTEGER, RunOutcome, terminal_outcome
from ..presets import PRESETS
from ..report import render
from ..reviewstate import ReviewState
from ..runstore import RunStore, default_root
from ..snapshots import SnapshotIdentity, record_snapshot
from ..themes import MAX_THEME_PROPOSALS, ThemeProposal, bounded_theme_metadata
from ..trust import MODEL_RE, validate_roster_entry
from ..verdicts import TERMINAL_STATES, judges_for, loop_should_terminate
from . import resumevalidation
from .checkpoint import (
    legacy_successful_friend_ids,
    normalize_friend_rows,
    normalize_repeat_tracker,
    normalize_resume_report_state,
    validate_lifecycle_and_snapshot,
)
from .exits import decide_exit
from .runmeta_migration import (
    CURRENT_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION,
    migrate_meta as migrate_meta,
)

if TYPE_CHECKING:
    from .crossexam import CrossexamOutcome

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
    "keep",
)

# Invocation-local authority grants are recorded for audit and continuity,
# but never restored from attacker-editable run.json. A resume must repeat
# any non-default grant exactly on its own command line.
_SECURITY_GRANTS: dict[str, tuple[type, object]] = {
    "allow_external_tools": (list, []),
    "allow_unsandboxed_friend": (bool, False),
    "unsafe_extra_args": (str, None),
    "i_accept_unsandboxed": (bool, False),
    "pass_env": (list, []),
}

_OPTIONAL_STRINGS = {"preset", "host_provider", "model", "effort", "roster"}
_STRING_SETTINGS = {"mode", "merge"}
_BOOL_SETTINGS = {"attributed", "include_self", "keep"}
_LIST_SETTINGS = {"enable_provider", "disable_provider", "lens"}
_OPTIONAL_INTS = {
    "max_friends",
    "max_calls",
    "require_friends",
}
_INT_SETTINGS = {
    "timeout",
    "max_rounds",
    "max_wall_clock",
    "max_loop_iterations",
}


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
    snapshot: SnapshotIdentity,
    snapshot_history: list[SnapshotIdentity],
    authority_policy: AuthorityPolicy,
    preset: str = "inherit",
    roster_source: str | None = None,
    env_withheld: list[str] | None = None,
    started_at: str | None = None,
    theme_proposals: list[ThemeProposal] | None = None,
    produced_new_themes: bool = False,
    prior_external_tool_policy: object = None,
) -> dict[str, Any]:
    """run.json's common fields.

    `invocation` and `roster` exist for --resume: deterministic settings are
    rebuilt from here because §4.2 requires the same response to produce the
    same run. Invocation-local security grants are only audited here; resume
    validates them against an exact, explicit command-line re-assertion.
    """
    meta: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "lifecycle_state": "running",
        "started_at": started_at or _finished_at(),
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
        "friends": friends_meta,
        "external_tool_policy": (
            "legacy-unknown"
            if prior_external_tool_policy == "legacy-unknown"
            else authority_policy.audit_summary
        ),
        "external_tool_grants": list(authority_policy.allowed_providers),
        "downgrades": downgrades,
        "invocation": {
            "artifact": str(artifact),
            "friend": list(args.friend),
            **{name: getattr(args, name, None) for name in _RESUMABLE_ARGS},
            **{
                name: (
                    list(authority_policy.allowed_providers)
                    if name == "allow_external_tools"
                    else getattr(args, name, default)
                )
                for name, (_type, default) in _SECURITY_GRANTS.items()
            },
        },
        "roster": [dataclasses.asdict(s) for s in specs],
        # Names of environment variables withheld from confined friends.
        # NAMES ONLY -- a run directory that recorded the values to prove
        # they were protected would be the leak it exists to prevent.
        "env_withheld": env_withheld or [],
        "theme_proposals": [
            proposal.to_dict() for proposal in (theme_proposals or [])[:MAX_THEME_PROPOSALS]
        ],
        "produced_new_themes": produced_new_themes,
    }
    # The nested form is authoritative. The final two fields written by
    # record_snapshot remain for v0.2 readers such as `afriend resolve`.
    record_snapshot(meta, snapshot, snapshot_history)
    return bounded_theme_metadata(meta)


def _resume_type_error(name: str, value: object) -> UsageError:
    return UsageError(
        f"cannot resume: saved --{name.replace('_', '-')} has invalid type/value {value!r}"
    )


def _validate_saved_setting(name: str, value: object) -> None:
    if name in _OPTIONAL_STRINGS and value is not None and not isinstance(value, str):
        raise _resume_type_error(name, value)
    if name in _STRING_SETTINGS and not isinstance(value, str):
        raise _resume_type_error(name, value)
    if name in _BOOL_SETTINGS and not isinstance(value, bool):
        raise _resume_type_error(name, value)
    if name in _LIST_SETTINGS and (
        not isinstance(value, list) or not all(isinstance(item, str) for item in value)
    ):
        raise _resume_type_error(name, value)
    if (
        name in _OPTIONAL_INTS
        and value is not None
        and (isinstance(value, bool) or not isinstance(value, int))
    ):
        raise _resume_type_error(name, value)
    if name in _INT_SETTINGS and (isinstance(value, bool) or not isinstance(value, int)):
        raise _resume_type_error(name, value)
    choices: tuple[str, ...] | None = None
    if name == "mode":
        choices = RUN_MODES
    elif name == "merge":
        choices = MERGE_CHOICES
    elif name == "preset" and value is not None:
        choices = PRESETS
    if choices is not None and value not in choices:
        raise _resume_type_error(name, value)


def _validate_saved_grant(name: str, value: object, expected_type: type) -> None:
    valid = isinstance(value, expected_type)
    if expected_type is bool:
        valid = type(value) is bool
    elif name == "pass_env":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif name == "unsafe_extra_args":
        valid = value is None or isinstance(value, str)
    elif name == "allow_external_tools":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not valid:
        raise _resume_type_error(name, value)


def _normalize_saved_grants(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UsageError("cannot resume: external tool grants must be a list of strings")
    grants = list(value)
    if len(grants) != len(set(grants)):
        raise UsageError("cannot resume: saved --allow-external-tools contains duplicates")
    if "*" in grants and grants != ["*"]:
        raise UsageError(
            "cannot resume: saved --allow-external-tools global '*' grant must be used alone"
        )
    return sorted(grants)


def _validated_roster_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise UsageError("cannot resume: saved roster must be a list")
    validated: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise UsageError("cannot resume: each saved roster entry must be an object")
        candidate = dict(entry)
        for field_name in ("name", "cli", "lens", "scope"):
            if not isinstance(candidate.get(field_name), str):
                raise UsageError(
                    f"cannot resume: saved roster field {field_name!r} must be a string"
                )
        for field_name in ("model", "effort"):
            if candidate.get(field_name) is not None and not isinstance(
                candidate.get(field_name), str
            ):
                raise UsageError(
                    f"cannot resume: saved roster field {field_name!r} must be a string or null"
                )
        validate_roster_entry(candidate)
        validated.append(candidate)
    return validated


def _checkpoint_count(meta: dict[str, Any], name: str, default: int) -> int:
    value = meta.get(name, default)
    if type(value) is not int or not 0 <= value <= MAX_JSON_SAFE_INTEGER:
        raise UsageError(f"cannot resume: saved {name} must be a nonnegative integer")
    return value


def _checkpoint_elapsed(meta: dict[str, Any]) -> float:
    value = meta.get("active_elapsed_s", 0.0)
    if type(value) not in {int, float}:
        raise UsageError(
            "cannot resume: saved active_elapsed_s must be a finite nonnegative number"
        )
    try:
        elapsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise UsageError(
            "cannot resume: saved active_elapsed_s must be a finite nonnegative number"
        ) from exc
    if not math.isfinite(elapsed) or elapsed < 0:
        raise UsageError(
            "cannot resume: saved active_elapsed_s must be a finite nonnegative number"
        )
    return elapsed


def _checkpoint_successes(
    meta: dict[str, Any], friends: list[dict[str, Any]], critique_round: int
) -> list[str]:
    if "successful_friend_ids" not in meta:
        successes = legacy_successful_friend_ids(friends, critique_round)
    else:
        value = meta["successful_friend_ids"]
        if type(value) is not list or not all(type(item) is str and item for item in value):
            raise UsageError(
                "cannot resume: saved successful_friend_ids must be a list of nonempty strings"
            )
        successes = list(value)
    if len(successes) != len(set(successes)):
        raise UsageError("cannot resume: saved successful_friend_ids must be unique")
    recorded_count = meta.get("succeeded_friends", len(successes))
    if type(recorded_count) is not int or recorded_count != len(successes):
        raise UsageError("cannot resume: saved succeeded_friends must match successful_friend_ids")
    return successes


def _checkpoint_themes(meta: dict[str, Any]) -> tuple[list[ThemeProposal], bool]:
    resumevalidation.validate_metadata_bound(meta)
    raw_proposals = meta.get("theme_proposals", [])
    if type(raw_proposals) is not list:
        raise UsageError("cannot resume: saved theme_proposals must be a list")
    proposals: list[ThemeProposal] = []
    seen: set[ThemeProposal] = set()
    for index, raw in enumerate(raw_proposals):
        try:
            proposal = ThemeProposal.from_dict(raw)
        except UsageError as exc:
            raise UsageError(
                f"cannot resume: saved theme_proposals[{index}] is invalid: {exc}"
            ) from exc
        if proposal in seen:
            raise UsageError("cannot resume: saved theme_proposals contains a duplicate")
        seen.add(proposal)
        proposals.append(proposal)
    produced = meta.get("produced_new_themes", False)
    if type(produced) is not bool:
        raise UsageError("cannot resume: saved produced_new_themes must be a boolean")
    return proposals, produced


def _normalized_checkpoint(
    meta: dict[str, Any],
    *,
    roster_names: set[str],
    max_calls: int | None,
    max_rounds: object,
    require_friends: object,
) -> dict[str, Any]:
    normalized = dict(meta)
    spent_calls = _checkpoint_count(meta, "spent_calls", 0)
    attempted_calls = _checkpoint_count(meta, "attempted_calls", spent_calls)
    if attempted_calls != spent_calls:
        raise UsageError("cannot resume: saved attempted_calls must equal saved spent_calls")
    if max_calls is not None and spent_calls > max_calls:
        raise UsageError("cannot resume: saved spent_calls exceeds saved max_calls")
    iterations_run = _checkpoint_count(meta, "iterations_run", 0)
    rounds_run = _checkpoint_count(meta, "rounds_run", 0)
    dry_streak = _checkpoint_count(meta, "dry_streak", 0)
    resume_iteration = _checkpoint_count(
        meta, "resume_iteration", iterations_run if iterations_run > 0 else 1
    )
    if resume_iteration < 1:
        raise UsageError("cannot resume: saved resume_iteration must be a positive integer")
    if resume_iteration not in {max(1, iterations_run), iterations_run + 1}:
        raise UsageError(
            "cannot resume: saved resume_iteration is inconsistent with iterations_run"
        )
    friends = normalize_friend_rows(meta.get("friends", []), roster_names)
    if type(max_rounds) is not int or max_rounds < 1:
        raise UsageError("cannot resume: saved max_rounds must be a positive integer")
    critique_round = (resume_iteration - 1) * max_rounds + 1
    if any(row["round"] > critique_round for row in friends):
        raise UsageError("cannot resume: saved friends contain a row after the pending round")
    successes = _checkpoint_successes(meta, friends, critique_round)
    theme_proposals, produced_new_themes = _checkpoint_themes(meta)
    if any(friend not in roster_names for friend in successes):
        raise UsageError(
            "cannot resume: saved successful_friend_ids contains a friend outside the roster"
        )
    required = meta.get("required_friends", require_friends)
    if required is not None and (
        type(required) is not int or not 1 <= required <= MAX_JSON_SAFE_INTEGER
    ):
        raise UsageError("cannot resume: saved required_friends must be a positive integer or null")
    if required != require_friends:
        raise UsageError(
            "cannot resume: saved required_friends disagrees with the original invocation"
        )
    normalized.update(
        {
            "attempted_calls": attempted_calls,
            "spent_calls": spent_calls,
            "iterations_run": iterations_run,
            "rounds_run": rounds_run,
            "dry_streak": dry_streak,
            "resume_iteration": resume_iteration,
            "active_elapsed_s": _checkpoint_elapsed(meta),
            "successful_friend_ids": successes,
            "succeeded_friends": len(successes),
            "required_friends": required,
            "repeat_tracker": normalize_repeat_tracker(meta.get("repeat_tracker", {})),
            "friends": friends,
            "theme_proposals": [proposal.to_dict() for proposal in theme_proposals],
            "produced_new_themes": produced_new_themes,
        }
    )
    normalized.update(normalize_resume_report_state(meta))
    return normalized


def _restore_args(args: argparse.Namespace) -> argparse.Namespace:
    """Rebuild the original invocation's settings from its run directory."""
    run_dir = _find_run_dir(args.resume, args.out)
    meta_path = run_dir / "run.json"
    if not meta_path.is_file():
        raise UsageError(f"cannot resume: {run_dir} has no run.json")
    try:
        meta = load_json_object(meta_path, label="saved run metadata")
    except UsageError as exc:
        raise UsageError(f"cannot resume: {exc}") from exc
    raw_version = meta.get("schema_version", 1)
    meta = migrate_meta(meta)
    resumevalidation.validate_metadata_bound(meta)
    saved = meta.get("invocation")
    if not isinstance(saved, dict):
        raise UsageError(
            f"cannot resume: {meta_path} has no valid invocation; it may predate "
            "resume support and does not record how the run was invoked."
        )
    normalize_repeat_tracker(meta.get("repeat_tracker", {}))
    for name in _RESUMABLE_ARGS:
        if name in saved:
            _validate_saved_setting(name, saved[name])
    resumevalidation.validate_saved_invocation(saved)
    artifact = saved["artifact"]
    friends = saved.get("friend", [])
    if not isinstance(friends, list) or not all(isinstance(item, str) for item in friends):
        raise UsageError("cannot resume: saved friend flags must be a list of strings")
    for name, (expected_type, default) in _SECURITY_GRANTS.items():
        saved_value = saved.get(name, default)
        _validate_saved_grant(name, saved_value, expected_type)
        current_value = getattr(args, name, default)
        _validate_saved_grant(name, current_value, expected_type)
        if name == "allow_external_tools":
            saved_value = _normalize_saved_grants(saved_value)
            current_value = _normalize_saved_grants(current_value)
        if current_value != saved_value:
            raise UsageError(
                f"cannot resume: prior --{name.replace('_', '-')} authority must be "
                "repeated exactly on the resume command line"
            )
    saved_external_grants = _normalize_saved_grants(saved.get("allow_external_tools", []))
    audit_external_grants = _normalize_saved_grants(meta.get("external_tool_grants", []))
    if audit_external_grants != saved_external_grants:
        raise UsageError("cannot resume: external_tool_grants disagrees with the saved invocation")
    roster_entries = _validated_roster_entries(meta.get("roster", []))
    validate_roster_entry_uniqueness(
        roster_entries, judging=saved.get("mode", "report") != "report"
    )
    meta = _normalized_checkpoint(
        meta,
        roster_names={entry["name"] for entry in roster_entries},
        max_calls=saved.get("max_calls"),
        max_rounds=saved.get("max_rounds", 1),
        require_friends=saved.get("require_friends"),
    )
    validate_lifecycle_and_snapshot(meta, run_dir=run_dir, legacy=raw_version == 1)
    restored = argparse.Namespace(**vars(args))
    for name in _RESUMABLE_ARGS:
        if name in saved:
            setattr(restored, name, saved[name])
    restored.artifact = artifact
    restored.friend = friends
    # Restore the frozen concrete roster; re-resolving its mutable inputs
    # could change quorum or ledger identities.
    restored._resume_roster = [FriendSpec(**entry) for entry in roster_entries]
    restored.out = str(run_dir.parent)
    restored._resume_dir = run_dir
    restored._resume_meta = meta
    # Resume the loop iteration that halted instead of restarting at one.
    restored._resume_iteration = meta["resume_iteration"]
    restored._resume_streak = meta["dry_streak"]
    restored._resume_attempted_calls = meta["attempted_calls"]
    restored._resume_spent_calls = meta["spent_calls"]
    restored._resume_iterations_run = meta["iterations_run"]
    restored._resume_rounds_run = meta["rounds_run"]
    restored._resume_active_elapsed_s = meta["active_elapsed_s"]
    restored._resume_successful_friend_ids = meta["successful_friend_ids"]
    restored._resume_theme_proposals = [
        ThemeProposal.from_dict(value) for value in meta["theme_proposals"]
    ]
    restored._resume_produced_new_themes = meta["produced_new_themes"]
    restored._resume_downgrades = list(meta.get("downgrades", []))
    return restored


IMPLEMENTED_MODES = frozenset(RUN_MODES)
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
        # Deterministic configuration comes from the run directory. Security
        # grants are checked against this invocation inside _restore_args and
        # are never restored from metadata.
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
    if not args.resume and not artifact.is_file():
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
    *,
    budget: Budget,
    downgrades: list[str],
    cross: "CrossexamOutcome | None",
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
    if budget.exhausted_by:
        reason = f"{BUDGET_EXHAUSTED}: {budget.exhausted_by}"
        if reason not in downgrades:
            downgrades.append(reason)
    if cross is not None:
        meta["claim_states"] = cross.states
        meta["amendment_notes"] = cross.notes
        meta["incomplete"] = cross.incomplete
    return meta


def _finished_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def finish_run(
    args: argparse.Namespace,
    store: RunStore,
    base_meta: dict[str, Any],
    cross: "CrossexamOutcome | None",
    abort_signum: int | None,
    any_success: bool,
    succeeded_friends: int | None,
    successful_friend_ids: list[str],
    iterations_run: int,
    streak: int,
    downgrades: list[str],
    budget: Budget,
    rounds_reached: int,
    tracker: RepeatTracker,
    loop_converged: bool,
    loop_exhausted: bool,
    active_elapsed_s: float,
    auth_abort: str | None = None,
    runtime_error: str | None = None,
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
        budget=budget,
        downgrades=downgrades,
        cross=cross,
    )
    meta["successful_friend_ids"] = list(successful_friend_ids)
    meta["succeeded_friends"] = len(successful_friend_ids)
    meta["required_friends"] = args.require_friends
    meta["active_elapsed_s"] = active_elapsed_s
    unresolved = bool(
        cross is not None
        and (
            cross.incomplete or any(state not in TERMINAL_STATES for state in cross.states.values())
        )
    )
    quorum_failed = bool(
        args.require_friends is not None
        and succeeded_friends is not None
        and succeeded_friends < args.require_friends
    )
    finished_at = _finished_at()
    started_at = str(meta.get("started_at", finished_at))
    outcome: RunOutcome = terminal_outcome(
        mode=args.mode,
        converged=(
            loop_converged
            if args.mode == "loop"
            else any_success
            and not unresolved
            and auth_abort is None
            and abort_signum is None
            and budget.exhausted_by is None
            and runtime_error is None
        ),
        loop_exhausted=loop_exhausted,
        budget_reason=budget.exhausted_by,
        blocking_ids=[claim.id for claim in blocking],
        any_success=any_success,
        unresolved=unresolved,
        auth_abort=auth_abort is not None,
        abort_signum=abort_signum,
        runtime_error=runtime_error is not None,
        quorum_failed=quorum_failed,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=active_elapsed_s,
        attempted_calls=budget.calls,
        spent_calls=budget.calls,
        iterations_run=iterations_run,
        rounds_run=max(rounds_reached, cross.rounds_run if cross is not None else 0),
        dry_streak=streak,
        repeat_tracker=tracker.snapshot(),
    )
    # Finalization adds fields after the fresh base was bounded. Refit both
    # before RunOutcome validates its input and after it adds terminal fields.
    meta = bounded_theme_metadata(meta)
    meta = bounded_theme_metadata(outcome.apply(meta))
    report = render(
        review,
        meta,
        states=cross.states if cross else None,
    )
    store.write_terminal_artifacts(meta, report)
    if args.json:
        # The path is still what a shell pipeline wants; --json is for a
        # caller that would otherwise have to read run.json itself.
        print(json.dumps(meta, indent=2, sort_keys=True))
    else:
        print(store.run_dir)

    detail = runtime_error or auth_abort
    if quorum_failed and outcome.exit_code == 12:
        detail = (
            f"only {succeeded_friends} of {args.require_friends} required friends "
            "produced a usable answer"
        )
    return decide_exit(outcome, detail=detail)

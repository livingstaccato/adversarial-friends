"""Restore a halted run's frozen invocation and checkpoint state."""

import argparse
from pathlib import Path

from ..adapters import FriendSpec, validate_roster_entry_uniqueness
from ..authority import AuthorityPolicy
from ..errors import UsageError
from ..jsonio import load_json_object
from ..readiness import can_be_host_provider
from ..runstore import default_root
from ..themes import ThemeProposal
from . import resumevalidation
from .checkpoint import (
    any_friend_succeeded,
    normalize_repeat_tracker,
    validate_lifecycle_and_snapshot,
)
from .legacyroles import reduce_legacy_host_checkpoint
from .runmeta import (
    _RESUMABLE_ARGS,
    _SECURITY_GRANTS,
    JUDGING_MODES,
    _frozen_host_context,
    _normalize_saved_grants,
    _normalized_checkpoint,
    _validate_saved_grant,
    _validate_saved_setting,
    _validated_roster_entries,
    migrate_meta,
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


def restore_args(args: argparse.Namespace) -> argparse.Namespace:
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
    resume_authority_policy = AuthorityPolicy.deny_all()
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
            saved_grants = _normalize_saved_grants(saved_value)
            current_grants = _normalize_saved_grants(current_value)
            if current_grants != saved_grants:
                raise UsageError(
                    f"cannot resume: prior --{name.replace('_', '-')} authority must be "
                    "repeated exactly on the resume command line"
                )
            try:
                resume_authority_policy = AuthorityPolicy(tuple(current_grants))
            except UsageError as exc:
                raise UsageError(f"cannot resume: {exc}") from exc
            continue
        if current_value != saved_value:
            raise UsageError(
                f"cannot resume: prior --{name.replace('_', '-')} authority must be "
                "repeated exactly on the resume command line"
            )
    saved_external_grants = _normalize_saved_grants(saved.get("allow_external_tools", []))
    audit_external_grants = _normalize_saved_grants(meta.get("external_tool_grants", []))
    if audit_external_grants != saved_external_grants:
        raise UsageError("cannot resume: external_tool_grants disagrees with the saved invocation")
    host_context_known, detected_host, effective_include_self = _frozen_host_context(meta, saved)
    raw_roster = meta.get("roster", [])
    legacy_host_role_migration = (
        host_context_known
        and detected_host is not None
        and isinstance(raw_roster, list)
        and any(
            isinstance(entry, dict)
            and entry.get("cli") == detected_host
            and ("independent" not in entry or "host_self_review" not in entry)
            for entry in raw_roster
        )
    )
    roster_entries = _validated_roster_entries(
        raw_roster,
        detected_host=detected_host,
        host_context_known=host_context_known,
    )
    validate_roster_entry_uniqueness(
        roster_entries, judging=saved.get("mode", "report") != "report"
    )
    ambiguous_host_entries = (
        not host_context_known
        and isinstance(raw_roster, list)
        and any(
            isinstance(entry, dict)
            and can_be_host_provider(entry.get("cli"))
            and ("independent" not in entry or "host_self_review" not in entry)
            for entry in raw_roster
        )
    )
    if saved.get("mode", "report") in JUDGING_MODES and ambiguous_host_entries:
        raise UsageError(
            "cannot resume judging: this run predates frozen host-role metadata, "
            "so an advisory host could be mistaken for an independent judge. "
            "rerun the review with the current afriend version."
        )
    roster_roles = {
        entry["name"]: (entry["independent"], entry["host_self_review"]) for entry in roster_entries
    }
    meta = _normalized_checkpoint(
        meta,
        roster_names={entry["name"] for entry in roster_entries},
        roster_roles=roster_roles,
        max_calls=saved.get("max_calls"),
        max_rounds=saved.get("max_rounds", 1),
        require_friends=saved.get("require_friends"),
    )
    # The normalized metadata is the sole input to carried_outcome and the
    # resumed report. Keeping only _resume_roster normalized would let those
    # readers reconstruct omitted legacy fields with FriendSpec's independent
    # default and silently restore the host's judging authority.
    meta["roster"] = [dict(entry) for entry in roster_entries]
    if saved.get("mode", "report") in JUDGING_MODES and legacy_host_role_migration:
        meta = reduce_legacy_host_checkpoint(meta, roster_entries, run_dir)
    validate_lifecycle_and_snapshot(meta, run_dir=run_dir, legacy=raw_version == 1)
    if host_context_known:
        meta["detected_host"] = detected_host
        meta["effective_include_self"] = effective_include_self
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
    restored._resume_authority_policy = resume_authority_policy
    # Resume the loop iteration that halted instead of restarting at one.
    restored._resume_iteration = meta["resume_iteration"]
    restored._resume_streak = meta["dry_streak"]
    restored._resume_attempted_calls = meta["attempted_calls"]
    restored._resume_spent_calls = meta["spent_calls"]
    restored._resume_iterations_run = meta["iterations_run"]
    restored._resume_rounds_run = meta["rounds_run"]
    restored._resume_active_elapsed_s = meta["active_elapsed_s"]
    restored._resume_successful_friend_ids = meta["successful_friend_ids"]
    restored._resume_any_success = any_friend_succeeded(meta["friends"])
    restored._resume_theme_proposals = [
        ThemeProposal.from_dict(value) for value in meta["theme_proposals"]
    ]
    restored._resume_produced_new_themes = meta["produced_new_themes"]
    restored._resume_downgrades = list(meta.get("downgrades", []))
    return restored

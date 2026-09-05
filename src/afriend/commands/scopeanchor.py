"""Repository scope identity and resume anchoring for a run."""

from pathlib import Path
from typing import Any

from ..errors import UsageError
from ..events import read_first_event
from ..runstore import RunStore
from ..secureio import secure_regular_exists
from ..snapshots import (
    EXPLICIT_REPOSITORY_SCOPE_AUDIT,
    SnapshotIdentity,
    repository_scope_mode as saved_repository_scope_mode,
)
from .environment import resolve_run_repo


def _validate_repository_scope_anchor(store: RunStore, saved_mode: str | None) -> None:
    events_path = store.events_path()
    try:
        events_exist = secure_regular_exists(events_path, root=store.root)
    except OSError as exc:
        raise UsageError(f"cannot resume: cannot inspect lifecycle events: {exc}") from exc
    if not events_exist:
        if saved_mode is None:
            return
        raise UsageError(
            "cannot resume: declared repository_scope_mode has no lifecycle event anchor"
        )
    try:
        started = read_first_event(events_path, root=store.root)
    except UsageError as exc:
        raise UsageError(f"cannot resume: {exc}") from exc
    if started.type != "run_started":
        raise UsageError(
            "cannot resume: first lifecycle event must be run_started for a declared "
            "repository_scope_mode"
        )
    if started.run_id != store.run_id:
        raise UsageError("cannot resume: first lifecycle event run_id does not match the run")
    anchored_mode = started.payload.get("repository_scope_mode")
    if anchored_mode is None:
        if saved_mode is None:
            return
        raise UsageError(
            "cannot resume: original run_started event has no repository_scope_mode anchor"
        )
    if anchored_mode != saved_mode:
        raise UsageError(
            "cannot resume: saved repository_scope_mode disagrees with the original "
            "run_started event"
        )


def resolve_repository_scope(
    resume_meta: dict[str, Any] | None, artifact: Path, explicit_repo: str | None
) -> tuple[Path | None, bool, str | None, str | None]:
    """Resolve repository scope and its persisted audit fields for a run."""
    if resume_meta is not None:
        repo_root = SnapshotIdentity.from_meta(resume_meta).repo_root
        repository_scope_mode = saved_repository_scope_mode(resume_meta)
        explicit = repository_scope_mode == "explicit"
    else:
        repo_root, explicit = resolve_run_repo(artifact, explicit_repo)
        repository_scope_mode = "explicit" if explicit else "automatic"
    audit = EXPLICIT_REPOSITORY_SCOPE_AUDIT if explicit else None
    return repo_root, explicit, repository_scope_mode, audit

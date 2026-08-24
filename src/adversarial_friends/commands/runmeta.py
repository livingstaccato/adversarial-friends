"""run.json's shape, and rebuilding a halted run's configuration from it.

Split out of commands/run.py when --resume arrived: cmd_run crossed this
repo's 500-line cap, and the metadata contract is a separate concern from
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
from typing import Any

from ..adapters import FriendSpec
from ..errors import UsageError
from ..runstore import default_root

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
    "allow_unsandboxed_friend",
    "max_rounds",
    "max_calls",
    "max_wall_clock",
    "max_loop_iterations",
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
            **{name: getattr(args, name) for name in _RESUMABLE_ARGS},
        },
        "roster": [dataclasses.asdict(s) for s in specs],
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
    restored.out = str(run_dir.parent)
    restored._resume_dir = run_dir
    restored._resume_meta = meta
    return restored

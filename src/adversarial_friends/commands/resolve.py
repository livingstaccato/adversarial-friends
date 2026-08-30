"""`afriend resolve`: attest that a gate-blocking claim has been dealt with.

Spec §7.5. Appends one Resolution to a finished run's ledger and re-reports
the gate, so the workflow is a loop the shell can drive:

    afriend run spec.md --mode gate            # exit 1, names what blocks
    afriend resolve <run-id> --claim c-0001@1 \\
        --disposition fixed --evidence src/auth.py:38
                                               # exit 1, one fewer blocking
    ...                                        # exit 0 once nothing blocks

**It never edits an artifact**, and it does not pretend to verify that a
defect is gone. What it verifies is narrower and honest: whether the location
the author named actually changed since the run started (§6.4). See
resolutions.py for why a whole-artifact hash would be worthless here, and
why an unverifiable location is a real answer rather than a rejection.
"""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from ..errors import UsageError
from ..ids import parse_claim_id
from ..ledger import Ledger, Resolution
from ..resolutions import (
    UNVERIFIABLE,
    parse_location,
    rejection_reason,
    verify_location,
)
from ..reviewstate import ReviewState
from ..runstore import default_root


def _find_run(run_id: str, out: str | None) -> Path:
    """Accept either a run id or a path to a run directory.

    A path is what `afriend run` actually prints, so pasting its output
    straight back in has to work; the bare id is what §7.5's usage line
    shows.
    """
    candidate = Path(run_id)
    if candidate.is_dir():
        return candidate
    root = Path(out) if out else default_root()
    resolved = root / run_id
    if not resolved.is_dir():
        raise UsageError(
            f"no such run: {run_id!r} (looked in {root}). Pass the run "
            "directory path that `afriend run` printed, or --out if the run "
            "was written somewhere else."
        )
    return resolved


def _load_meta(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.is_file():
        raise UsageError(f"{run_dir} is not a run directory: no run.json")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def cmd_resolve(args: argparse.Namespace) -> int:
    run_dir = _find_run(args.run_id, args.out)
    meta = _load_meta(run_dir)
    ledger = Ledger(run_dir / "claims.jsonl")
    review = ReviewState.replay(ledger.records())

    parse_claim_id(args.claim)  # rejects a malformed id with a usage error
    claims = review.claims
    by_id = {c.id: c for c in claims}
    if args.claim not in by_id:
        raise UsageError(
            f"run {run_dir.name} has no claim {args.claim!r}. "
            f"Known: {', '.join(sorted(by_id)) or 'none'}"
        )

    location = parse_location(args.evidence)
    if location is None:
        # §6.4: evidence must name a location. Prose alone leaves nothing to
        # verify, and recording it would make every resolution look equally
        # well-supported.
        raise UsageError(
            f"--evidence must name a location (e.g. src/auth.py:38), got "
            f"{args.evidence!r}. §6.4 requires one: a resolution with no "
            "location is an assertion nothing can check."
        )

    repo_root = Path(meta["repo_root"]) if meta.get("repo_root") else None
    frozen_dir = run_dir / "artifact"
    frozen = next(iter(frozen_dir.iterdir()), None) if frozen_dir.is_dir() else None
    artifact_path = Path(meta["artifact_path"]) if meta.get("artifact_path") else None
    if artifact_path is None:
        old = Path((meta.get("invocation") or {}).get("artifact") or "")
        if old.is_absolute():
            artifact_path = old
        elif repo_root is not None and old:
            candidate = repo_root / old
            if candidate.is_file():
                artifact_path = candidate
    verified = verify_location(
        location,
        repo_root,
        meta.get("snapshot_sha"),
        frozen_artifact=frozen,
        artifact_path=artifact_path,
    )

    refusal = rejection_reason(args.disposition, verified)
    if refusal:
        raise UsageError(refusal)

    resolution = Resolution(
        claim_id=args.claim,
        disposition=args.disposition,
        author=args.author or os.environ.get("USER") or "unknown",
        evidence=args.evidence,
        round=int(meta.get("rounds_run", 1)),
        verified=verified,
    )
    ledger.append(resolution)
    review.apply(resolution)

    if verified == UNVERIFIABLE:
        # Recorded, not refused -- but the operator should know the runner
        # checked nothing, rather than reading silence as confirmation.
        print(
            f"afriend: recorded, but {location.path} could not be reconstructed "
            "from this run; the resolution is an attestation only.",
            file=sys.stderr,
        )

    states = meta.get("claim_states") or {}
    blocking = review.blocking(states)

    print(f"{resolution.claim_id} {args.disposition} ({verified})")
    if blocking:
        print(
            f"afriend: gate blocked -- {len(blocking)} claim(s) still need a "
            "resolution: " + ", ".join(c.id for c in blocking),
            file=sys.stderr,
        )
        return 1
    print("gate clear")
    return 0

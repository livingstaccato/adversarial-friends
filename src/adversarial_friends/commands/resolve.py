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
why unverifiable evidence can record risk or rejection but cannot support
`fixed`.
"""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from ..errors import UsageError
from ..ids import parse_claim_id
from ..jsonio import load_json_object
from ..ledger import MAX_LEDGER_BYTES, Claim, Ledger, Record, Resolution, record_from_dict
from ..outcomes import json_node_count
from ..resolutions import (
    UNVERIFIABLE,
    parse_location,
    rejection_reason,
    resolve_form_error,
    verify_location,
)
from ..reviewstate import ReviewState
from ..runstore import default_root
from ..secureio import secure_read_bytes

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_EVIDENCE_REQUIREMENT = "--disposition fixed|rejected|accepted-risk --evidence PATH[:LINE]"


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
    return load_json_object(path, label="saved run metadata")


def _claim_states(meta: dict[str, Any]) -> dict[str, str]:
    """Read persisted claim states while accepting state-less legacy runs.

    Before claim-state metadata existed, resolve treated every unresolved
    non-advisory claim conservatively.  Keeping that behavior makes those
    runs inspectable; a present but malformed state map is not legacy data
    and must not be guessed at.
    """
    raw = meta.get("claim_states")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(claim_id, str) and isinstance(state, str) for claim_id, state in raw.items()
    ):
        raise UsageError(
            "saved run metadata has malformed claim_states; expected string keys and values"
        )
    return raw


def _unresolved_claims(review: ReviewState, meta: dict[str, Any]) -> list[Claim]:
    states = _claim_states(meta)
    return sorted(
        review.blocking(states),
        key=lambda claim: (_SEVERITY_ORDER.get(claim.severity, len(_SEVERITY_ORDER)), claim.id),
    )


def _read_discovery_records(path: Path, *, run_dir: Path) -> list[Record]:
    """Read the ledger without writer initialization or permission changes."""
    try:
        payload = secure_read_bytes(path, root=run_dir, max_bytes=MAX_LEDGER_BYTES)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UsageError(f"cannot read ledger {path}: {exc}") from exc
    records: list[Record] = []
    for line_no, raw in enumerate(payload.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            decoded = raw.decode("utf-8")
            parsed = json.loads(decoded)
            json_node_count(parsed, f"ledger record {line_no}")
            records.append(record_from_dict(parsed))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise UsageError(f"{path}:{line_no}: invalid ledger record: {exc}") from exc
        except UsageError as exc:
            raise UsageError(f"{path}:{line_no}: {exc}") from exc
    return records


def _location(claim: Claim) -> str:
    """Render the durable finding location, without inferring a new one."""
    return claim.location or "not recorded"


def _render_claim(claim: Claim) -> list[str]:
    return [
        f"{claim.id} [{claim.severity}] {claim.claim}",
        f"  location: {_location(claim)}",
        f"  evidence: {claim.evidence}",
        f"  resolution requires: {_EVIDENCE_REQUIREMENT}",
    ]


def _write_command(run_dir: Path, claim: Claim) -> str:
    return (
        f"afriend resolve {run_dir} --claim {claim.id} "
        f"--disposition <fixed|rejected|accepted-risk> --evidence PATH[:LINE]"
    )


def _cmd_discovery(args: argparse.Namespace, run_dir: Path, meta: dict[str, Any]) -> int:
    """Render read-only unresolved-claim discovery from the durable ledger."""
    review = ReviewState.replay(_read_discovery_records(run_dir / "claims.jsonl", run_dir=run_dir))
    claims = _unresolved_claims(review, meta)
    if not claims:
        print("No unresolved claims. No resolution action is needed.")
        return 0

    if getattr(args, "list", False):
        for index, claim in enumerate(claims):
            if index:
                print()
            print("\n".join(_render_claim(claim)))
        print()
        print(f"next: afriend resolve {run_dir} --next")
        return 0

    highest_priority = claims[0].severity
    candidates = [claim for claim in claims if claim.severity == highest_priority]
    if len(candidates) != 1:
        choices = ", ".join(claim.id for claim in candidates)
        raise UsageError(
            f"multiple {highest_priority} unresolved claims are equally highest priority; "
            f"choose --claim explicitly: {choices}"
        )
    claim = candidates[0]
    print("\n".join(_render_claim(claim)))
    print(f"next: {_write_command(run_dir, claim)}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    run_dir = _find_run(args.run_id, args.out)
    meta = _load_meta(run_dir)
    form_error = resolve_form_error(
        discovery=bool(getattr(args, "list", False) or getattr(args, "next", False)),
        claim=getattr(args, "claim", None),
        disposition=getattr(args, "disposition", None),
        evidence=getattr(args, "evidence", None),
        author=getattr(args, "author", None),
    )
    if form_error is not None:
        raise UsageError(form_error)
    if getattr(args, "list", False) or getattr(args, "next", False):
        return _cmd_discovery(args, run_dir, meta)

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
        # A fixed disposition was refused above. For accepted risk or a
        # rejected claim, record the attestation but say that nothing was
        # independently checked.
        print(
            f"afriend: recorded, but {location.path} could not be reconstructed "
            "from this run; the resolution is an attestation only.",
            file=sys.stderr,
        )

    states = _claim_states(meta)
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

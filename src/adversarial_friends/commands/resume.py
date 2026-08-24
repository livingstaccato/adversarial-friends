"""Continuing a run that halted for the orchestrator -- §4.2.

Round 1 already ran, in the process that exited 10. Its claims are
reconstructed from the ledger rather than re-dispatched: re-running the
critique would spend a full fan-out and produce *different* claims than the
ones the orchestrator just adjudicated, so the adjudication would apply to
ids that no longer exist.

Reconstruction is not a plain read. The ledger deliberately keeps aliased
duplicates as claim records, and every record's `origin` is frozen as first
written -- see merge.canonical_claims for why, and for how corroboration is
folded back in.
"""

import argparse
from collections.abc import Callable
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any

from ..adapters import Adapter, FriendSpec
from ..ceilings import Budget
from ..ledger import Alias, Claim
from ..merge import canonical_claims
from ..orchestrator import apply_merges, read_response
from ..runstore import RunStore
from ..verdictschema import schema_path as verdict_schema_path
from .crossexam import CrossexamOutcome, run_rounds

JUDGING_MODES = frozenset({"crossexam", "gate", "loop"})


@dataclass
class ResumedRun:
    claims: list[Claim] = field(default_factory=list)
    aliases: list[Alias] = field(default_factory=list)
    friends_meta: list[dict[str, Any]] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    cross: CrossexamOutcome | None = None


def resume_round_one(
    args: argparse.Namespace,
    store: RunStore,
    specs: list[FriendSpec],
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    artifact: Path,
    artifact_text: str,
    repo_root: Path | None,
    snapshot_sha: str | None,
    abort_event: threading.Event,
    budget: Budget,
    base_round: int,
    on_pool: Callable[[concurrent.futures.ThreadPoolExecutor | None], None],
) -> ResumedRun:
    """Apply the orchestrator's merges, then carry on into judging."""
    resumed = ResumedRun()
    claims = canonical_claims(list(store.ledger.records()))
    decisions = read_response(store.round_dir(base_round), {c.id for c in claims})
    claims, adjudicated = apply_merges(claims, decisions, base_round)
    for alias in adjudicated:
        store.ledger.append(alias)
    resumed.claims = claims
    resumed.aliases = adjudicated
    resumed.friends_meta = list(getattr(args, "_resume_meta", {}).get("friends", []))
    resumed.downgrades.append(
        f"resumed from {store.run_id} after orchestrator merge adjudication: "
        f"{len(adjudicated)} merge(s) applied."
    )
    # The halted process already paid for round 1; charge it here so a
    # resumed run cannot spend the whole budget a second time.
    budget.spend(len(specs))

    if args.mode not in JUDGING_MODES:
        return resumed

    resumed.cross = run_rounds(
        specs,
        claims,
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
        on_pool=on_pool,
        first_round=base_round + 1,
        allow_unsandboxed=args.allow_unsandboxed_friend,
    )
    resumed.claims = resumed.cross.claims
    resumed.friends_meta.extend(resumed.cross.friends_meta)
    resumed.downgrades.extend(resumed.cross.downgrades)
    return resumed

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
import json
from pathlib import Path
import threading
from typing import Any

from .. import verdicts as vd
from ..adapters import Adapter, FriendSpec
from ..ceilings import Budget
from ..ids import format_claim_id
from ..ledger import Alias, Claim, Verdict
from ..merge import canonical_claims
from ..orchestrator import (
    QUESTION_EXTRACT,
    apply_merges,
    read_extract_response,
    read_response,
    request_path,
)
from ..report import render
from ..runstore import RunStore
from ..verdictschema import schema_path as verdict_schema_path
from .crossexam import CrossexamOutcome, run_rounds
from .runmeta import JUDGING_MODES


def _question_asked(round_dir: Path) -> str:
    """Which question this round's REQUEST.json posed.

    Read from the request rather than inferred from the response: the
    response is the file a human just edited, and guessing its shape would
    turn a typo into a confusing schema error rather than a clear one.
    """
    path = request_path(round_dir)
    if not path.is_file():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("question", ""))
    except json.JSONDecodeError:
        return ""


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
    round_dir = store.round_dir(base_round)

    # The same handshake serves two questions (§4.2, §14.2), so the answer is
    # read according to what was actually asked rather than assumed.
    question = _question_asked(round_dir)
    adjudicated: list[Alias] = []
    if question == QUESTION_EXTRACT:
        extracted = read_extract_response(round_dir)
        counter = len(claims)
        for finding in extracted:
            counter += 1
            claims.append(
                Claim(
                    id=format_claim_id(counter),
                    supersedes=None,
                    # The friend that produced the unparseable output keeps
                    # authorship: an orchestrator read its words, it did not
                    # invent them, and judging is decided by origin (§7.1).
                    origin=[finding.get("friend", "orchestrator")],
                    lens="extracted",
                    round=base_round,
                    advisory=False,
                    severity=finding["severity"],
                    claim=finding["claim"],
                    location=finding.get("location"),
                    evidence=finding["evidence"],
                    failure_scenario=finding["failure_scenario"],
                    suggested_fix=finding["suggested_fix"],
                )
            )
            store.ledger.append(claims[-1])
        resumed.downgrades.append(
            f"resumed from {store.run_id} after claim extraction: "
            f"{len(extracted)} claim(s) read out of unparseable output by hand."
        )
    else:
        decisions = read_response(round_dir, {c.id for c in claims})
        claims, adjudicated = apply_merges(claims, decisions, base_round)
        for alias in adjudicated:
            store.ledger.append(alias)
        resumed.downgrades.append(
            f"resumed from {store.run_id} after orchestrator merge adjudication: "
            f"{len(adjudicated)} merge(s) applied."
        )
    resumed.claims = claims
    resumed.aliases = adjudicated
    resumed.friends_meta = list(getattr(args, "_resume_meta", {}).get("friends", []))
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


def carried_outcome(store: RunStore, meta: dict[str, Any]) -> "CrossexamOutcome | None":
    """The previous iteration's outcome, rebuilt from what the run recorded.

    A `loop` iteration inherits states, verdicts, notes and discard
    signatures (§7.3). None of that survives in memory across an
    orchestrator halt, and all of it survives on disk: states and notes in
    `run.json`, verdicts in the ledger, and signatures are a pure function
    of the verdicts. Rebuilt rather than re-derived by re-judging, which
    would spend a fan-out to recompute something already written down.

    Returns None when there is nothing to carry -- a halt in iteration 1,
    before any judging round ran.
    """
    states = meta.get("claim_states") or {}
    if not states:
        return None
    outcome = CrossexamOutcome()
    outcome.states = dict(states)
    outcome.notes = list(meta.get("amendment_notes") or [])
    outcome.incomplete = bool(meta.get("incomplete"))
    outcome.verdicts = [r for r in store.ledger.records() if isinstance(r, Verdict)]
    outcome.signatures = {
        claim_id: vd.verdict_set_signature(outcome.verdicts, claim_id)
        for claim_id, state in outcome.states.items()
        if state == vd.UNPROVEN
    }
    return outcome


def loop_position(
    args: argparse.Namespace, store: RunStore, resuming: bool
) -> tuple[int, int, "CrossexamOutcome | None"]:
    """Where a resumed `loop` re-enters: iteration, dry-round streak, and
    what that iteration inherits.

    (1, 0, None) for a fresh run and for any mode that does not loop. An
    orchestrator halt happens once per iteration, so a resumed loop that
    started over at iteration 1 would repeat work already adjudicated, and
    one that treated itself as finished would silently drop the iterations
    the operator asked for.
    """
    if not resuming or args.mode != "loop":
        return 1, 0, None
    meta = getattr(args, "_resume_meta", {}) or {}
    return (
        int(getattr(args, "_resume_iteration", 1) or 1),
        int(getattr(args, "_resume_streak", 0) or 0),
        carried_outcome(store, meta),
    )


def write_halt(
    args: argparse.Namespace,
    store: RunStore,
    meta: dict[str, Any],
    claims: list[Claim],
    aliases: list[Alias],
    iteration: int,
    streak: int,
    carry_over: "CrossexamOutcome | None",
) -> None:
    """Leave behind a run directory a resume can actually continue from.

    A halt in a `loop` must record everything the resumed iteration
    inherits, or it re-enters knowing only that it was interrupted: which
    iteration it was in, the dry-round streak, and whatever earlier
    iterations already decided. The completion path writes these; the halt
    path did not, which is what made `--merge orchestrator` unusable with
    `--mode loop` and is why that combination was refused rather than
    supported.
    """
    if args.mode == "loop":
        meta["iterations_run"] = iteration
        meta["dry_streak"] = streak
    if carry_over is not None:
        meta["claim_states"] = carry_over.states
        meta["amendment_notes"] = carry_over.notes
        meta["incomplete"] = carry_over.incomplete
    store.write_run_json(meta)
    store.write_report(render(claims, aliases, meta))
    print(store.run_dir)

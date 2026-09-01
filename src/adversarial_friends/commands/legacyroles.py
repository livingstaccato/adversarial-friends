"""Rebuild legacy checkpoint facts after host roles become trustworthy.

Old checkpoints counted every selected CLI as an independent reviewer.  Once
the frozen host identifies one of those rows as self-review, derived state
from that checkpoint is no longer trustworthy.  The append-only ledger and
friend audit are the durable facts; reduce those again instead of carrying
the old authority decision forward.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import verdicts as vd
from ..adapters import FriendSpec, friend_key, independent_friend_keys
from ..errors import UsageError
from ..ledger import Claim, Ledger, Verdict
from ..reviewstate import ReviewState
from ..themes import ThemeProposal


def _cannot_replay(detail: str) -> UsageError:
    return UsageError(
        "cannot resume legacy judging: durable ledger and friend audit data "
        f"are insufficient to recompute independent host roles ({detail}); "
        "rerun the review with the current afriend version."
    )


def _success(status: str) -> bool:
    return status in {"ok", "ok [orphans suspected]"} or status.startswith("ok (diagnostics: ")


def _audit_index(rows: Iterable[dict[str, Any]]) -> dict[tuple[int, str], str]:
    indexed: dict[tuple[int, str], str] = {}
    for row in rows:
        identity = int(row["round"]), str(row["name"])
        status = str(row["status"])
        prior = indexed.get(identity)
        if prior is not None and prior != status:
            raise _cannot_replay(
                f"conflicting audit rows for {identity[1]!r} in round {identity[0]}"
            )
        indexed[identity] = status
    return indexed


def _verdict_index(verdicts: Iterable[Verdict]) -> dict[tuple[str, str, int], Verdict]:
    indexed: dict[tuple[str, str, int], Verdict] = {}
    for verdict in verdicts:
        identity = verdict.claim_id, verdict.judge, verdict.round
        prior = indexed.get(identity)
        if prior is not None and prior != verdict:
            raise _cannot_replay(
                f"conflicting verdicts for {verdict.claim_id!r} from {verdict.judge!r}"
            )
        indexed[identity] = verdict
    return indexed


def _block_end(round_no: int, rounds_per_iteration: int) -> int:
    return ((round_no - 1) // rounds_per_iteration + 1) * rounds_per_iteration


def _active_specs(
    specs: list[FriendSpec], audit: dict[tuple[int, str], str], round_no: int
) -> list[FriendSpec]:
    dropped = {
        name
        for (seen_round, name), status in audit.items()
        if seen_round <= round_no and status.startswith("skipped: ")
    }
    return [spec for spec in specs if spec.independent and spec.name not in dropped]


def _round_verdicts(
    verdicts: Iterable[Verdict], independent_judges: set[str], round_no: int
) -> list[Verdict]:
    return [
        vd.downgrade_unverifiable(verdict)
        for verdict in verdicts
        if verdict.judge in independent_judges and verdict.round <= round_no
    ]


def _missing_judges(
    claim: Claim,
    specs: list[FriendSpec],
    audit: dict[tuple[int, str], str],
    verdicts: dict[tuple[str, str, int], Verdict],
    round_no: int,
) -> set[str]:
    missing: set[str] = set()
    for spec in specs:
        judge = friend_key(spec)
        if judge in claim.origin:
            continue
        if (claim.id, judge, round_no) in verdicts:
            continue
        status = audit.get((round_no, spec.name))
        if status is None:
            raise _cannot_replay(f"no verdict or audit row for {spec.name!r} in round {round_no}")
        # A successful batch with no verdict is an omitted verdict and is
        # missing just like a failed batch.  A skipped friend was removed by
        # _active_specs and cannot reach this branch.
        missing.add(judge)
    return missing


def _reduced_claim_state(
    review: ReviewState,
    meta: dict[str, Any],
    specs: list[FriendSpec],
    audit: dict[tuple[int, str], str],
) -> tuple[dict[str, str], bool]:
    saved = meta.get("claim_states") or {}
    if not saved:
        return {}, False
    claims = {claim.id: claim for claim in review.claims}
    if any(claim_id not in claims for claim_id in saved):
        raise _cannot_replay("a saved claim state has no canonical ledger claim")

    rounds_per_iteration = int(meta["invocation"]["max_rounds"])
    critique_rounds = set(range(1, int(meta["rounds_run"]) + 1, rounds_per_iteration))
    judging_rounds = sorted(
        {
            verdict.round
            for verdict in review.verdicts
            if verdict.round <= int(meta["rounds_run"]) and verdict.round not in critique_rounds
        }
        | {
            round_no
            for round_no, _name in audit
            if round_no <= int(meta["rounds_run"]) and round_no not in critique_rounds
        }
    )
    if not judging_rounds:
        raise _cannot_replay("saved claim states have no durable judging round")

    verdict_index = _verdict_index(review.verdicts)
    states = {claim_id: vd.CONTESTED for claim_id in saved}
    signatures: dict[str, vd._Signature] = {}
    incomplete = False
    for round_no in judging_rounds:
        active_specs = _active_specs(specs, audit, round_no)
        roster = independent_friend_keys(active_specs)
        independent_judges = set(roster)
        cast = _round_verdicts(review.verdicts, independent_judges, round_no)
        for claim_id, claim in claims.items():
            if claim_id not in states or claim.round >= round_no:
                continue
            if states[claim_id] in vd.TERMINAL_STATES:
                continue
            missing = _missing_judges(claim, active_specs, audit, verdict_index, round_no)
            incomplete = incomplete or bool(missing)
            state = vd.state_for(
                claim,
                cast,
                roster,
                round_no,
                _block_end(round_no, rounds_per_iteration),
                required_missing=bool(missing),
            )
            if state == vd.UNPROVEN:
                signature = vd.verdict_set_signature(
                    (verdict for verdict in cast if verdict.judge in independent_judges),
                    claim.id,
                )
                if vd.should_discard(signatures.get(claim.id), signature):
                    state = vd.DISCARDED
                signatures[claim.id] = signature
            else:
                signatures.pop(claim.id, None)
            states[claim_id] = state
    return states, incomplete


def _round_novelty(
    review: ReviewState,
    proposals: list[ThemeProposal],
    independent_judges: set[str],
    round_no: int,
) -> bool:
    duplicates = {alias.duplicate for alias in review.aliases}
    duplicates.update(proposal.duplicate for proposal in proposals)
    return any(
        claim.round == round_no
        and claim.id not in duplicates
        and bool(set(claim.origin) & independent_judges)
        for claim in review.claims_by_id.values()
    )


def _round_health(
    specs: list[FriendSpec],
    audit: dict[tuple[int, str], str],
    round_no: int,
) -> tuple[bool, bool]:
    statuses: list[str] = []
    for spec in specs:
        if not spec.independent:
            continue
        status = audit.get((round_no, spec.name))
        if status is None:
            raise _cannot_replay(
                f"no independent audit row for {spec.name!r} in critique round {round_no}"
            )
        if not status.startswith("skipped: "):
            statuses.append(status)
    any_success = any(_success(status) for status in statuses)
    any_failed = any(status.startswith("failed: ") for status in statuses)
    return any_success, any_failed or not any_success


def _reduced_loop_state(
    review: ReviewState,
    meta: dict[str, Any],
    specs: list[FriendSpec],
    audit: dict[tuple[int, str], str],
) -> tuple[int, bool, bool]:
    rounds_per_iteration = int(meta["invocation"]["max_rounds"])
    pending_round = (int(meta["resume_iteration"]) - 1) * rounds_per_iteration + 1
    proposals = [ThemeProposal.from_dict(value) for value in meta.get("theme_proposals", [])]
    independent_judges = set(independent_friend_keys(specs))
    streak = 0
    for round_no in range(1, pending_round, rounds_per_iteration):
        any_success, failed = _round_health(specs, audit, round_no)
        dry = vd.round_is_dry(
            not _round_novelty(review, proposals, independent_judges, round_no),
            any_success and not failed,
        )
        streak = vd.next_streak(streak, failed=failed, dry=dry)
    any_success, halted_failed = _round_health(specs, audit, pending_round)
    halted_dry = vd.round_is_dry(
        not _round_novelty(review, proposals, independent_judges, pending_round),
        any_success and not halted_failed,
    )
    return streak, halted_dry, halted_failed


def reduce_legacy_host_checkpoint(
    meta: dict[str, Any], roster: list[dict[str, Any]], run_dir: Path
) -> dict[str, Any]:
    """Replace authority-derived legacy fields with a durable replay."""
    normalized = dict(meta)
    specs = [FriendSpec(**entry) for entry in roster]
    audit = _audit_index(meta.get("friends", []))
    ledger = Ledger(run_dir / "claims.jsonl", root=run_dir.parent)
    review = ReviewState.replay(ledger.records())
    states, incomplete = _reduced_claim_state(review, meta, specs, audit)
    if "claim_states" in meta:
        normalized["claim_states"] = states
        normalized["incomplete"] = incomplete
    if meta.get("mode") == "loop":
        streak, halted_dry, halted_failed = _reduced_loop_state(review, meta, specs, audit)
        normalized["dry_streak"] = streak
        normalized["halted_round_dry"] = halted_dry
        normalized["halted_round_failed"] = halted_failed
    return normalized

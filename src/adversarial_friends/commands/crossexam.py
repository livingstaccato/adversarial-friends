"""`afriend run --mode crossexam`: rounds 2..N, friends judging each other.

Round 1 is a critique round and belongs to commands/run.py, which calls
`run_rounds` once it has claims. From here on every round is a judging
round: each friend receives the still-contested claims it did not write
(§7.1), rendered blind (§5.1), and returns one verdict each.

The decisions this file makes are all IO and bookkeeping. Every rule that
decides an outcome -- who may judge, what settles, what deadlocks, what is
discarded, what a late amendment becomes -- lives in verdicts.py as pure
functions, and is tested there without a subprocess in sight.
"""

from collections.abc import Callable
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any

from .. import verdicts as vd
from ..adapters import Adapter, FriendSpec
from ..ceilings import BUDGET_EXHAUSTED, Budget
from ..failures import RepeatTracker
from ..judgeprompt import build_judge_prompt
from ..ledger import Claim, Verdict
from ..rounds import dispatch_round, persist_result
from ..runstore import RunStore
from ..verdictschema import VERDICT_CONTRACT


def friend_key(spec: FriendSpec) -> str:
    """The identity a claim's `origin` records -- see commands/run.py, which
    writes `origin=[f"{spec.cli}/{spec.lens}"]`.

    Judging is decided by matching this against `Claim.origin`, so it must
    stay exactly what round 1 wrote. It is deliberately NOT `spec.name`:
    names carry a positional index (`codex-ops-0`) that the ledger does not,
    and a ledger record has to mean the same thing when read back.
    """
    return f"{spec.cli}/{spec.lens}"


@dataclass
class CrossexamOutcome:
    """Everything rounds 2..N produced, for the report and the exit code."""

    verdicts: list[Verdict] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    friends_meta: list[dict[str, Any]] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rounds_run: int = 1
    ceiling_hit: str | None = None
    incomplete: bool = False


def _duplicate_key_downgrade(specs: list[FriendSpec]) -> str | None:
    """Two friends sharing a (cli, lens) pair share an `origin` value.

    `--friend codex:ops --friend codex:ops` produces two distinct friends
    with one ledger identity, so each is excluded from judging the other's
    claims as though it had written them. That is a real reduction in the
    number of independent judges, and it must be visible rather than
    silently shrinking every claim's judge set.
    """
    seen: set[str] = set()
    collisions: set[str] = set()
    for spec in specs:
        key = friend_key(spec)
        if key in seen:
            collisions.add(key)
        seen.add(key)
    if not collisions:
        return None
    return (
        f"two or more friends share the ledger identity {sorted(collisions)}: "
        "claims from either are treated as written by both, so each is "
        "excluded from judging the other's claims and this run has fewer "
        "independent judges than it has friends."
    )


def _parse_verdicts(payload: dict[str, Any], judge: str, round_no: int) -> list[Verdict]:
    """Turn one judge's validated payload into Verdict records.

    The payload has already passed VERDICT_CONTRACT.validate inside
    normalize, so the required fields are present and well-typed; this only
    shapes them. `judge` comes from the runner, never from the payload -- a
    friend does not get to say who it is.
    """
    out = []
    # See critique.run_critique: nullable container, so `or []`.
    for entry in payload.get("verdicts") or []:
        out.append(
            Verdict(
                claim_id=entry["claim_id"],
                judge=judge,
                round=round_no,
                verdict=entry["verdict"],
                confidence=entry["confidence"],
                evidence_assessment=entry.get("evidence_assessment") or "",
                reasoning=entry["reasoning"],
                counter_evidence=entry.get("counter_evidence"),
                amended_claim=entry.get("amended_claim"),
            )
        )
    return out


def _slice_for(spec: FriendSpec, contested: list[Claim]) -> list[Claim]:
    """The claims this friend may judge: everything contested it did not
    originate (§7.1). An empty slice means this friend wrote every claim
    still open, and it is simply not dispatched this round."""
    key = friend_key(spec)
    return [claim for claim in contested if key not in claim.origin]


def _never_reported(missing: dict[str, set[str]], spec: FriendSpec, claims: list[Claim]) -> None:
    """Record `spec` as a judge that did not report on `claims` this round."""
    for claim in claims:
        missing.setdefault(claim.id, set()).add(friend_key(spec))


def _prior_verdicts_by_claim(
    all_verdicts: list[Verdict], claim_ids: set[str]
) -> dict[str, list[Verdict]]:
    prior: dict[str, list[Verdict]] = {}
    for verdict in all_verdicts:
        if verdict.claim_id in claim_ids:
            prior.setdefault(verdict.claim_id, []).append(verdict)
    return prior


def run_rounds(
    specs: list[FriendSpec],
    claims: list[Claim],
    store: RunStore,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    schema_file: Path,
    artifact: Path,
    artifact_text: str,
    repo_root: Path | None,
    snapshot_sha: str | None,
    abort_event: threading.Event,
    budget: Budget,
    max_rounds: int,
    attributed: bool = False,
    on_pool: Callable[[concurrent.futures.ThreadPoolExecutor | None], None] = lambda _pool: None,
    now: Callable[[], float] = time.monotonic,
    first_round: int = 2,
    allow_unsandboxed: bool = False,
    tracker: RepeatTracker | None = None,
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
) -> CrossexamOutcome:
    """Judge `claims` over rounds `first_round`..`max_rounds`.

    `first_round` is 2 for a plain crossexam -- round 1 is the critique. A
    `loop` iteration passes a higher number: each iteration owns a distinct
    block of round numbers so its rounds never collide with an earlier
    iteration's in the run directory or the ledger.
    """
    outcome = CrossexamOutcome(claims=list(claims))
    duplicate_note = _duplicate_key_downgrade(specs)
    if duplicate_note:
        outcome.downgrades.append(duplicate_note)

    # A claim starts unjudged. `contested` is the non-terminal set, so
    # seeding every claim as contested is what puts it in round 2's slice.
    outcome.states = {claim.id: vd.CONTESTED for claim in outcome.claims}
    signatures: dict[str, vd._Signature] = {}

    round_no = first_round
    while round_no <= max_rounds:
        if abort_event.is_set():
            break
        contested = [
            c for c in outcome.claims if outcome.states.get(c.id) not in vd.TERMINAL_STATES
        ]
        if not contested:
            break

        judge_specs: list[FriendSpec] = []
        prompt_for: dict[str, Path] = {}
        contested_ids = {c.id: c for c in contested}
        prior = _prior_verdicts_by_claim(outcome.verdicts, set(contested_ids))
        for spec in specs:
            slice_ = _slice_for(spec, contested)
            if not slice_:
                continue
            prompt_text, note = build_judge_prompt(spec, artifact_text, slice_, prior, attributed)
            if note:
                outcome.downgrades.append(note)
            path = store.friend_prompt_path(round_no, spec.name)
            path.write_text(prompt_text, encoding="utf-8")
            judge_specs.append(spec)
            prompt_for[spec.name] = path

        if not judge_specs:
            # Every remaining claim was written by every friend. Nothing is
            # left that anyone is independent enough to judge, so further
            # rounds would cost a fan-out and decide nothing.
            outcome.downgrades.append(
                f"round {round_no}: no friend is independent of any remaining "
                "claim, so no judging round could be run."
            )
            # Settle them before leaving. Breaking out directly would leave
            # every remaining claim at its `contested` seed, which reads as
            # "judges disagreed" -- the opposite of what happened, which is
            # that no judge existed. state_for returns `unproven` for a claim
            # with no judges, which is the honest answer.
            _settle_round(outcome, contested, signatures, specs, store, round_no, max_rounds, {})
            break

        if budget.would_exceed_calls(len(judge_specs)):
            budget.exhaust(
                f"--max-calls={budget.max_calls} reached before round {round_no} "
                f"({budget.calls} calls spent, {len(judge_specs)} more required)"
            )
            break
        if budget.out_of_time(now()):
            budget.exhaust(f"--max-wall-clock reached before round {round_no}")
            break

        results = dispatch_round(
            judge_specs,
            round_no,
            prompt_for,
            store,
            registry,
            fake_cmd,
            schema_file,
            artifact,
            repo_root,
            snapshot_sha,
            abort_event,
            on_pool=on_pool,
            contract=VERDICT_CONTRACT,
            allow_unsandboxed=allow_unsandboxed,
            tracker=tracker,
            downgrades=outcome.downgrades,
            extra_args=extra_args,
            pass_env=pass_env,
            keep=keep,
        )
        budget.spend(len(results))
        outcome.rounds_run = round_no

        # A judge the repeat tracker refused to dispatch is a judge that
        # never reported -- §7.2's M12, the same as one that failed. The
        # tracker filters inside dispatch_round, so from here a withheld
        # judge is simply absent from `results`; before this was counted, a
        # round in which EVERY judge was withheld looked like a round in
        # which nothing failed. Below-quorum claims then went `unproven`,
        # and two such rounds -- identical, since nobody spoke -- tripped
        # the discard rule. Seen in a real run: five claims `discarded`,
        # "judges looked twice and could not verify", when no judge had
        # been dispatched at all.
        dispatched = {spec.name for spec, _capability, _result in results}
        withheld = [s for s in judge_specs if s.name not in dispatched]
        # §7.2's M12, per claim: the judges that never reported on it this
        # round. A friend that failed or was withheld is missing from every
        # claim in its slice and from no other; a judge that answered only
        # part of its slice is missing from the rest. `incomplete` is then
        # what a claim reads when one of ITS judges was silent. Until this
        # was per claim, one unrelated friend's failure marked every
        # below-quorum claim in the run `incomplete` and reset its discard
        # signature -- raised by the judges of a real crossexam, reviewing
        # the previous version of this file.
        missing: dict[str, set[str]] = {}
        for spec in withheld:
            _never_reported(missing, spec, _slice_for(spec, contested))
        if not results and withheld and not abort_event.is_set():
            names = ", ".join(s.name for s in withheld)
            outcome.downgrades.append(
                f"round {round_no}: every judge with claims left to judge is "
                f"disabled ({names}); no judging round could be run."
            )
            outcome.incomplete = True
            _settle_round(
                outcome, contested, signatures, specs, store, round_no, max_rounds, missing
            )
            break

        round_verdicts: list[Verdict] = []
        any_failed = bool(withheld)
        for spec, capability, result in results:
            outcome.friends_meta.append(persist_result(store, round_no, spec, capability, result))
            if result.failure_reason is not None:
                # §7.2's M12: a round in which a required friend fails marks
                # the RUN incomplete, regardless of per-claim states.
                any_failed = True
                _never_reported(missing, spec, _slice_for(spec, contested))
                continue
            cast = _parse_verdicts(result.result.payload or {}, friend_key(spec), round_no)
            # Both spec-mandated rewrites, applied before anything counts the
            # verdict: an unverifiable dispositive verdict is not dispositive
            # (§6.5), and a final-round amendment cannot create a successor
            # nobody can judge (§7.2).
            downgraded = [vd.apply_downgrades(v, round_no, max_rounds) for v in cast]
            # §7.2: "the report flags it as a late amendment the operator may
            # want to run again". The rewrite records its proposal in
            # `reasoning`, but a claim that ends settled-upheld never has its
            # reasoning rendered -- so without this the flag existed only in
            # the ledger, which is the one place an operator does not look.
            # `upheld` is what the late-amendment rewrite produces and
            # nothing else does: the evidence rule rewrites `amended` to
            # `unproven`, in any round, and `!= "amended"` could not tell
            # the two apart -- so a judge whose amendment merely cited
            # unverifiable evidence in round 2 was reported as having
            # amended "in the final round", and told to add rounds that
            # would change nothing. Settled-upheld by two judges of a real
            # crossexam.
            for before, after in zip(cast, downgraded, strict=True):
                if before.verdict == "amended" and after.verdict == "upheld":
                    outcome.notes.append(
                        f"{before.claim_id}: {friend_key(spec)} proposed an amendment in "
                        f"the final round, too late to judge a successor, so it was "
                        f"counted as `upheld`. Proposed wording: "
                        f"{before.amended_claim!r}. Re-run with a higher "
                        "--max-rounds to have it judged."
                    )
            cast = downgraded
            # A judge may only rule on what it was actually shown. Anything
            # else is a verdict on a claim it never saw -- or on its own.
            shown = {c.id for c in _slice_for(spec, contested)}
            # A judge is told to return one verdict per claim in its slice.
            # One that silently returns fewer still passes validation, and
            # the claims it skipped would look merely `unproven` -- which
            # the discard rule turns TERMINAL after two rounds. A claim
            # nobody was willing to judge would then be closed as though
            # judges had looked and failed. Recorded, and the round is
            # marked incomplete so those claims read as `incomplete`
            # (§7.2's M12: a judge that never reported) rather than
            # `unproven`, which is what keeps them out of the discard rule.
            omitted = shown - {v.claim_id for v in cast}
            if omitted:
                any_failed = True
                _never_reported(missing, spec, [contested_ids[c] for c in omitted])
                outcome.downgrades.append(
                    f"round {round_no}: {spec.name} was shown {len(shown)} claim(s) "
                    f"and returned no verdict on {sorted(omitted)}; those claims "
                    "were not judged by it."
                )
            for verdict in cast:
                if verdict.claim_id not in shown:
                    outcome.downgrades.append(
                        f"round {round_no}: {spec.name} returned a verdict on "
                        f"{verdict.claim_id!r}, which was not in its slice; discarded."
                    )
                    continue
                round_verdicts.append(verdict)
        if any_failed:
            outcome.incomplete = True

        for verdict in round_verdicts:
            store.ledger.append(verdict)
        outcome.verdicts.extend(round_verdicts)

        _settle_round(outcome, contested, signatures, specs, store, round_no, max_rounds, missing)
        round_no += 1

    if budget.exhausted_by:
        outcome.ceiling_hit = BUDGET_EXHAUSTED
        outcome.downgrades.append(f"{BUDGET_EXHAUSTED}: {budget.exhausted_by}")
    return outcome


def _settle_round(
    outcome: CrossexamOutcome,
    contested: list[Claim],
    signatures: dict[str, "vd._Signature"],
    specs: list[FriendSpec],
    store: RunStore,
    round_no: int,
    max_rounds: int,
    missing: dict[str, set[str]],
) -> None:
    """Recompute every contested claim's state and grow the claim list with
    any successors a unanimous amendment produced. `missing` maps a claim
    id to the judges that never reported on it this round."""
    roster = [friend_key(s) for s in specs]
    for claim in contested:
        state = vd.state_for(
            claim,
            outcome.verdicts,
            roster,
            round_no,
            max_rounds,
            required_missing=bool(missing.get(claim.id)),
        )

        if state == vd.UNPROVEN:
            # §7.2's discard rule. A claim whose evidence names a path that
            # does not exist draws the same non-dispositive verdicts every
            # round, identically, at full cost until max_rounds.
            signature = vd.verdict_set_signature(outcome.verdicts, claim.id)
            if vd.should_discard(signatures.get(claim.id), signature):
                state = vd.DISCARDED
            signatures[claim.id] = signature
        else:
            # "Two consecutive rounds" means consecutive. A claim that was
            # unproven in round 2, contested in round 3 (judges engaged and
            # split) and unproven again in round 4 was, until this reset,
            # compared against round 2 and discarded -- closing a claim
            # with live disagreement on the record as though nobody had
            # ever been able to look. Raised by codex reviewing this file;
            # reachability confirmed by test_discard_consecutive.
            signatures.pop(claim.id, None)

        if state == vd.SUPERSEDED:
            # Latest per judge, not every amendment ever cast. `verdicts`
            # accumulates across rounds, so a judge that amended in round 2
            # and changed its mind in round 3 would otherwise still supply
            # wording for the successor -- the same accumulation bug already
            # fixed in state_for and verdict_set_signature, missed here.
            amendments = [
                v
                for v in vd.latest_per_judge(v for v in outcome.verdicts if v.claim_id == claim.id)
                if v.verdict == "amended"
            ]
            successor, note = vd.build_successor(claim, amendments, round_no)
            if note:
                outcome.notes.append(note)
            # The successor is a real claim and goes in the ledger like any
            # other, or `supersedes` on it points at a version the ledger
            # records while the successor itself exists nowhere.
            store.ledger.append(successor)
            outcome.claims.append(successor)
            outcome.states[successor.id] = vd.CONTESTED

        outcome.states[claim.id] = state

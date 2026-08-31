"""`afriend run --mode crossexam`: rounds 2..N, friends judging each other.

Round 1 is a critique round and belongs to commands/run.py, which calls
`run_rounds` once it has claims. From here on every round is a judging
round: each friend receives the still-contested claims it did not write
(§7.1), rendered blind (§5.1), and returns one verdict each.

The decisions this file makes are all IO and bookkeeping. Every rule that
decides an outcome -- who may judge, what settles, what deadlocks, what is
discarded, what an amendment becomes -- lives in verdicts.py as pure
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
from ..adapters import Adapter, FriendSpec, friend_key
from ..authority import ExternalToolPolicy
from ..ceilings import BUDGET_EXHAUSTED, Budget, within_deadline
from ..dispatch import argv_size_warning
from ..failures import RepeatTracker
from ..judgeprompt import build_judge_prompt
from ..ledger import Claim, Verdict
from ..progress import Progress
from ..reviewstate import ReviewState
from ..rounds import (
    DispatchRoundOutcome,
    RoundResult,
    dispatch_round,
    partition_dispatchable,
    persist_result,
    persist_skip,
    prune_undispatched_prompts,
)
from ..runstore import RunStore
from ..verdictschema import VERDICT_CONTRACT
from .judging import (
    _never_reported,
    _parse_verdicts,
    _prior_verdicts_by_claim,
    _slice_for,
)


@dataclass
class CrossexamOutcome:
    """Everything rounds 2..N produced, for the report and the exit code.

    In `loop` mode this accumulates across iterations rather than starting
    fresh: `run_rounds` seeds a block from the previous one (`prior`). It
    carried only claim states at first, and a review of that found four
    things wrong with it at once -- a claim deadlocked in an earlier
    iteration printed under "Unsettled" with no verdicts beneath it; later
    blocks' judges never saw earlier arguments; a required friend's failure
    in an earlier iteration was forgotten; and the discard rule, which
    needs two consecutive rounds, could never fire in a loop whose blocks
    hold one judging round each.
    """

    verdicts: list[Verdict] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    friends_meta: list[dict[str, Any]] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rounds_run: int = 1
    ceiling_hit: str | None = None
    incomplete: bool = False
    # Per-claim discard fingerprints (§7.2), on the outcome rather than
    # local to `run_rounds` so a loop's next block can go on comparing
    # against the last round that actually happened.
    signatures: dict[str, "vd._Signature"] = field(default_factory=dict)
    # Friends already announced as disabled. On the outcome rather than local
    # to `run_rounds` for the same reason `signatures` is: a loop calls
    # run_rounds once per iteration, so a per-call set re-announced the same
    # friend every iteration, when the spec asks for it once per run.
    dropped: set[str] = field(default_factory=set)
    # Set when a judging round hit a deterministic auth failure. The round
    # that found it is still settled and its verdicts kept -- only further
    # rounds are skipped.
    auth_abort: str | None = None
    dispatch_error: BaseException | None = None


def run_rounds(
    specs: list[FriendSpec],
    claims: list[Claim],
    store: RunStore,
    review: ReviewState,
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
    prior: CrossexamOutcome | None = None,
    final_block: bool = True,
    reporter: Progress | None = None,
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY,
    announced_skips: set[str] | None = None,
) -> CrossexamOutcome:
    """Judge `claims` over rounds `first_round`..`max_rounds`.

    `first_round` is 2 for a plain crossexam -- round 1 is the critique. A
    `loop` iteration passes a higher number: each iteration owns a distinct
    block of round numbers so its rounds never collide with an earlier
    iteration's in the run directory or the ledger.

    `prior` is the previous block's outcome, and carries what an earlier
    loop iteration decided AND what it heard -- states, verdicts, notes and
    discard signatures (see `CrossexamOutcome`).
    Terminal is terminal (§7.2): without it every iteration re-seeded every
    claim `contested` and re-judged claims it had already settled -- and a
    claim re-amended each iteration produced a successor with the SAME id
    each time, since `bump_claim_id` counts versions rather than records.
    A three-iteration loop wrote `c-0002@2` into the ledger three times.

    `final_block` says this is the last block of rounds the run will
    schedule. It is False for every loop iteration but the last, where
    another iteration will judge what this one leaves contested.
    """
    outcome = CrossexamOutcome(claims=list(claims))
    announced = announced_skips if announced_skips is not None else set()
    if prior is not None:
        # Everything the next block needs to keep telling the truth about
        # what already happened: the arguments themselves, the notes, the
        # discard fingerprints, and whether a required friend has already
        # failed somewhere in this run.
        outcome.verdicts = list(prior.verdicts)
        outcome.notes = list(prior.notes)
        outcome.signatures = dict(prior.signatures)
        outcome.incomplete = prior.incomplete

    # A claim starts unjudged. `contested` is the non-terminal set, so
    # seeding every claim as contested is what puts it in round 2's slice.
    carried = prior.states if prior is not None else {}
    outcome.states = {claim.id: carried.get(claim.id, vd.CONTESTED) for claim in outcome.claims}
    # Seeded from the previous block so a loop announces a disabled friend
    # once per RUN, not once per iteration.
    outcome.dropped = set(prior.dropped) if prior is not None else set()

    round_no = first_round
    while round_no <= max_rounds:
        if abort_event.is_set():
            break
        contested = [
            c for c in outcome.claims if outcome.states.get(c.id) not in vd.TERMINAL_STATES
        ]
        if not contested:
            break

        # A friend the repeat tracker disabled stays disabled for the rest
        # of the run, so it is not a judge that happens to be silent this
        # round -- it is not a judge.
        #
        # Note WHY, because the obvious reason is wrong: `RepeatTracker`
        # does clear a disabled friend, in `record`, on any success. An
        # earlier version of this comment claimed it never does. What makes
        # the exclusion permanent is this filter itself -- a friend dropped
        # from `active` is never dispatched again, so it never records the
        # success that would re-enable it.
        # Counted as one, it was recorded "missing" every round, which
        # pinned its claims at `incomplete`: never `unproven`, so never
        # discardable, so a loop could not converge on them. Dropping it
        # from the roster makes quorum reflect who can still vote, and the
        # shrunken roster is reported rather than implied.
        active, skipped = partition_dispatchable(specs, tracker)
        for item in skipped:
            outcome.friends_meta.append(persist_skip(store, round_no, item))
            outcome.dropped.add(item.spec.name)
            if item.spec.name not in announced:
                outcome.downgrades.append(
                    f"round {round_no}: {item.reason} It is no longer counted as "
                    "one of the judges a claim needs."
                )
                announced.add(item.spec.name)
        judge_specs: list[FriendSpec] = []
        prompt_for: dict[str, Path] = {}
        prompt_text_for: dict[str, str] = {}
        prompt_downgrades_for: dict[str, list[str]] = {}
        contested_ids = {c.id: c for c in contested}
        for spec in active:
            slice_ = _slice_for(spec, contested)
            if not slice_:
                continue
            # Built per judge, not per round: each one must be told what the
            # OTHERS concluded, never what it concluded itself.
            prior_cast = _prior_verdicts_by_claim(
                outcome.verdicts, set(contested_ids), exclude_judge=friend_key(spec)
            )
            prompt_text, note = build_judge_prompt(
                spec, artifact_text, slice_, prior_cast, attributed
            )
            if note:
                prompt_downgrades_for.setdefault(spec.name, []).append(note)
            if spec.cli != "fake":
                # The round the original check never covered, and the one
                # more likely to trip it: a judging prompt carries the claims
                # and the prior verdicts on top of the same artifact.
                size_note = argv_size_warning(spec.name, registry[spec.cli], prompt_text)
                if size_note is not None:
                    prompt_downgrades_for.setdefault(spec.name, []).append(size_note)
            path = store.friend_prompt_path(round_no, spec.name)
            judge_specs.append(spec)
            prompt_for[spec.name] = path
            prompt_text_for[spec.name] = prompt_text

        if not judge_specs:
            # Every remaining claim was written by every friend. Nothing is
            # left that anyone is independent enough to judge, so further
            # rounds would cost a fan-out and decide nothing.
            if active:
                outcome.downgrades.append(
                    f"round {round_no}: no friend is independent of any remaining "
                    "claim, so no judging round could be run."
                )
            # Settle them before leaving. Breaking out directly would leave
            # every remaining claim at its `contested` seed, which reads as
            # "judges disagreed" -- the opposite of what happened, which is
            # that no judge existed. state_for returns `unproven` for a claim
            # with no judges, which is the honest answer.
            _settle_round(
                outcome, contested, active, store, review, round_no, max_rounds, {}, final_block
            )
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
        # A friend may not outlive the ceiling it was dispatched under.
        judge_specs = within_deadline(judge_specs, budget.seconds_left(now()))
        if not judge_specs:
            budget.exhaust(f"--max-wall-clock leaves no usable time for round {round_no}")
            break
        for spec in judge_specs:
            prompt_for[spec.name].write_text(prompt_text_for[spec.name], encoding="utf-8")

        results: list[RoundResult] = []
        try:
            batch: DispatchRoundOutcome = dispatch_round(
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
                extra_args=extra_args,
                pass_env=pass_env,
                keep=keep,
                reporter=reporter,
                kind="judging",
                external_tool_policy=external_tool_policy,
            )
            results = batch.results
            round_auth_abort = batch.auth_abort
            outcome.dispatch_error = batch.error
        finally:
            prune_undispatched_prompts(judge_specs, prompt_for, results)
        for spec, _capability, _result in results:
            outcome.downgrades.extend(prompt_downgrades_for.get(spec.name, []))
        budget.spend(len(results))
        outcome.rounds_run = round_no

        # A judge selected for this round but absent from results never
        # reported -- §7.2's M12, the same as one that failed. Repeat-disabled
        # judges were partitioned and audited before prompts above; this
        # catches a dispatch that was interrupted during setup instead.
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
                outcome,
                contested,
                # `active`, not `specs`: the other two settle paths use the
                # shrunken roster, and counting friends that cannot vote
                # toward quorum is the exact thing the roster shrinking
                # exists to prevent.
                active,
                store,
                review,
                round_no,
                max_rounds,
                missing,
                final_block,
            )
            break

        round_verdicts: list[Verdict] = []
        any_failed = bool(withheld)
        for spec, capability, result in results:
            transport = "fake" if spec.cli == "fake" else registry[spec.cli].transport
            outcome.friends_meta.append(
                persist_result(
                    store,
                    round_no,
                    spec,
                    capability,
                    result,
                    transport,
                    external_tool_policy,
                )
            )
            if result.failure_reason is not None:
                # §7.2's M12: a round in which a required friend fails marks
                # the RUN incomplete, regardless of per-claim states.
                any_failed = True
                _never_reported(missing, spec, _slice_for(spec, contested))
                continue
            cast = _parse_verdicts(result.result.payload or {}, friend_key(spec), round_no)
            # §6.5's rewrite, applied before anything counts the verdict: an
            # unverifiable dispositive verdict is not dispositive. There is no
            # final-round rewrite of `amended` to `upheld` any more -- see
            # `_settle_round`'s successor handling for what replaced it.
            cast = [vd.downgrade_unverifiable(v) for v in cast]
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
            review.apply(verdict)
        outcome.verdicts.extend(round_verdicts)

        _settle_round(
            outcome,
            contested,
            active,
            store,
            review,
            round_no,
            max_rounds,
            missing,
            final_block,
        )
        if round_auth_abort is not None:
            outcome.auth_abort = round_auth_abort
            break
        if outcome.dispatch_error is not None:
            break
        round_no += 1

    if budget.exhausted_by:
        outcome.ceiling_hit = BUDGET_EXHAUSTED
        outcome.downgrades.append(f"{BUDGET_EXHAUSTED}: {budget.exhausted_by}")
    return outcome


def _settle_round(
    outcome: CrossexamOutcome,
    contested: list[Claim],
    specs: list[FriendSpec],
    store: RunStore,
    review: ReviewState,
    round_no: int,
    max_rounds: int,
    missing: dict[str, set[str]],
    final_block: bool,
) -> None:
    """Recompute every contested claim's state and grow the claim list with
    any successors a unanimous amendment produced. `missing` maps a claim
    id to the judges that never reported on it this round."""
    roster = [friend_key(s) for s in specs]
    for claim in contested:
        state = review.claim_state(
            claim,
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
            if vd.should_discard(outcome.signatures.get(claim.id), signature):
                state = vd.DISCARDED
            outcome.signatures[claim.id] = signature
        else:
            # "Two consecutive rounds" means consecutive. A claim that was
            # unproven in round 2, contested in round 3 (judges engaged and
            # split) and unproven again in round 4 was, until this reset,
            # compared against round 2 and discarded -- closing a claim
            # with live disagreement on the record as though nobody had
            # ever been able to look. Raised by codex reviewing this file;
            # reachability confirmed by test_discard_consecutive.
            outcome.signatures.pop(claim.id, None)

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
            # Every id the ledger already holds, so a re-amended claim in a
            # loop cannot mint a successor id that already exists.
            successor, note = vd.build_successor(
                claim, amendments, round_no, taken={c.id for c in outcome.claims}
            )
            if note:
                outcome.notes.append(note)
            # The successor is a real claim and goes in the ledger like any
            # other, or `supersedes` on it points at a version the ledger
            # records while the successor itself exists nowhere.
            store.ledger.append(successor)
            review.apply(successor)
            outcome.claims.append(successor)
            # Created by the run's last judging round, so nothing will judge
            # it: it stays non-terminal and the run says so. The rule this
            # replaced rewrote a final-round `amended` to `upheld` so that
            # no unjudgeable successor could exist -- and on a real
            # crossexam turned two judges' "the headline is false, here is
            # the rewrite" into `settled-upheld`, reported as "judges
            # unanimously agreed the claim stands". A rewrite nobody could
            # judge is the honest outcome; it blocks a gate, as a defect two
            # judges called real should.
            #
            # `final_block` is what keeps this from firing on every loop
            # iteration: `max_rounds` there is the iteration's own ceiling,
            # and a successor the next iteration will judge is not one
            # nobody could judge. The run-level `incomplete` flag is NOT set
            # -- it means "a required friend failed" (§7.2 M12), which the
            # report says in those words, and no friend failed here.
            last_block_round = round_no >= max_rounds and final_block
            outcome.states[successor.id] = vd.INCOMPLETE if last_block_round else vd.CONTESTED
            if last_block_round:
                outcome.downgrades.append(
                    f"{successor.id}: created by an amendment in round {round_no}, "
                    "the last this run will schedule; no round was left to judge "
                    "it. Re-run with a higher --max-rounds to have it judged."
                )
            elif round_no >= max_rounds:
                # Last round of a block that is not the run's last. The next
                # iteration is expected to judge it -- but the loop may stop
                # first (a dry streak, or a ceiling), and a successor with no
                # independent judge is excluded from the termination test, so
                # it cannot even hold the loop open. Left silent, it would be
                # reported as `contested`: "judges disagreed" about a rewrite
                # no judge has seen.
                outcome.downgrades.append(
                    f"{successor.id}: created by an amendment in round {round_no}, "
                    "the last of this iteration; it is judged by the next iteration "
                    "if the loop runs one, and is unjudged if the loop stops here."
                )

        outcome.states[claim.id] = state

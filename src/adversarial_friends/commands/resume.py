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
from collections.abc import Callable, Sequence
import concurrent.futures
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
from typing import Any

from ..adapters import Adapter, FriendSpec
from ..authority import ExternalToolPolicy
from ..ceilings import Budget
from ..failures import RepeatTracker
from ..ids import format_claim_id
from ..ledger import Alias, Claim
from ..merge import next_claim_number
from ..orchestrator import (
    QUESTION_EXTRACT,
    apply_merges,
    read_extract_response,
    read_response,
    request_path,
)
from ..progress import Progress
from ..reviewstate import ReviewState
from ..runstore import RunStore
from ..verdictschema import schema_path as verdict_schema_path
from .crossexam import CrossexamOutcome, run_rounds
from .haltstate import resumed_streak
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
    # The next unused claim number, taken from the whole ledger rather than
    # from the length of the canonical list. See merge.next_claim_number:
    # counting the canonical list re-issues every id a merge retired.
    counter: int = 0
    aliases: list[Alias] = field(default_factory=list)
    friends_meta: list[dict[str, Any]] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    cross: CrossexamOutcome | None = None


# The name a consumed adjudication response is renamed to. Kept beside the
# original rather than deleted: it is the operator's own written judgment,
# and a run directory that discards it cannot be audited afterwards.
CONSUMED_SUFFIX = ".applied"


def _mark_response_consumed(round_dir: Path) -> None:
    """Rename RESPONSE.json once its contents are in the ledger.

    The ledger is append-only with no dedupe (`ledger.append` is a bare JSONL
    write), and a resume was free to be run twice: the second run re-read the
    same untouched RESPONSE.json and appended every extracted claim again,
    under fresh ids because the counter had grown. The run history then held
    each finding twice, with no way to tell the copies apart.

    The merge branch never had this failure -- canonical reconstruction has
    already dropped the aliased duplicate by then, so `read_response` refuses
    the now-unknown id with a UsageError. Judges split on exactly that
    distinction when this was raised, one refuting because the scenario named
    the merge path and two amending to name the extraction path. Both are
    covered here: the merge branch's loud refusal is a worse experience than
    a resume that simply finds nothing left to apply.

    Rename rather than delete, and tolerate a rename that loses a race: two
    resumes cannot both be applying this file anyway, because `store.lock()`
    admits one writer per run directory.
    """
    response = round_dir / "RESPONSE.json"
    with contextlib.suppress(OSError):
        response.rename(response.with_suffix(response.suffix + CONSUMED_SUFFIX))


def _resumed_progress(records: Sequence[object], round_no: int) -> tuple[int, frozenset[str]]:
    """How much of THIS round's RESPONSE.json the ledger already reflects.

    Read from the ledger itself, the same source `canonical_claims` and
    `next_claim_number` already read, rather than a separate progress file
    -- the append-only log is already this module's source of truth for
    "what actually happened", including what happened in an attempt that
    then crashed.

    A crash between appending a ledger record and `_mark_response_consumed`
    renaming RESPONSE.json used to mean every retry re-read the identical,
    still-present file from the start: extraction re-appended findings
    already written under fresh ids, and merge crashed outright, because
    `canonical_claims` had already folded away a `duplicate` id the response
    still names -- turning one crash into a run permanently unable to
    resume.

    Two independent counts, because the two branches record progress
    differently:

    * `extracted_count` -- how many `lens="extracted"` claims already carry
      this round number. `read_extract_response` returns entries in a
      stable file order every call (RESPONSE.json is not mutated until the
      whole response is applied), so the leading `extracted_count` entries
      of a fresh read are exactly the ones an earlier, crashed attempt at
      this same round already wrote. The caller skips them rather than
      re-appending.
    * `merged_duplicates` -- the `duplicate` id of every Alias already
      recorded for this round. Passed to `read_response` so it can skip
      re-validating exactly the merges a prior attempt already finished,
      instead of refusing the whole file over ids that are correctly gone.
    """
    extracted_count = 0
    merged: set[str] = set()
    for record in records:
        if isinstance(record, Claim) and record.round == round_no and record.lens == "extracted":
            extracted_count += 1
        elif isinstance(record, Alias) and record.round == round_no:
            merged.add(record.duplicate)
    return extracted_count, frozenset(merged)


def resume_round_one(
    args: argparse.Namespace,
    store: RunStore,
    review: ReviewState,
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
    prior: "CrossexamOutcome | None" = None,
    tracker: RepeatTracker | None = None,
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
    reporter: Progress | None = None,
    final_block: bool = True,
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY,
) -> ResumedRun:
    """Apply the orchestrator's merges, then carry on into judging.

    **`prior` is what an earlier iteration already decided, and dropping it
    was a defect.** This called `run_rounds` with none of it, so a `loop`
    resumed at iteration 2 re-seeded every claim `contested` and re-judged
    what iteration 1 had settled: judges saw none of the prior arguments,
    the repeat signatures reset so the two-rounds-unproven discard rule
    could never fire, an earlier required-friend failure was forgotten, and
    a disabled friend was re-announced on every resume. The cost was a full
    fan-out per resume to reach conclusions the run already held.

    The same call was also missing `tracker`, `keep`, `extra_args` and
    `pass_env` -- so a resumed judging round silently ran with repeat
    detection off, isolation kept regardless of the flag, and the operator's
    own flags dropped. One omission, five behaviours.

    **`final_block` was missing too, and its default is `True`.** An
    amendment nobody judges in the LAST round of a block that is not the
    run's own last block is left `contested` for the next iteration to
    pick up; `final_block=True` instead marks it `incomplete` and tells the
    operator to raise `--max-rounds`, even when another loop iteration is
    seconds away and would have judged it. The caller computes this exactly
    the way the non-resumed path does: `mode != "loop" or iteration ==
    max_iterations`.
    """
    resumed = ResumedRun()
    claims = review.claims
    historical = [*review.claims_by_id.values(), *review.aliases]
    counter = next_claim_number(historical)
    round_dir = store.round_dir(base_round)

    # The same handshake serves two questions (§4.2, §14.2), so the answer is
    # read according to what was actually asked rather than assumed.
    question = _question_asked(round_dir)
    # What an EARLIER attempt at this exact round already applied, before it
    # crashed between a ledger write and _mark_response_consumed. Read from
    # the ledger, not guessed: see _resumed_progress.
    already_extracted, already_merged = _resumed_progress(historical, base_round)
    adjudicated: list[Alias] = []
    if question == QUESTION_EXTRACT:
        extracted = read_extract_response(round_dir)
        skipped = extracted[:already_extracted]
        for finding in extracted[already_extracted:]:
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
            review.apply(claims[-1])
        _mark_response_consumed(round_dir)
        note = (
            f"resumed from {store.run_id} after claim extraction: "
            f"{len(extracted) - already_extracted} claim(s) read out of unparseable output by hand."
        )
        if skipped:
            note += (
                f" ({len(skipped)} were already applied by an earlier, "
                "interrupted attempt at this same round.)"
            )
        resumed.downgrades.append(note)
    else:
        decisions = read_response(
            round_dir, {c.id for c in claims}, tolerate_duplicates=already_merged
        )
        claims, adjudicated = apply_merges(claims, decisions, base_round)
        for alias in adjudicated:
            store.ledger.append(alias)
            review.apply(alias)
        claims = review.claims
        _mark_response_consumed(round_dir)
        note = (
            f"resumed from {store.run_id} after orchestrator merge adjudication: "
            f"{len(adjudicated)} merge(s) applied."
        )
        if already_merged:
            note += (
                f" ({len(already_merged)} were already applied by an earlier, "
                "interrupted attempt at this same round.)"
            )
        resumed.downgrades.append(note)
    resumed.claims = claims
    resumed.counter = counter
    resumed.aliases = adjudicated
    resumed.friends_meta = list(getattr(args, "_resume_meta", {}).get("friends", []))
    # The FULL spend the halted process had accumulated, not just the round
    # this call is directly resuming. `len(specs)` -- one round's cost --
    # used to be charged back here instead, which under-counted every halt
    # but the run's very first: see write_halt's docstring for the failure
    # this reproduced. `spent_calls` is 0 when absent, which is correct for
    # a run halted by a version that predates this field -- an undercount
    # by omission, not a crash, and the honest choice when the true number
    # was simply never recorded.
    resume_meta = getattr(args, "_resume_meta", {}) or {}
    budget.spend(int(resume_meta.get("spent_calls", 0) or 0))

    if args.mode not in JUDGING_MODES:
        return resumed

    resumed.cross = run_rounds(
        specs,
        claims,
        store,
        review,
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
        tracker=tracker,
        keep=keep,
        extra_args=extra_args,
        pass_env=pass_env,
        prior=prior,
        reporter=reporter,
        final_block=final_block,
        external_tool_policy=external_tool_policy,
    )
    resumed.claims = resumed.cross.claims
    resumed.friends_meta.extend(resumed.cross.friends_meta)
    resumed.downgrades.extend(resumed.cross.downgrades)
    return resumed


@dataclass
class ResumedStep:
    """One resumed iteration: what it produced, and whether the loop is done.

    Exists so `cmd_run`'s loop body does not carry a second, parallel copy
    of the resume path inline. That copy was where four separate defects
    lived at once -- a re-issued claim counter, a judging round that
    inherited nothing, and a streak zeroed on every resume -- and none of
    them were reachable by a test without driving the whole command.
    """

    resumed: ResumedRun
    streak: int
    # True when no further iteration is due -- a non-loop mode is finished
    # the moment its resumed judging returns. A loop asks `loop_is_done`
    # itself, because that answer depends on the whole roster.
    done: bool


def resume_iteration(
    args: argparse.Namespace,
    store: RunStore,
    review: ReviewState,
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
    streak: int,
    prior: "CrossexamOutcome | None" = None,
    tracker: RepeatTracker | None = None,
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
    reporter: Progress | None = None,
    final_block: bool = True,
    external_tool_policy: ExternalToolPolicy = ExternalToolPolicy.DENY,
) -> ResumedStep:
    """Apply the adjudication, judge, and decide whether the loop continues."""
    resumed = resume_round_one(
        args,
        store,
        review,
        specs,
        registry,
        fake_cmd,
        artifact,
        artifact_text,
        repo_root,
        snapshot_sha,
        abort_event,
        budget,
        base_round,
        on_pool,
        prior=prior,
        tracker=tracker,
        keep=keep,
        extra_args=extra_args,
        pass_env=pass_env,
        reporter=reporter,
        final_block=final_block,
        external_tool_policy=external_tool_policy,
    )
    if args.mode != "loop":
        return ResumedStep(resumed=resumed, streak=streak, done=True)
    return ResumedStep(resumed=resumed, streak=resumed_streak(args, streak), done=False)

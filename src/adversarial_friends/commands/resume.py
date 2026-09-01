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
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import threading
from typing import Any

from ..adapters import Adapter, FriendSpec
from ..authority import ExternalToolPolicy
from ..ceilings import Budget
from ..errors import UsageError
from ..failures import RepeatTracker
from ..ids import format_claim_id
from ..jsonio import MAX_JSON_FILE_BYTES, decode_json_object
from ..ledger import Alias, Claim
from ..merge import next_claim_number
from ..orchestrator import (
    QUESTION_EXTRACT,
    QUESTION_MERGE,
    MergeDecision,
    apply_merges,
    request_path,
    validate_extract_response,
    validate_merge_response,
)
from ..progress import Progress
from ..reviewstate import ReviewState
from ..runstore import RunStore
from ..verdictschema import schema_path as verdict_schema_path
from .crossexam import CrossexamOutcome, run_rounds
from .haltstate import resumed_streak
from .runmeta import JUDGING_MODES


@dataclass(frozen=True)
class OutstandingRequest:
    question: str
    digest: str


def _read_owned_json_bytes(store: RunStore, path: Path, label: str) -> bytes:
    try:
        return store.read_owned_bytes(path, max_bytes=MAX_JSON_FILE_BYTES)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UsageError(f"{label} is not a safely readable regular file: {exc}") from exc


def _outstanding_request(store: RunStore, round_dir: Path, round_no: int) -> OutstandingRequest:
    """Authenticate the exact request before touching its response."""
    path = request_path(round_dir)
    try:
        payload = _read_owned_json_bytes(store, path, "orchestrator request")
        data = decode_json_object(payload, path=path, label="orchestrator request")
    except UsageError as exc:
        raise UsageError(
            f"cannot resume: outstanding orchestrator request is invalid: {exc}"
        ) from exc
    expected_keys = {"version", "run_id", "round", "question"}
    if not expected_keys.issubset(data):
        raise UsageError(
            "cannot resume: outstanding orchestrator request is missing identity fields"
        )
    question = data.get("question")
    if (
        data.get("version") != 1
        or data.get("run_id") != store.run_id
        or data.get("round") != round_no
        or not isinstance(question, str)
        or question not in {QUESTION_MERGE, QUESTION_EXTRACT}
    ):
        raise UsageError(
            "cannot resume: outstanding orchestrator request does not match this run and round"
        )
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return OutstandingRequest(question, digest)


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
APPLYING_SUFFIX = ".applying"


def _mark_response_consumed(store: RunStore, round_dir: Path, prepared: "PreparedResponse") -> None:
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

    Rename rather than delete. A durable response-application checkpoint is
    written first, so a rename failure remains recoverable and must not be
    hidden while judging continues.
    """
    applying = round_dir / f"RESPONSE.json{APPLYING_SUFFIX}"
    applied = round_dir / f"RESPONSE.json{CONSUMED_SUFFIX}"
    # Never promote the mutable staging pathname.  The applied evidence is
    # created from the exact bounded byte snapshot that passed semantic
    # validation; a concurrent replacement of `.applying` therefore cannot
    # change either the audit artifact or its checkpoint digest.
    if not store.owned_regular_exists(applied):
        store.create_owned_bytes(applied, prepared.payload)
        store.fsync_owned_directory(round_dir)
    applied_payload = _read_owned_json_bytes(store, applied, "applied orchestrator response")
    if applied_payload != prepared.payload:
        raise UsageError("cannot resume: retained applied response changed after validation")
    live = round_dir / "RESPONSE.json"
    if store.owned_regular_exists(live):
        live_payload = _read_owned_json_bytes(store, live, "orchestrator response")
        if _response_digest(live_payload) != prepared.digest:
            raise UsageError(
                "cannot resume: live response changed after validation; retained for audit"
            )
        store.unlink_owned(live)
        store.fsync_owned_directory(round_dir)
    # `.applying` is only a legacy crash artifact now.  It is never a source
    # for the retained copy, so even a post-validation swap is safe to remove.
    if store.owned_regular_exists(applying):
        store.unlink_owned(applying)
        store.fsync_owned_directory(round_dir)


def _response_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validated_response_checkpoint(
    meta: dict[str, Any], *, round_no: int, question: str, request_digest: str
) -> dict[str, Any] | None:
    raw = meta.get("applied_response")
    if raw is None:
        if meta.get("lifecycle_state") == "response-applied":
            raise UsageError("cannot resume: response-applied checkpoint has no applied_response")
        return None
    if type(raw) is not dict:
        raise UsageError("cannot resume: saved applied_response has an invalid shape")
    keys = {"version", "round", "question", "request_sha256", "sha256", "records"}
    legacy_keys = keys - {"request_sha256"}
    if frozenset(raw) not in {frozenset(keys), frozenset(legacy_keys)}:
        raise UsageError("cannot resume: saved applied_response has an invalid shape")
    if "request_sha256" not in raw and meta.get("lifecycle_state") != "response-applied":
        raise UsageError("cannot resume: prepared response is not bound to its request")
    digest = raw.get("sha256")
    records = raw.get("records")
    if (
        raw.get("version") != 1
        or raw.get("round") != round_no
        or raw.get("question") != question
        or raw.get("request_sha256", request_digest) != request_digest
        or type(records) is not int
        or records < 0
        or not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in digest[7:])
    ):
        raise UsageError("cannot resume: saved applied_response is inconsistent with this halt")
    return raw


@dataclass(frozen=True)
class PreparedResponse:
    path: Path
    payload: bytes
    digest: str
    checkpoint: dict[str, Any] | None


def _prepare_response(
    store: RunStore,
    round_dir: Path,
    meta: dict[str, Any],
    *,
    round_no: int,
    question: str,
    request_digest: str,
) -> PreparedResponse:
    """Capture response bytes without mutating any lifecycle artifact."""
    live = round_dir / "RESPONSE.json"
    applying = round_dir / f"RESPONSE.json{APPLYING_SUFFIX}"
    applied = live.with_suffix(live.suffix + CONSUMED_SUFFIX)
    try:
        live_exists = store.owned_regular_exists(live)
        applying_exists = store.owned_regular_exists(applying)
        applied_exists = store.owned_regular_exists(applied)
    except OSError as exc:
        raise UsageError(f"cannot resume: response artifact must be a regular file: {exc}") from exc
    checkpoint = _validated_response_checkpoint(
        meta,
        round_no=round_no,
        question=question,
        request_digest=request_digest,
    )
    if checkpoint is None:
        if (applied_exists or applying_exists) and not live_exists:
            raise UsageError(
                "cannot resume: retained applying/applied response has no matching durable checkpoint"
            )
        selected = live
    else:
        selected = applied if applied_exists else applying if applying_exists else live
    if not store.owned_regular_exists(selected):
        if checkpoint is None:
            raise UsageError(
                f"no RESPONSE.json in {round_dir}. This run halted for orchestrator "
                "judgment; write the response described by REQUEST.json and re-run "
                "with --resume."
            )
        raise UsageError("cannot resume: response checkpoint has no response artifact")
    payload = _read_owned_json_bytes(store, selected, "orchestrator response")
    digest = _response_digest(payload)
    if checkpoint is not None and digest != checkpoint["sha256"]:
        raise UsageError("cannot resume: applied response artifact hash disagrees with checkpoint")
    for sibling, exists in (
        (live, live_exists),
        (applying, applying_exists),
        (applied, applied_exists),
    ):
        if exists and sibling != selected:
            sibling_payload = _read_owned_json_bytes(store, sibling, "orchestrator response")
            if _response_digest(sibling_payload) != digest:
                raise UsageError(
                    "cannot resume: multiple response artifacts disagree; state is ambiguous"
                )
    return PreparedResponse(selected, payload, digest, checkpoint)


def _materialize_response(store: RunStore, round_dir: Path, prepared: PreparedResponse) -> None:
    """Durably freeze the exact validated bytes before checkpoint mutation."""
    applied = round_dir / f"RESPONSE.json{CONSUMED_SUFFIX}"
    if store.owned_regular_exists(applied):
        existing = _read_owned_json_bytes(store, applied, "orchestrator response")
        if existing != prepared.payload:
            raise UsageError("cannot resume: prepared response disagrees with validated bytes")
        return
    store.create_owned_bytes(applied, prepared.payload)
    store.fsync_owned_directory(round_dir)


def _checkpoint_response_application(
    args: argparse.Namespace,
    store: RunStore,
    *,
    round_no: int,
    question: str,
    request_digest: str,
    response_digest: str,
    records: int,
) -> None:
    meta = dict(getattr(args, "_resume_meta", {}) or {})
    meta["lifecycle_state"] = "response-applied"
    meta["applied_response"] = {
        "version": 1,
        "round": round_no,
        "question": question,
        "request_sha256": request_digest,
        "sha256": response_digest,
        "records": records,
    }
    store.write_run_json(meta)
    args._resume_meta = meta


def _checkpoint_response_preparation(
    args: argparse.Namespace,
    store: RunStore,
    *,
    round_no: int,
    question: str,
    request_digest: str,
    response_digest: str,
    records: int,
) -> None:
    meta = dict(getattr(args, "_resume_meta", {}) or {})
    meta["lifecycle_state"] = "response-applying"
    meta["applied_response"] = {
        "version": 1,
        "round": round_no,
        "question": question,
        "request_sha256": request_digest,
        "sha256": response_digest,
        "records": records,
    }
    store.write_run_json(meta)
    args._resume_meta = meta


def _resumed_progress(records: Sequence[object], round_no: int) -> tuple[list[Claim], list[Alias]]:
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

    * `extracted` -- the `lens="extracted"` claims already carrying this
      round number, in durable order. The caller requires them to equal the
      response prefix before skipping them.
    * `merged` -- the orchestrator Alias records for this round, in durable
      order. Exact-dedup aliases are unrelated progress and are excluded;
      the caller requires every retained alias to equal the response prefix.
    """
    extracted: list[Claim] = []
    merged: list[Alias] = []
    for record in records:
        if isinstance(record, Claim) and record.round == round_no and record.lens == "extracted":
            extracted.append(record)
        elif (
            isinstance(record, Alias)
            and record.round == round_no
            and record.source == "orchestrator"
        ):
            merged.append(record)
    return extracted, merged


def _validate_partial_extraction(
    previous: Sequence[Claim], extracted: Sequence[dict[str, Any]], round_no: int
) -> None:
    if len(previous) > len(extracted):
        raise UsageError("cannot resume: partial extraction ledger does not match response")
    for claim, finding in zip(previous, extracted, strict=False):
        expected = (
            [finding.get("friend", "orchestrator")],
            round_no,
            finding["severity"],
            finding["claim"],
            finding.get("location"),
            finding["evidence"],
            finding["failure_scenario"],
            finding["suggested_fix"],
        )
        actual = (
            claim.origin,
            claim.round,
            claim.severity,
            claim.claim,
            claim.location,
            claim.evidence,
            claim.failure_scenario,
            claim.suggested_fix,
        )
        if claim.supersedes is not None or claim.advisory or actual != expected:
            raise UsageError("cannot resume: partial extraction ledger does not match response")


def _validate_partial_merges(
    previous: Sequence[Alias], decisions: Sequence[MergeDecision], round_no: int
) -> None:
    if len(previous) > len(decisions):
        raise UsageError("cannot resume: partial merge ledger does not match response")
    for alias, decision in zip(previous, decisions, strict=False):
        expected = Alias(
            canonical=decision.canonical,
            duplicate=decision.duplicate,
            round=round_no,
            source="orchestrator",
            rationale=decision.rationale or "adjudicated by orchestrator",
        )
        if alias != expected:
            raise UsageError("cannot resume: partial merge ledger does not match response")


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
    announced_skips: set[str] | None = None,
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
    try:
        round_dir = store.existing_round_dir(base_round)
    except OSError as exc:
        raise UsageError(
            f"cannot resume: round {base_round} is not safely readable: {exc}"
        ) from exc

    # The same handshake serves two questions (§4.2, §14.2), so the answer is
    # read according to what was actually asked rather than assumed.
    outstanding = _outstanding_request(store, round_dir, base_round)
    question = outstanding.question
    # What an EARLIER attempt at this exact round already applied, before it
    # crashed between a ledger write and _mark_response_consumed. Read from
    # the ledger, not guessed: see _resumed_progress.
    previous_extractions, previous_merges = _resumed_progress(historical, base_round)
    prepared = _prepare_response(
        store,
        round_dir,
        getattr(args, "_resume_meta", {}) or {},
        round_no=base_round,
        question=question,
        request_digest=outstanding.digest,
    )
    response_data = decode_json_object(
        prepared.payload, path=prepared.path, label="orchestrator response"
    )
    applied_checkpoint = prepared.checkpoint
    adjudicated: list[Alias] = []
    if question == QUESTION_EXTRACT:
        extracted = validate_extract_response(response_data, prepared.path)
        _validate_partial_extraction(previous_extractions, extracted, base_round)
        already_extracted = len(previous_extractions)
        if applied_checkpoint is not None and applied_checkpoint["records"] != len(extracted):
            raise UsageError("cannot resume: applied response result disagrees with durable ledger")
        if applied_checkpoint is None:
            _checkpoint_response_preparation(
                args,
                store,
                round_no=base_round,
                question=question,
                request_digest=outstanding.digest,
                response_digest=prepared.digest,
                records=len(extracted),
            )
            _materialize_response(store, round_dir, prepared)
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
        applied_records = len(extracted)
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
        decisions = validate_merge_response(
            response_data,
            prepared.path,
            {c.id for c in claims} | {alias.duplicate for alias in previous_merges},
        )
        _validate_partial_merges(previous_merges, decisions, base_round)
        already_merged = frozenset(alias.duplicate for alias in previous_merges)
        expected_records = len(decisions)
        if applied_checkpoint is not None and applied_checkpoint["records"] != expected_records:
            raise UsageError("cannot resume: applied response result disagrees with durable ledger")
        if applied_checkpoint is None:
            _checkpoint_response_preparation(
                args,
                store,
                round_no=base_round,
                question=question,
                request_digest=outstanding.digest,
                response_digest=prepared.digest,
                records=expected_records,
            )
            _materialize_response(store, round_dir, prepared)
        remaining_decisions = decisions[len(previous_merges) :]
        claims, adjudicated = apply_merges(claims, remaining_decisions, base_round)
        for alias in adjudicated:
            store.ledger.append(alias)
            review.apply(alias)
        claims = review.claims
        applied_records = len(already_merged) + len(adjudicated)
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
    # Parsing and semantic validation completed without mutation. Only now is
    # it safe to repair modes on an older supported run and its operator file.
    store.repair_permissions()
    _checkpoint_response_application(
        args,
        store,
        round_no=base_round,
        question=question,
        request_digest=outstanding.digest,
        response_digest=prepared.digest,
        records=applied_records,
    )
    _mark_response_consumed(store, round_dir, prepared)
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
    prior_calls = int(resume_meta.get("spent_calls", 0) or 0)
    if budget.calls == 0:
        # Compatibility for direct callers that still construct a blank
        # Budget. cmd_run restores before its first ceiling check, so it
        # arrives here already carrying the exact checkpoint total.
        budget.spend(prior_calls)
    elif budget.calls != prior_calls:
        raise ValueError("resume budget calls disagree with the saved checkpoint")

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
        announced_skips=announced_skips,
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
    announced_skips: set[str] | None = None,
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
        announced_skips=announced_skips,
    )
    if args.mode != "loop":
        return ResumedStep(resumed=resumed, streak=streak, done=True)
    return ResumedStep(resumed=resumed, streak=resumed_streak(args, streak), done=False)

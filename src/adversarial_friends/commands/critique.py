"""One critique round: build each friend's prompt, dispatch, merge claims.

Extracted from commands/run.py when `--mode loop` arrived. A loop iteration
is a whole critique round followed by a whole cross-examination, so the
round-1 body had to become callable more than once per run rather than
inlined in cmd_run.

Round numbering is passed in rather than fixed at 1 for the same reason:
iteration 2's critique is round `max_rounds + 1`, so every round in a run
gets a distinct directory and a distinct number in the ledger.
"""

from collections.abc import Callable
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any

from ..adapters import Adapter, FriendSpec, friend_key
from ..authority import DENY_ALL, AuthorityPolicy
from ..dispatch import argv_size_warning
from ..failures import RepeatTracker
from ..ids import format_claim_id
from ..ledger import Alias, Claim
from ..merge import exact_merge
from ..orchestrator import NeedsOrchestrator, write_extract_request
from ..progress import Progress
from ..prompt import _build_friend_prompt
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
from ..spawn import SpawnResult
from ..themes import ThemeProposal, classify_novel


@dataclass
class CritiqueOutcome:
    """What one critique round produced."""

    claims: list[Claim] = field(default_factory=list)
    aliases: list[Alias] = field(default_factory=list)
    friends_meta: list[dict[str, Any]] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    calls: int = 0
    any_success: bool = False
    any_failed: bool = False
    # Set when a friend hit a deterministic auth failure this round. Every
    # result this round produced is still persisted and merged below --
    # only the caller's decision to schedule another round is affected.
    auth_abort: str | None = None
    # A deliberate stop or interruption after one or more friend workers
    # crossed the dispatch boundary. Results remain fully auditable; the
    # outer run uses this to terminalize instead of scheduling more work.
    dispatch_error: BaseException | None = None
    # How many distinct friends produced a usable answer this round.
    # `any_success` alone cannot distinguish "1 of 50" from "50 of 50" --
    # both report the same True, and a run reporting SUCCESS because one
    # friend of a large roster answered is not the cross-examination its
    # exit code implies. See `--require-friends` in cliargs.py.
    succeeded_friends: int = 0
    successful_friend_ids: list[str] = field(default_factory=list)
    # Retained as an exact-merge diagnostic. Loop dryness is intentionally
    # based on produced_new_themes instead, so wording variants do not reset
    # convergence while exact ledger identities remain unchanged.
    produced_only_aliases: bool = True
    # Theme novelty is advisory and independent of exact ledger identity.
    produced_new_themes: bool = False
    theme_proposals: list[ThemeProposal] = field(default_factory=list)


def build_prompts(
    specs: list[FriendSpec],
    artifact_text: str,
    store: RunStore,
    registry: dict[str, Adapter],
    round_no: int,
) -> tuple[dict[str, Path], dict[str, bool], list[str]]:
    """Return (prompt path per friend, advisory flag per friend, downgrades).

    Every friend gets its OWN prompt, built from its own lens -- not a single
    prompt shared byte-for-byte across every friend regardless of
    `--friend cli:lens`. That was a real bug: the lens name was recorded for
    bookkeeping but its prose never reached the friend, so the only diversity
    in a run was model diversity. Each prompt is written next to that
    friend's `.raw`/`.meta` so a human can see exactly what it was asked.
    """
    prompt_for: dict[str, Path] = {}
    advisory_for: dict[str, bool] = {}
    downgrades: list[str] = []
    try:
        for spec in specs:
            prompt_text, advisory, lens_downgrade = _build_friend_prompt(spec, artifact_text)
            if lens_downgrade:
                downgrades.append(lens_downgrade)
            # claude, opencode, and agy all place the WHOLE prompt in one argv
            # element (prompt_mode "trailing-arg"/"flag-value"); Linux commonly
            # caps a single argument near 128KB (the limit varies by OS -- this
            # runner is not always run on Linux), so a large artifact can make
            # Popen() fail with E2BIG ("Argument list too long"). This is
            # detected, not solved -- switching prompt modes is a design change.
            # Recording the risk up front means an E2BIG failure is already
            # explained by the time it is read, not a surprise raw exit code.
            if spec.cli != "fake":
                warning = argv_size_warning(spec.name, registry[spec.cli], prompt_text)
                if warning is not None:
                    downgrades.append(warning)
            prompt_path = store.friend_prompt_path(round_no, spec.name)
            # Record the pathname before writing so a partial write that
            # raises is cleaned along with every earlier staged prompt.
            prompt_for[spec.name] = prompt_path
            store.write_sensitive(prompt_path, prompt_text)
            advisory_for[spec.name] = advisory
    except BaseException:
        for path in prompt_for.values():
            store.unlink_owned(path, missing_ok=True)
        raise
    return prompt_for, advisory_for, downgrades


def extraction_candidates(result: SpawnResult) -> bool:
    """Whether this failed result's raw text may be offered to §14.2
    extraction.

    Extraction is the path designed to pull meaning out of text nothing else
    could parse, which makes it precisely the wrong place to send a buffer
    that was CUT OFF. A prefix of a friend's answer can be valid JSON, and
    presenting a partial answer as a whole one is the failure spawn's own
    docstring rules out -- but that rule only ever governed normalize().
    This path read `payload is None`, which is exactly what a timed-out or
    overflowed result carries, so truncated output arrived here anyway.
    """
    if result.timed_out or result.output_truncated:
        return False
    return result.result.payload is None and bool(result.stdout.strip())


def run_critique(
    specs: list[FriendSpec],
    round_no: int,
    known_claims: list[Claim],
    claim_counter: int,
    artifact_text: str,
    store: RunStore,
    review: ReviewState,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    schema_file: Path,
    artifact: Path,
    repo_root: Path | None,
    snapshot_sha: str | None,
    abort_event: threading.Event,
    on_pool: Callable[[concurrent.futures.ThreadPoolExecutor | None], None] = lambda _p: None,
    allow_unsandboxed: bool = False,
    tracker: RepeatTracker | None = None,
    keep: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
    merge: str = "exact",
    run_id: str = "",
    reporter: Progress | None = None,
    authority_policy: AuthorityPolicy = DENY_ALL,
    announced_skips: set[str] | None = None,
) -> tuple[CritiqueOutcome, list[Claim], int]:
    """Dispatch one critique round and merge its claims into `known_claims`.

    Returns (outcome, the updated full claim list, the updated claim
    counter). `known_claims` is not mutated; the caller takes the returned
    list, which is what makes a second iteration's dedup work against
    everything seen so far rather than only against its own round.
    """
    outcome = CritiqueOutcome()
    announced = announced_skips if announced_skips is not None else set()
    dispatchable, skipped = partition_dispatchable(specs, tracker)
    for item in skipped:
        outcome.friends_meta.append(persist_skip(store, round_no, item))
        if item.spec.name not in announced:
            outcome.downgrades.append(item.reason)
            announced.add(item.spec.name)
    prompt_for, advisory_for, prompt_downgrades = build_prompts(
        dispatchable, artifact_text, store, registry, round_no
    )

    results: list[RoundResult] = []
    try:
        batch: DispatchRoundOutcome = dispatch_round(
            dispatchable,
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
            allow_unsandboxed=allow_unsandboxed,
            tracker=tracker,
            extra_args=extra_args,
            pass_env=pass_env,
            keep=keep,
            reporter=reporter,
            kind="critique",
            authority_policy=authority_policy,
        )
        results = batch.results
        outcome.auth_abort = batch.auth_abort
        outcome.dispatch_error = batch.error
    finally:
        prune_undispatched_prompts(dispatchable, prompt_for, results, store)
    result_names = {spec.name for spec, _capability, _result in results}
    outcome.downgrades.extend(
        note for note in prompt_downgrades if note.split(":", 1)[0] in result_names
    )
    outcome.calls = len(results)

    all_claims = list(known_claims)
    counter = claim_counter
    unparseable: list[dict[str, Any]] = []
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
                authority_policy.for_provider(spec.cli),
            )
        )
        if result.failure_reason is not None:
            # §14.2: repair is a pure transformation, so when it fails the
            # only thing left that can read the raw text is something with
            # judgment. Under --merge=orchestrator this halts for extraction
            # rather than discarding whatever the friend actually found;
            # under --merge=exact the friend is simply failed, which is what
            # keeps the default usable from a plain shell.
            if merge == "orchestrator" and extraction_candidates(result):
                # Collected, not raised here: every friend in this round has
                # already been dispatched, and halting mid-loop would strand
                # the claims of friends processed after this one -- their
                # results exist only in memory and would be gone on resume.
                unparseable.append(
                    {
                        "friend": spec.name,
                        "raw": result.stdout,
                        "errors": result.result.errors,
                    }
                )
            outcome.any_failed = True
            continue
        outcome.any_success = True
        outcome.succeeded_friends += 1
        outcome.successful_friend_ids.append(spec.name)
        incoming = []
        # `or []`, not a .get default: `findings` is nullable in the schema
        # (strict mode requires every property in `required`, so a friend
        # reporting nothing sends `findings: null`), and .get returns that
        # None rather than the default when the key is present.
        for finding in (result.result.payload or {}).get("findings") or []:
            counter += 1
            incoming.append(
                Claim(
                    id=format_claim_id(counter),
                    supersedes=None,
                    origin=[friend_key(spec)],
                    lens=spec.lens,
                    round=round_no,
                    advisory=advisory_for[spec.name],
                    severity=finding["severity"],
                    claim=finding["claim"],
                    location=finding.get("location"),
                    evidence=finding["evidence"],
                    failure_scenario=finding["failure_scenario"],
                    suggested_fix=finding["suggested_fix"],
                )
            )
        novel_theme_ids, proposals = classify_novel(all_claims, incoming)
        if novel_theme_ids:
            outcome.produced_new_themes = True
        outcome.theme_proposals.extend(proposals)
        kept, aliases, _updated_existing = exact_merge(all_claims, incoming, round_no=round_no)
        if kept:
            # Preserve the exact-merge diagnostic for callers that inspect
            # it; loop novelty is the separate theme classification above.
            outcome.produced_only_aliases = False
        # Every incoming claim is written to the ledger, not just the ones
        # exact_merge kept: an Alias record's `duplicate` id must resolve to
        # a real `claim` record, or claims.jsonl has a dangling reference --
        # a reader following canonical<-duplicate links (the only way to
        # recover full corroboration from the ledger alone) hits a dead end.
        for record in incoming:
            store.ledger.append(record)
            review.apply(record)
        for alias in aliases:
            store.ledger.append(alias)
            review.apply(alias)
        # The reducer owns canonicalization and accumulated provenance. It
        # now replaces the parallel process-local reconstruction that used
        # `updated_existing` and `kept` to approximate the ledger's state.
        all_claims = review.claims
        outcome.aliases.extend(aliases)
        outcome.claims.extend(kept)
    # Auth abort takes priority over an extraction request: an auth failure
    # will recur identically on --resume, so asking a human to adjudicate
    # merges or read unparseable output is asking them to fix something a
    # broken credential will just fail again. This is not returned as a
    # NeedsOrchestrator halt for the same reason -- there is nothing a
    # RESPONSE.json could resolve.
    if outcome.auth_abort is None and unparseable:
        path = write_extract_request(
            store.round_dir(round_no), run_id, round_no, unparseable, store=store
        )
        names = ", ".join(e["friend"] for e in unparseable)
        raise NeedsOrchestrator(
            f"{names} produced output that could not be parsed into claims. "
            f"Fill in `findings` for each in {path}, save it as RESPONSE.json "
            "beside it, then re-run with --resume.",
            calls=outcome.calls,
            friends_meta=outcome.friends_meta,
            downgrades=outcome.downgrades,
            successful_friend_ids=outcome.successful_friend_ids,
            theme_proposals=outcome.theme_proposals,
            produced_new_themes=outcome.produced_new_themes,
        )
    return outcome, all_claims, counter

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
from ..dispatch import argv_size_warning
from ..failures import RepeatTracker
from ..ids import format_claim_id
from ..ledger import Alias, Claim
from ..merge import exact_merge
from ..orchestrator import NeedsOrchestrator, write_extract_request
from ..progress import Progress
from ..prompt import _build_friend_prompt
from ..rounds import dispatch_round, persist_result
from ..runstore import RunStore
from ..spawn import SpawnResult


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
    # §7.3: a round is dry when every required friend completed successfully
    # AND every claim it produced was an alias of one already known -- i.e.
    # the round cost a full fan-out and learned nothing new.
    produced_only_aliases: bool = True


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
        prompt_path.write_text(prompt_text, encoding="utf-8")
        prompt_for[spec.name] = prompt_path
        advisory_for[spec.name] = advisory
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
) -> tuple[CritiqueOutcome, list[Claim], int]:
    """Dispatch one critique round and merge its claims into `known_claims`.

    Returns (outcome, the updated full claim list, the updated claim
    counter). `known_claims` is not mutated; the caller takes the returned
    list, which is what makes a second iteration's dedup work against
    everything seen so far rather than only against its own round.
    """
    outcome = CritiqueOutcome()
    prompt_for, advisory_for, prompt_downgrades = build_prompts(
        specs, artifact_text, store, registry, round_no
    )
    outcome.downgrades.extend(prompt_downgrades)

    results = dispatch_round(
        specs,
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
        downgrades=outcome.downgrades,
        extra_args=extra_args,
        pass_env=pass_env,
        keep=keep,
        reporter=reporter,
        kind="critique",
    )
    outcome.calls = len(results)

    all_claims = list(known_claims)
    counter = claim_counter
    unparseable: list[dict[str, Any]] = []
    for spec, capability, result in results:
        outcome.friends_meta.append(persist_result(store, round_no, spec, capability, result))
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
        kept, aliases, updated_existing = exact_merge(all_claims, incoming, round_no=round_no)
        if kept:
            # Something survived dedup, so this round taught the run
            # something. §7.3's dry-round test is exactly this, inverted.
            outcome.produced_only_aliases = False
        # Every incoming claim is written to the ledger, not just the ones
        # exact_merge kept: an Alias record's `duplicate` id must resolve to
        # a real `claim` record, or claims.jsonl has a dangling reference --
        # a reader following canonical<-duplicate links (the only way to
        # recover full corroboration from the ledger alone) hits a dead end.
        for record in incoming:
            store.ledger.append(record)
        for alias in aliases:
            store.ledger.append(alias)
        if updated_existing:
            # A canonical claim from an EARLIER friend just gained this
            # friend's origin too. The ledger keeps its original, immutable
            # record as first written -- Alias plus the duplicate's own claim
            # record already let a reader reconstruct the same corroboration
            # -- but the in-memory list this run still uses (for the NEXT
            # friend's dedup pass, and for the final report) must reflect the
            # grown origin, or report.md would undercount how many friends
            # actually agreed.
            updated_by_id = {c.id: c for c in updated_existing}
            all_claims = [updated_by_id.get(c.id, c) for c in all_claims]
        all_claims.extend(kept)
        outcome.aliases.extend(aliases)
        outcome.claims.extend(kept)
    if unparseable:
        path = write_extract_request(store.round_dir(round_no), run_id, round_no, unparseable)
        names = ", ".join(e["friend"] for e in unparseable)
        raise NeedsOrchestrator(
            f"{names} produced output that could not be parsed into claims. "
            f"Fill in `findings` for each in {path}, save it as RESPONSE.json "
            "beside it, then re-run with --resume."
        )
    return outcome, all_claims, counter

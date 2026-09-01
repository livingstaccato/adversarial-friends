"""The halt/resume handshake for judgment the runner cannot make -- §4.2.

Deduplication is judgment. `--merge=exact` under-merges on purpose, because
guessing at equivalence corrupts termination arithmetic; but it means two
friends describing one defect in different words produce two claims, and the
report shows one problem twice.

`--merge=orchestrator` hands that judgment out. The runner writes
`round-N/REQUEST.json`, exits 10, and stops. Something with judgment -- an
agent driving this skill, or a person -- writes `RESPONSE.json`. `afriend run
--resume RUN_ID` reads it and continues.

**The same response must always produce the same run.** That is what makes
mode drivers deterministic and lets fixtures ship canned responses, so this
module applies a response as data rather than re-deciding anything.

**A response is checked, not trusted.** It arrives as a file on disk, written
by another process, naming claim ids that become permanent ledger records. A
merge naming an id that does not exist, or a chain of merges, would corrupt
the alias graph in ways only discovered much later while reading a report --
so every referenced id is resolved and every structural rule is enforced
before a single Alias is written.
"""

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

from .claimschema import validate_payload
from .errors import AfError, UsageError
from .jsonio import load_json_object
from .ledger import Alias, Claim
from .secureio import secure_write_text
from .themes import ThemeProposal

REQUEST_NAME = "REQUEST.json"
RESPONSE_NAME = "RESPONSE.json"

# §7.6's exit code for "needs orchestrator".
NEEDS_ORCHESTRATOR_EXIT = 10

SCHEMA_VERSION = 1

# What the orchestrator is being asked to decide. Only merge adjudication is
# implemented; §14.2's parse-halt extraction is the other user of this same
# handshake and would arrive as a second question kind rather than a second
# mechanism.
QUESTION_MERGE = "merge"
# §14.2's other use of this same handshake: a friend whose output could not
# be repaired by pure transformation. Repair is deliberately not a model call
# (re-prompting reaches a fresh process that never produced the broken
# output), so when it fails the only thing left that can read the raw text is
# something with judgment.
QUESTION_EXTRACT = "extract"

_INSTRUCTIONS = (
    "Two of these claims may describe the same defect in different words. "
    "For each such pair, add an entry to `merges` naming the claim to keep "
    "as `canonical` and the one it subsumes as `duplicate`, with a short "
    "`rationale`. Merge only what you are confident about: an unmerged "
    "duplicate costs a round, a wrong merge silently deletes a finding. "
    "Write this file as RESPONSE.json beside REQUEST.json, then re-run with "
    "--resume."
)


class NeedsOrchestrator(AfError):
    """Raised to stop a run that is waiting on RESPONSE.json."""

    exit_code = NEEDS_ORCHESTRATOR_EXIT

    def __init__(
        self,
        message: str,
        *,
        calls: int = 0,
        friends_meta: list[dict[str, Any]] | None = None,
        downgrades: list[str] | None = None,
        successful_friend_ids: list[str] | None = None,
        theme_proposals: list[ThemeProposal] | None = None,
        produced_new_themes: bool = False,
    ) -> None:
        super().__init__(message)
        # Extraction halts are raised from inside run_critique after the
        # dispatch completed. Carry those observed facts to the centralized
        # halt writer instead of losing them with the stack frame.
        self.calls = calls
        self.friends_meta = list(friends_meta or [])
        self.downgrades = list(downgrades or [])
        self.successful_friend_ids = list(successful_friend_ids or [])
        self.theme_proposals = list(theme_proposals or [])
        self.produced_new_themes = produced_new_themes


@dataclass(frozen=True)
class MergeDecision:
    canonical: str
    duplicate: str
    rationale: str


def request_path(round_dir: Path) -> Path:
    return Path(round_dir) / REQUEST_NAME


def response_path(round_dir: Path) -> Path:
    return Path(round_dir) / RESPONSE_NAME


def write_request(round_dir: Path, run_id: str, round_no: int, claims: list[Claim]) -> Path:
    """Write the question. Returns the path, for the message that names it.

    Claims are rendered with the fields dedup actually needs and no others.
    `origin` is omitted: knowing that two friends raised a claim says nothing
    about whether two *texts* mean the same thing, and including it would
    invite merging by author rather than by content.
    """
    path = request_path(round_dir)
    payload = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "round": round_no,
        "question": QUESTION_MERGE,
        "instructions": _INSTRUCTIONS,
        "claims": [
            {
                "id": claim.id,
                "severity": claim.severity,
                "claim": claim.claim,
                "location": claim.location,
                "evidence": claim.evidence,
            }
            for claim in claims
        ],
        "merges": [],
    }
    secure_write_text(path, json.dumps(payload, indent=2, sort_keys=True))
    return path


def _load(path: Path) -> dict[str, Any]:
    return load_json_object(path, label="orchestrator response")


def read_response(
    round_dir: Path,
    known_ids: set[str],
    tolerate_duplicates: frozenset[str] = frozenset(),
    *,
    response_file: Path | None = None,
) -> list[MergeDecision]:
    """Parse and validate RESPONSE.json against the claims that exist.

    Every rule here exists because breaking it corrupts the alias graph in a
    way that only surfaces later, while someone is reading a report and
    wondering where a finding went:

    * An unknown id would produce an Alias pointing at nothing.
    * Merging a claim into itself would make it its own canonical.
    * A chain (A->B, B->C) leaves A pointing at a claim that is itself gone.
      Rejected rather than resolved, because resolving it silently would
      pick a canonical the orchestrator never actually chose.
    * The same duplicate twice would record two different fates for one claim.

    **`tolerate_duplicates` exists for exactly one caller and one moment: a
    `--resume` retrying a round whose RESPONSE.json was already partly
    applied before the process crashed.** `known_ids` is built from
    `canonical_claims`, which has already folded away every id a completed
    merge named as `duplicate` -- so re-validating the SAME response against
    the SAME file, unaware anything already happened, refused with "not a
    claim in this run" on precisely the merges the crashed attempt had
    already finished. That turned a transient crash into a run permanently
    unable to resume: every retry re-read the identical file and hit the
    identical refusal. A duplicate named here is skipped rather than
    validated -- the caller populates this from the ledger's own Alias
    records for the round being resumed, so a tolerated id is one this
    exact response is already known to have applied, not a guess.
    """
    path = response_path(round_dir) if response_file is None else Path(response_file)
    if not path.is_file():
        raise UsageError(
            f"no {RESPONSE_NAME} in {round_dir}. This run halted for merge "
            f"adjudication; write the file described in {REQUEST_NAME} and "
            "re-run with --resume."
        )
    data = _load(path)

    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise UsageError(
            f"{path}: unsupported version {version!r} (this build understands {SCHEMA_VERSION})"
        )

    raw = data.get("merges")
    if not isinstance(raw, list):
        raise UsageError(f"{path}: 'merges' must be an array (use [] to merge nothing)")

    decisions: list[MergeDecision] = []
    duplicates: set[str] = set()
    canonicals: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise UsageError(f"{path}: merges[{index}] is not an object")
        canonical = entry.get("canonical")
        duplicate = entry.get("duplicate")
        for field, value in (("canonical", canonical), ("duplicate", duplicate)):
            if not isinstance(value, str) or not value.strip():
                raise UsageError(f"{path}: merges[{index}].{field} missing or empty")
        assert isinstance(canonical, str) and isinstance(duplicate, str)
        if duplicate not in known_ids and duplicate in tolerate_duplicates:
            # Already applied by an earlier attempt at this exact round,
            # before it crashed. Not re-validated against `known_ids`
            # because it is, correctly, no longer there.
            continue
        for field, value in (("canonical", canonical), ("duplicate", duplicate)):
            if value not in known_ids:
                raise UsageError(
                    f"{path}: merges[{index}].{field} names {value!r}, which is "
                    "not a claim in this run"
                )
        if canonical == duplicate:
            raise UsageError(f"{path}: merges[{index}] merges {canonical!r} into itself")
        if duplicate in duplicates:
            raise UsageError(
                f"{path}: {duplicate!r} appears as a duplicate twice, which would "
                "record two different fates for one claim"
            )
        duplicates.add(duplicate)
        canonicals.add(canonical)
        rationale = entry.get("rationale")
        decisions.append(
            MergeDecision(
                canonical=canonical,
                duplicate=duplicate,
                rationale=(rationale if isinstance(rationale, str) and rationale.strip() else ""),
            )
        )

    chained = duplicates & canonicals
    if chained:
        raise UsageError(
            f"{path}: {sorted(chained)} appear as both a canonical and a duplicate. "
            "A chain leaves the first claim pointing at one that is itself merged "
            "away; name the final canonical directly instead."
        )
    return decisions


def apply_merges(
    claims: list[Claim], decisions: list[MergeDecision], round_no: int
) -> tuple[list[Claim], list[Alias]]:
    """Fold merge decisions into the claim list.

    Mirrors merge.exact_merge's contract on purpose: a merged-away claim's
    `origin` joins its canonical, so corroboration survives adjudicated
    merges exactly as it survives exact ones. Losing it here would be worse
    than in the exact path -- these are the merges that combine *differently
    worded* claims, which is precisely where independent agreement is
    strongest evidence.
    """
    by_id = {claim.id: claim for claim in claims}
    aliases: list[Alias] = []
    origins: dict[str, list[str]] = {c.id: list(c.origin) for c in claims}
    removed: set[str] = set()

    for decision in decisions:
        duplicate = by_id[decision.duplicate]
        for value in duplicate.origin:
            if value not in origins[decision.canonical]:
                origins[decision.canonical].append(value)
        removed.add(decision.duplicate)
        aliases.append(
            Alias(
                canonical=decision.canonical,
                duplicate=decision.duplicate,
                round=round_no,
                source="orchestrator",
                rationale=decision.rationale or "adjudicated by orchestrator",
            )
        )

    kept = [
        replace(c, origin=origins[c.id]) if origins[c.id] != list(c.origin) else c
        for c in claims
        if c.id not in removed
    ]
    return kept, aliases


_EXTRACT_INSTRUCTIONS = (
    "This friend produced output that could not be parsed into claims, and "
    "repair is a pure transformation with no model call (§14.2) -- so it "
    "stopped here rather than guessing. Read `raw` and fill in `findings` "
    "with what the friend actually claimed, using the same shape a friend "
    "returns: severity, claim, location, evidence, failure_scenario, "
    "suggested_fix. Extract only what is there; an empty list is the right "
    "answer if the output contains no real findings."
)


def write_extract_request(
    round_dir: Path, run_id: str, round_no: int, unparseable: list[dict[str, Any]]
) -> Path:
    """Ask for claims to be read out of unparseable friend output.

    Covers every friend in the round that could not be parsed, not one at a
    time: the whole round has already been dispatched by the time this is
    written, and halting per-friend would ask the same question repeatedly
    for output that is already sitting on disk.
    """
    path = request_path(round_dir)
    payload = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "round": round_no,
        "question": QUESTION_EXTRACT,
        "instructions": _EXTRACT_INSTRUCTIONS,
        "unparseable": [
            {"friend": e["friend"], "parse_errors": e["errors"], "raw": e["raw"], "findings": []}
            for e in unparseable
        ],
    }
    secure_write_text(path, json.dumps(payload, indent=2, sort_keys=True))
    return path


def read_extract_response(
    round_dir: Path, *, response_file: Path | None = None
) -> list[dict[str, Any]]:
    """Parse RESPONSE.json's `findings` and validate them as claims.

    Validated with the SAME contract a friend's own output goes through, not
    a looser one. An orchestrator is trusted to read, not to bypass the
    schema -- a hand-extracted claim missing `failure_scenario` is
    unsubstantiated for exactly the reasons §6.1 gives, whoever wrote it.
    """
    path = response_path(round_dir) if response_file is None else Path(response_file)
    if not path.is_file():
        raise UsageError(
            f"no {RESPONSE_NAME} in {round_dir}. This run halted for claim "
            f"extraction; fill in `findings` in {REQUEST_NAME}, save it as "
            f"{RESPONSE_NAME}, and re-run with --resume."
        )
    data = _load(path)
    if data.get("version") != SCHEMA_VERSION:
        raise UsageError(
            f"{path}: unsupported version {data.get('version')!r} "
            f"(this build understands {SCHEMA_VERSION})"
        )
    entries = data.get("unparseable")
    if not isinstance(entries, list):
        raise UsageError(f"{path}: 'unparseable' must be an array")
    extracted: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise UsageError(f"{path}: unparseable[{index}] is not an object")
        findings = entry.get("findings")
        if not isinstance(findings, list):
            raise UsageError(
                f"{path}: unparseable[{index}].findings must be an array "
                "(use [] if this output contains no real findings)"
            )
        errors = validate_payload({"findings": findings} if findings else {"no_findings": True})
        if errors:
            raise UsageError(
                f"{path}: unparseable[{index}] findings are not valid claims: {'; '.join(errors)}"
            )
        for finding in findings:
            extracted.append({"friend": entry.get("friend", "orchestrator"), **finding})
    return extracted

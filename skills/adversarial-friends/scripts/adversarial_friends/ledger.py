"""Append-only claim ledger.

The ledger is the durable record of what was claimed, who judged it, which
claims were merged, and how anything was resolved. It is append-only so a run
can be replayed and audited; nothing is ever rewritten in place.
"""
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterator, Union

from .errors import UsageError


@dataclass(frozen=True)
class Claim:
    id: str
    supersedes: str | None
    origin: list[str]
    lens: str
    round: int
    advisory: bool
    severity: str
    claim: str
    location: str | None
    evidence: str
    failure_scenario: str
    suggested_fix: str


@dataclass(frozen=True)
class Verdict:
    claim_id: str
    judge: str
    round: int
    verdict: str
    confidence: str
    evidence_assessment: str
    reasoning: str
    counter_evidence: str | None
    amended_claim: str | None


@dataclass(frozen=True)
class Alias:
    canonical: str
    duplicate: str
    round: int
    source: str
    rationale: str


@dataclass(frozen=True)
class Resolution:
    claim_id: str
    disposition: str
    author: str
    evidence: str
    round: int
    verified: str


Record = Union[Claim, Verdict, Alias, Resolution]

_TYPE_NAMES: dict[type, str] = {
    Claim: "claim", Verdict: "verdict", Alias: "alias", Resolution: "resolution",
}
_BY_NAME = {name: cls for cls, name in _TYPE_NAMES.items()}


def record_to_dict(record: Record) -> dict:
    payload = asdict(record)
    payload["type"] = _TYPE_NAMES[type(record)]
    return payload


def record_from_dict(payload: dict) -> Record:
    if not isinstance(payload, dict):
        raise UsageError(f"ledger record must be a dict, got {type(payload).__name__}")

    kind = payload.get("type")
    cls = _BY_NAME.get(kind)
    if cls is None:
        raise UsageError(f"unknown ledger record type: {kind!r}")

    known = {f.name for f in fields(cls)}
    # Ignore extra keys not in the dataclass — forward compatibility.
    filtered = {k: v for k, v in payload.items() if k in known}

    try:
        return cls(**filtered)
    except TypeError as e:
        raise UsageError(f"malformed {kind!r} record: {e}")


class Ledger:
    """A JSONL file of ledger records, read and written in append order."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Record) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record_to_dict(record), sort_keys=True) + "\n")

    def records(self) -> Iterator[Record]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield record_from_dict(json.loads(line))

    def claims(self) -> list[Claim]:
        return [r for r in self.records() if isinstance(r, Claim)]

    def aliases(self) -> list[Alias]:
        return [r for r in self.records() if isinstance(r, Alias)]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        # Exact match on the versioned id: a verdict on c-0001@1 says nothing
        # about c-0001@2, whose wording a judge may never have seen.
        return [r for r in self.records()
                if isinstance(r, Verdict) and r.claim_id == claim_id]

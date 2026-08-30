"""Append-only claim ledger.

The ledger is the durable record of what was claimed, who judged it, which
claims were merged, and how anything was resolved. It is append-only so a run
can be replayed and audited; nothing is ever rewritten in place.
"""

from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
from typing import Any, cast

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


Record = Claim | Verdict | Alias | Resolution

_TYPE_NAMES: dict[type, str] = {
    Claim: "claim",
    Verdict: "verdict",
    Alias: "alias",
    Resolution: "resolution",
}
_BY_NAME = {name: cls for cls, name in _TYPE_NAMES.items()}


def record_to_dict(record: Record) -> dict[str, Any]:
    payload = asdict(record)
    payload["type"] = _TYPE_NAMES[type(record)]
    return payload


def record_from_dict(payload: dict[str, Any]) -> Record:
    if not isinstance(payload, dict):
        raise UsageError(f"ledger record must be a dict, got {type(payload).__name__}")

    kind = payload.get("type")
    if not isinstance(kind, str):
        raise UsageError(f"ledger record has a missing or non-string 'type': {kind!r}")
    cls = _BY_NAME.get(kind)
    if cls is None:
        raise UsageError(f"unknown ledger record type: {kind!r}")

    known = {f.name for f in fields(cls)}
    # Ignore extra keys not in the dataclass — forward compatibility.
    filtered = {k: v for k, v in payload.items() if k in known}

    try:
        return cast(Record, cls(**filtered))
    except TypeError as e:
        raise UsageError(f"malformed {kind!r} record: {e}") from e


class Ledger:
    """A JSONL file of ledger records, read and written in append order."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Record) -> None:
        encoded = (json.dumps(record_to_dict(record), sort_keys=True) + "\n").encode("utf-8")
        created = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("ledger append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            parent_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def records(self) -> Iterator[Record]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise UsageError(
                        f"{self.path}:{line_no}: malformed JSON: {exc.msg}"
                    ) from exc
                try:
                    yield record_from_dict(payload)
                except UsageError as exc:
                    raise UsageError(f"{self.path}:{line_no}: {exc}") from exc

    def claims(self) -> list[Claim]:
        return [r for r in self.records() if isinstance(r, Claim)]

    def aliases(self) -> list[Alias]:
        return [r for r in self.records() if isinstance(r, Alias)]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        # Exact match on the versioned id: a verdict on c-0001@1 says nothing
        # about c-0001@2, whose wording a judge may never have seen.
        return [r for r in self.records() if isinstance(r, Verdict) and r.claim_id == claim_id]

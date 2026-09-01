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
from .outcomes import json_node_count
from .secureio import secure_mkdir, secure_open_append, secure_open_directory, secure_open_read


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

_FIELD_TYPES: dict[str, dict[str, tuple[type, ...]]] = {
    "claim": {
        "id": (str,),
        "supersedes": (str, type(None)),
        "origin": (list,),
        "lens": (str,),
        "round": (int,),
        "advisory": (bool,),
        "severity": (str,),
        "claim": (str,),
        "location": (str, type(None)),
        "evidence": (str,),
        "failure_scenario": (str,),
        "suggested_fix": (str,),
    },
    "verdict": {
        "claim_id": (str,),
        "judge": (str,),
        "round": (int,),
        "verdict": (str,),
        "confidence": (str,),
        "evidence_assessment": (str,),
        "reasoning": (str,),
        "counter_evidence": (str, type(None)),
        "amended_claim": (str, type(None)),
    },
    "alias": {
        "canonical": (str,),
        "duplicate": (str,),
        "round": (int,),
        "source": (str,),
        "rationale": (str,),
    },
    "resolution": {
        "claim_id": (str,),
        "disposition": (str,),
        "author": (str,),
        "evidence": (str,),
        "round": (int,),
        "verified": (str,),
    },
}

MAX_LEDGER_BYTES = 128 * 1024 * 1024
MAX_LEDGER_LINE_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024


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
    expected = known | {"type"}
    unexpected = set(payload) - expected
    if unexpected:
        raise UsageError(f"malformed {kind!r} record: unexpected keys {sorted(unexpected)}")
    missing = known - set(payload)
    if missing:
        raise UsageError(f"malformed {kind!r} record: missing keys {sorted(missing)}")
    filtered = {k: payload[k] for k in known}
    for field, expected_types in _FIELD_TYPES[kind].items():
        value = filtered[field]
        if type(value) not in expected_types:
            names = " or ".join(item.__name__ for item in expected_types)
            raise UsageError(
                f"malformed {kind!r} record: {field} must be {names}, got {type(value).__name__}"
            )
    if kind == "claim" and not all(type(value) is str for value in filtered["origin"]):
        raise UsageError("malformed 'claim' record: origin must contain only strings")

    try:
        return cast(Record, cls(**filtered))
    except TypeError as e:
        raise UsageError(f"malformed {kind!r} record: {e}") from e


class Ledger:
    """A JSONL file of ledger records, read and written in append order."""

    def __init__(self, path: Path, *, root: Path | None = None) -> None:
        self.path = Path(path)
        if root is None:
            secure_mkdir(self.path.parent, parents=True, exist_ok=True)
            self.root = self.path.parent
        else:
            self.root = Path(root)
            secure_mkdir(self.path.parent, parents=True, exist_ok=True, root=self.root)

    def append(self, record: Record) -> None:
        encoded = (json.dumps(record_to_dict(record), sort_keys=True) + "\n").encode("utf-8")
        fd = secure_open_append(self.path, root=self.root)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("ledger append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = secure_open_directory(self.path.parent, root=self.root)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def records(self) -> Iterator[Record]:
        try:
            descriptor = secure_open_read(self.path, root=self.root)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise UsageError(f"cannot read ledger {self.path}: {exc}") from exc
        try:
            info = os.fstat(descriptor)
            if info.st_size > MAX_LEDGER_BYTES:
                raise UsageError(
                    f"ledger {self.path} file exceeds the {MAX_LEDGER_BYTES}-byte limit"
                )
            for line_no, raw in enumerate(_bounded_lines(descriptor, self.path), start=1):
                if not raw.strip():
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise UsageError(f"{self.path}:{line_no}: ledger must be valid UTF-8") from exc
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                    detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                    raise UsageError(f"{self.path}:{line_no}: malformed JSON: {detail}") from exc
                try:
                    json_node_count(payload, f"ledger record {line_no}")
                except (RecursionError, TypeError, ValueError) as exc:
                    raise UsageError(f"{self.path}:{line_no}: JSON bounds exceeded: {exc}") from exc
                try:
                    yield record_from_dict(payload)
                except UsageError as exc:
                    raise UsageError(f"{self.path}:{line_no}: {exc}") from exc
        except OSError as exc:
            raise UsageError(f"cannot read ledger {self.path}: {exc}") from exc
        finally:
            os.close(descriptor)

    def claims(self) -> list[Claim]:
        return [r for r in self.records() if isinstance(r, Claim)]

    def aliases(self) -> list[Alias]:
        return [r for r in self.records() if isinstance(r, Alias)]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        # Exact match on the versioned id: a verdict on c-0001@1 says nothing
        # about c-0001@2, whose wording a judge may never have seen.
        return [r for r in self.records() if isinstance(r, Verdict) and r.claim_id == claim_id]


def _bounded_lines(descriptor: int, path: Path) -> Iterator[bytes]:
    """Yield bounded JSONL records without materializing the whole ledger."""
    pending = bytearray()
    total = 0
    line_no = 1
    while chunk := os.read(descriptor, _READ_CHUNK):
        total += len(chunk)
        if total > MAX_LEDGER_BYTES:
            raise UsageError(f"ledger {path} file exceeds the {MAX_LEDGER_BYTES}-byte limit")
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            segment = chunk[start:] if newline < 0 else chunk[start:newline]
            if len(pending) + len(segment) > MAX_LEDGER_LINE_BYTES:
                raise UsageError(
                    f"ledger {path} line {line_no} exceeds the {MAX_LEDGER_LINE_BYTES}-byte limit"
                )
            pending.extend(segment)
            if newline < 0:
                break
            yield bytes(pending)
            pending.clear()
            line_no += 1
            start = newline + 1
    if pending:
        yield bytes(pending)

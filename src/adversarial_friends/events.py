"""Safe, append-only lifecycle events stored beside a run.

The event stream is intentionally much less detailed than a run's audit
artifacts.  It tells a host what progressed without copying a prompt, model
answer, diagnostic, credential, or authority decision into another file.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Final

from .errors import UsageError
from .secureio import secure_open_append, secure_open_directory, secure_read_bytes

EVENT_SCHEMA_VERSION: Final = 1
MAX_EVENT_BYTES: Final = 4 * 1024
MAX_EVENT_LOG_BYTES: Final = 8 * 1024 * 1024
EVENT_TYPES: Final = frozenset(
    {"run_started", "friend_finished", "friend_failed", "round_finished", "run_finished"}
)
_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_RUN_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_UTC_RFC3339: Final = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
_STATUSES: Final = frozenset(
    {
        "started",
        "succeeded",
        "failed",
        "completed",
        "halted",
        "error",
        "incomplete",
        "blocked",
        "interrupted",
    }
)
_NEXT_ACTIONS: Final = frozenset(
    {"inspect_report", "resume", "resolve", "fix_configuration", "retry"}
)
_MODE_VALUES: Final = frozenset({"report", "crossexam", "gate", "loop"})
_FIELDS: Final[dict[str, frozenset[str]]] = {
    "run_started": frozenset({"mode", "profile", "status"}),
    "friend_finished": frozenset({"friend", "provider", "lens", "round", "duration_s", "status"}),
    "friend_failed": frozenset({"friend", "provider", "lens", "round", "duration_s", "status"}),
    "round_finished": frozenset({"round", "status"}),
    "run_finished": frozenset({"duration_s", "status", "next_action"}),
}
_REQUIRED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "run_started": frozenset({"mode", "profile", "status"}),
    "friend_finished": frozenset({"friend", "provider", "lens", "round", "duration_s", "status"}),
    "friend_failed": frozenset({"friend", "provider", "lens", "round", "duration_s", "status"}),
    "round_finished": frozenset({"round", "status"}),
    "run_finished": frozenset({"status", "next_action"}),
}


def _invalid(detail: str) -> UsageError:
    return UsageError(f"invalid lifecycle event: {detail}")


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise _invalid("run_id must be a bounded identifier")
    return value


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_RFC3339.fullmatch(value) is None:
        raise _invalid("timestamp must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid("timestamp must be RFC3339 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise _invalid("timestamp must be RFC3339 UTC")
    return value


def _validate_payload(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise _invalid(f"type must be one of {sorted(EVENT_TYPES)!r}")
    if not isinstance(payload, Mapping):
        raise _invalid("payload must be an object")
    data = dict(payload)
    fields = _FIELDS[event_type]
    unexpected = set(data) - fields
    if unexpected:
        raise _invalid(f"payload fields are not allowed: {sorted(unexpected)!r}")
    missing = _REQUIRED_FIELDS[event_type] - set(data)
    if missing:
        raise _invalid(f"payload fields are required: {sorted(missing)!r}")
    for name in ("friend", "provider", "lens", "profile"):
        if name not in data:
            continue
        value = data[name]
        if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
            raise _invalid(f"{name} must be a bounded identifier")
    if "mode" in data and (not isinstance(data["mode"], str) or data["mode"] not in _MODE_VALUES):
        raise _invalid(f"mode must be one of {sorted(_MODE_VALUES)!r}")
    if "status" in data and (
        not isinstance(data["status"], str) or data["status"] not in _STATUSES
    ):
        raise _invalid(f"status must be one of {sorted(_STATUSES)!r}")
    if "next_action" in data and (
        not isinstance(data["next_action"], str) or data["next_action"] not in _NEXT_ACTIONS
    ):
        raise _invalid(f"next_action must be one of {sorted(_NEXT_ACTIONS)!r}")
    if "round" in data and (
        isinstance(data["round"], bool) or not isinstance(data["round"], int) or data["round"] < 1
    ):
        raise _invalid("round must be a positive integer")
    if "duration_s" in data:
        duration = data["duration_s"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            raise _invalid("duration_s must be a finite non-negative number")
        data["duration_s"] = round(float(duration), 3)
    return data


@dataclass(frozen=True)
class EventRecord:
    """One small, schema-checked lifecycle record."""

    run_id: str
    type: str
    payload: Mapping[str, object]
    timestamp: str
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != EVENT_SCHEMA_VERSION
        ):
            raise _invalid(f"schema_version must be {EVENT_SCHEMA_VERSION}")
        object.__setattr__(self, "run_id", _validate_run_id(self.run_id))
        object.__setattr__(self, "timestamp", _validate_timestamp(self.timestamp))
        object.__setattr__(
            self, "payload", MappingProxyType(_validate_payload(self.type, self.payload))
        )

    @classmethod
    def create(
        cls,
        event_type: str,
        payload: Mapping[str, object],
        *,
        run_id: str,
        timestamp: str | None = None,
    ) -> "EventRecord":
        created_at = timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return cls(run_id, event_type, payload, created_at)

    @classmethod
    def from_dict(cls, value: object) -> "EventRecord":
        keys = {"schema_version", "timestamp", "run_id", "type", "payload"}
        if not isinstance(value, dict) or set(value) != keys:
            raise _invalid(f"record keys must be exactly {sorted(keys)!r}")
        schema_version = value["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != EVENT_SCHEMA_VERSION
        ):
            raise _invalid(f"schema_version must be {EVENT_SCHEMA_VERSION}")
        event_type = value["type"]
        if not isinstance(event_type, str):
            raise _invalid("type must be a string")
        return cls.create(
            event_type,
            value["payload"],
            run_id=value["run_id"],
            timestamp=value["timestamp"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "type": self.type,
            "payload": dict(self.payload),
        }


@dataclass
class EventWriter:
    """A synchronized writer for a private JSONL event stream."""

    path: Path
    root: Path
    run_id: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.run_id = _validate_run_id(self.run_id)

    def append(self, event: EventRecord) -> None:
        if not isinstance(event, EventRecord):
            raise TypeError("event writer accepts EventRecord instances only")
        if event.run_id != self.run_id:
            raise _invalid("event run_id does not match its writer")
        line = (
            json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if len(line) > MAX_EVENT_BYTES:
            raise _invalid(f"record is too long (limit {MAX_EVENT_BYTES} bytes)")
        with self._lock:
            descriptor = secure_open_append(self.path, root=self.root)
            try:
                view = memoryview(line)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("event append made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory = secure_open_directory(self.path.parent, root=self.root)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)


def read_events(path: Path, *, root: Path) -> list[EventRecord]:
    """Read complete records, tolerating only a final unterminated tail."""
    try:
        payload = secure_read_bytes(path, root=root, max_bytes=MAX_EVENT_LOG_BYTES)
    except FileNotFoundError:
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path}: invalid UTF-8 lifecycle event stream") from exc
    complete = text.splitlines(keepends=True)
    if complete and not complete[-1].endswith("\n"):
        complete.pop()
    records: list[EventRecord] = []
    for line_no, raw_line in enumerate(complete, start=1):
        try:
            value = json.loads(raw_line)
            records.append(EventRecord.from_dict(value))
        except (json.JSONDecodeError, UsageError, ValueError, TypeError) as exc:
            raise UsageError(f"{path.name} line {line_no}: {exc}") from exc
    return records

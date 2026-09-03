"""Read-only inspection and bounded watching of persisted runs."""

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from ..errors import UsageError
from ..events import EventRecord, read_events
from ..jsonio import MAX_JSON_FILE_BYTES, decode_json_object
from ..ledger import Claim, Resolution, record_from_dict
from ..runstore import default_root
from ..secureio import secure_open_directory, secure_read_bytes

STATUS_SCHEMA_VERSION = 1
_POLL_S = 0.25
_MAX_LEDGER_BYTES = 128 * 1024 * 1024


def _as_root(value: str | None) -> Path:
    """Return the caller-selected root without creating or resolving it."""
    return (Path(value) if value else default_root()).absolute()


def _open_directory(path: Path, *, root: Path) -> None:
    try:
        descriptor = secure_open_directory(path, root=root)
    except OSError as exc:
        raise UsageError(f"cannot inspect run directory {path}: {exc}") from exc
    os.close(descriptor)


def find_run(run_id_or_path: str, out: str | None) -> tuple[Path, Path]:
    """Find a run without accepting paths outside the selected run root."""
    root = _as_root(out)
    try:
        _open_directory(root, root=root)
    except UsageError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise UsageError(
                f"no such run: {run_id_or_path!r} (run root {root} does not exist)"
            ) from exc
        raise
    supplied = Path(run_id_or_path)
    if supplied.is_absolute() or len(supplied.parts) != 1 or supplied.name in {"", ".", ".."}:
        candidate = supplied.absolute()
    else:
        candidate = root / supplied.name
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UsageError(f"run path {candidate} is outside the run root {root}") from exc
    try:
        _open_directory(candidate, root=root)
    except UsageError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise UsageError(
                f"no such run: {run_id_or_path!r} (looked in {root}). Pass the run directory "
                "that afriend run printed, or --out if it was written elsewhere."
            ) from exc
        raise
    return candidate, root


def _read_json(path: Path, *, root: Path, label: str) -> dict[str, Any]:
    try:
        payload = secure_read_bytes(path, root=root, max_bytes=MAX_JSON_FILE_BYTES)
    except FileNotFoundError as exc:
        raise UsageError(f"{path.parent} is not a run directory: no {path.name}") from exc
    except OSError as exc:
        raise UsageError(f"cannot read {label} {path}: {exc}") from exc
    return decode_json_object(payload, path=path, label=label)


def _read_ledger(path: Path, *, root: Path) -> list[Claim | Resolution]:
    """Read the two record types status needs without constructing a Ledger.

    ``Ledger`` deliberately initializes its parent for writers, which is the
    wrong abstraction here: a status command must not chmod or create any
    caller-owned run artifact merely to inspect it.
    """
    try:
        payload = secure_read_bytes(path, root=root, max_bytes=_MAX_LEDGER_BYTES)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UsageError(f"cannot read ledger {path}: {exc}") from exc
    records: list[Claim | Resolution] = []
    for line_no, raw in enumerate(payload.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
            record = record_from_dict(parsed)
        except (json.JSONDecodeError, UsageError, TypeError, ValueError) as exc:
            raise UsageError(f"{path}:{line_no}: invalid ledger record: {exc}") from exc
        if isinstance(record, (Claim, Resolution)):
            records.append(record)
    return records


def _claim_counts(records: Iterable[Claim | Resolution]) -> dict[str, object]:
    claims: dict[str, Claim] = {}
    resolutions: dict[str, Resolution] = {}
    for record in records:
        if isinstance(record, Claim):
            claims[record.id] = record
        else:
            resolutions[record.claim_id] = record
    counts = Counter(
        resolutions[claim_id].disposition if claim_id in resolutions else "pending"
        for claim_id in claims
    )
    return {"total": len(claims), "by_status": dict(sorted(counts.items()))}


def _friends(meta: dict[str, Any]) -> dict[str, int]:
    rows = meta.get("friends")
    if not isinstance(rows, list):
        return {"total": 0, "finished": 0, "failed": 0}
    statuses = [row.get("status") for row in rows if isinstance(row, dict)]
    failed = sum(
        isinstance(value, str) and value.lower().startswith("failed") for value in statuses
    )
    finished = sum(isinstance(value, str) and bool(value) for value in statuses)
    return {"total": len(rows), "finished": finished, "failed": failed}


def _state(meta: dict[str, Any], events: list[EventRecord]) -> tuple[str, str | None, str | None]:
    finished = next((event for event in reversed(events) if event.type == "run_finished"), None)
    if finished is not None:
        status = finished.payload.get("status")
        action = finished.payload.get("next_action")
        return (
            "terminal",
            status if isinstance(status, str) else None,
            action if isinstance(action, str) else None,
        )
    lifecycle = meta.get("lifecycle_state")
    if lifecycle == "terminal":
        return "terminal", None, None
    if isinstance(lifecycle, str) and ("waiting" in lifecycle or "halt" in lifecycle):
        return "halted", None, "resume"
    if lifecycle == "running" or events:
        return "live", None, None
    return "legacy", None, None


def _next_action(
    state: str, reported: str | None, *, mode: str | None, claims: dict[str, object]
) -> str:
    if reported is not None:
        return reported
    if state == "halted":
        return "resume"
    by_status = claims["by_status"]
    if (
        state == "terminal"
        and mode == "gate"
        and isinstance(by_status, dict)
        and by_status.get("pending")
    ):
        return "resolve"
    if state == "live":
        return "watch"
    return "inspect_report"


def summarize(run_dir: Path, *, root: Path) -> dict[str, object]:
    """Reconstruct a stable status schema entirely from existing artifacts."""
    meta = _read_json(run_dir / "run.json", root=root, label="saved run metadata")
    events = read_events(run_dir / "events.jsonl", root=root)
    claims = _claim_counts(_read_ledger(run_dir / "claims.jsonl", root=root))
    mode = meta.get("mode") if isinstance(meta.get("mode"), str) else None
    profile = meta.get("profile") if isinstance(meta.get("profile"), str) else None
    state, outcome, reported_action = _state(meta, events)
    downgrades = meta.get("downgrades")
    return {
        "version": STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "path": str(run_dir),
        "state": state,
        "outcome": outcome,
        "mode": mode,
        "profile": profile,
        "claims": claims,
        "friends": _friends(meta),
        "downgrades": list(downgrades)
        if isinstance(downgrades, list) and all(isinstance(item, str) for item in downgrades)
        else [],
        "next_action": _next_action(state, reported_action, mode=mode, claims=claims),
    }


def watch_events(
    path: Path,
    *,
    root: Path,
    poll_s: float = _POLL_S,
    snapshots: Iterable[str] | None = None,
    start_at_end: bool = False,
) -> Iterator[EventRecord]:
    """Yield each complete event once and stop at ``run_finished``.

    The optional snapshots seam is solely for deterministic tests of a writer
    completing a torn tail. Production always rereads the bounded event log.
    """
    if poll_s < 0:
        raise ValueError("poll_s must be non-negative")
    emitted = 0
    initial = True
    source = iter(snapshots) if snapshots is not None else None
    while True:
        if source is None:
            events = read_events(path, root=root)
        else:
            try:
                snapshot = next(source)
            except StopIteration:
                return
            temporary = path.with_name(f".{path.name}.status-snapshot")
            # Parse the supplied snapshot through the exact event validator
            # without writing the run: a tiny local decoder mirrors
            # read_events' only permitted recovery (an incomplete final line).
            lines = snapshot.splitlines(keepends=True)
            if lines and not lines[-1].endswith("\n"):
                lines.pop()
            events = []
            for line_no, line in enumerate(lines, start=1):
                try:
                    events.append(EventRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, UsageError, TypeError, ValueError) as exc:
                    raise UsageError(f"{temporary.name} line {line_no}: {exc}") from exc
        if initial and start_at_end:
            if any(event.type == "run_finished" for event in events):
                return
            emitted = len(events)
        initial = False
        for event in events[emitted:]:
            yield event
            if event.type == "run_finished":
                return
        emitted = max(emitted, len(events))
        time.sleep(poll_s)


def _render(summary: dict[str, object]) -> str:
    outcome = f" ({summary['outcome']})" if summary["outcome"] else ""
    claims = summary["claims"]
    assert isinstance(claims, dict)
    by_status = claims["by_status"]
    assert isinstance(by_status, dict)
    claim_text = ", ".join(f"{name}={count}" for name, count in by_status.items()) or "none"
    lines = [
        f"{summary['run_id']}: {summary['state']}{outcome}",
        f"mode: {summary['mode'] or 'unknown'}  profile: {summary['profile'] or 'legacy'}",
        f"claims: {claims['total']} ({claim_text})",
        f"next: {summary['next_action']}",
    ]
    downgrades = summary["downgrades"]
    if isinstance(downgrades, list) and downgrades:
        lines.append("downgrades:")
        lines.extend(f"  - {item}" for item in downgrades)
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    run_dir, root = find_run(args.run_id, getattr(args, "out", None))
    summary = summarize(run_dir, root=root)
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_render(summary))
    if getattr(args, "watch", False) and summary["state"] == "live":
        for event in watch_events(run_dir / "events.jsonl", root=root, start_at_end=True):
            print(f"afriend: {event.type} {dict(event.payload)}", file=sys.stderr)
    return 0

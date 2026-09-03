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
from ..ids import FRIEND_NAME_RE
from ..jsonio import MAX_JSON_FILE_BYTES, decode_json_object
from ..ledger import Claim, Resolution, record_from_dict
from ..reviewcompleteness import from_friends
from ..runstore import default_root
from ..secureio import secure_open_directory, secure_read_bytes, secure_regular_exists

STATUS_SCHEMA_VERSION = 2
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


def _read_optional_json(path: Path, *, root: Path, label: str) -> tuple[dict[str, Any], bool]:
    """A run can expose events before its initial metadata checkpoint."""
    try:
        return _read_json(path, root=root, label=label), True
    except UsageError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return {}, False
        raise


def _read_events(path: Path, *, root: Path) -> list[EventRecord]:
    """Keep unreadable telemetry in the command's normal error boundary."""
    try:
        return read_events(path, root=root)
    except OSError as exc:
        raise UsageError(f"cannot read lifecycle events {path}: {exc}") from exc


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


def _roster_rows(meta: dict[str, Any]) -> dict[str, dict[str, object]]:
    """Project only the fixed, safe roster fields into a status response."""
    roster = meta.get("roster")
    if not isinstance(roster, list):
        return {}
    rows: dict[str, dict[str, object]] = {}
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        name, provider, scope = entry.get("name"), entry.get("cli"), entry.get("scope")
        if (
            not isinstance(name, str)
            or FRIEND_NAME_RE.fullmatch(name) is None
            or not isinstance(provider, str)
            or FRIEND_NAME_RE.fullmatch(provider) is None
            or scope not in {"doc", "repo"}
        ):
            continue
        rows[name] = {
            "name": name,
            "provider": provider,
            "scope": scope,
            "round": 0,
            "status": "pending",
        }
    return rows


def _metadata_status(value: object) -> str:
    """Map diagnostic-bearing legacy statuses to a small safe vocabulary."""
    if not isinstance(value, str):
        return "pending"
    normalized = value.lower()
    if (
        normalized == "ok"
        or normalized.startswith("ok ")
        or normalized in {"succeeded", "completed"}
    ):
        return "succeeded"
    if normalized == "failed" or normalized.startswith("failed:"):
        return "failed"
    if normalized == "skipped" or normalized.startswith("skipped:"):
        return "skipped"
    return "pending"


def _metadata_rows(meta: dict[str, Any], rows: dict[str, dict[str, object]]) -> None:
    """Overlay validated historical friend rows without exposing diagnostics."""
    saved = meta.get("friends")
    if not isinstance(saved, list):
        return
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        round_no = entry.get("round")
        if (
            not isinstance(name, str)
            or FRIEND_NAME_RE.fullmatch(name) is None
            or type(round_no) is not int
            or round_no < 1
        ):
            continue
        prior = rows.get(name)
        scope = entry.get("scope")
        if scope not in {"doc", "repo"}:
            scope = prior["scope"] if prior is not None else "unknown"
        provider = entry.get("provider", entry.get("cli"))
        if not isinstance(provider, str) or FRIEND_NAME_RE.fullmatch(provider) is None:
            provider = prior["provider"] if prior is not None else "unknown"
        status = _metadata_status(entry.get("status"))
        if prior is not None:
            prior_round = prior["round"]
            assert isinstance(prior_round, int)
            if round_no < prior_round:
                continue
        rows[name] = {
            "name": name,
            "provider": provider,
            "scope": scope,
            "round": round_no,
            "status": status,
        }


def _friends(meta: dict[str, Any], events: Iterable[EventRecord]) -> dict[str, object]:
    rows = _roster_rows(meta)
    _metadata_rows(meta, rows)
    for event in events:
        if event.type not in {"friend_finished", "friend_failed"}:
            continue
        payload = event.payload
        name = payload["friend"]
        provider = payload["provider"]
        round_no = payload["round"]
        event_status = payload["status"]
        assert isinstance(name, str)
        assert isinstance(provider, str)
        assert isinstance(round_no, int)
        assert isinstance(event_status, str)
        row = rows.setdefault(
            name,
            {
                "name": name,
                "provider": provider,
                "scope": "unknown",
                "round": 0,
                "status": "pending",
            },
        )
        prior_round = row["round"]
        assert isinstance(prior_round, int)
        row["round"] = max(prior_round, round_no)
        row["status"] = event_status
    ordered = [rows[name] for name in sorted(rows)]
    failed = sum(row["status"] == "failed" for row in ordered)
    finished = sum(row["status"] in {"succeeded", "failed"} for row in ordered)
    return {"total": len(ordered), "finished": finished, "failed": failed, "rows": ordered}


def _rounds(
    meta: dict[str, Any], events: Iterable[EventRecord], state: str
) -> dict[str, int | None]:
    saved = meta.get("rounds_run")
    saved_rounds = saved if type(saved) is int and saved >= 0 else 0
    observed: list[int] = []
    for event in events:
        if event.type not in {"friend_finished", "friend_failed", "round_finished"}:
            continue
        round_no = event.payload["round"]
        assert isinstance(round_no, int)
        observed.append(round_no)
    current = max([saved_rounds, *observed], default=0)
    return {"current": current, "final": current if state in {"terminal", "halted"} else None}


def _latest_invocation(events: list[EventRecord]) -> list[EventRecord]:
    """Discard completed earlier attempts when a halted run has resumed."""
    for index in range(len(events) - 1, -1, -1):
        if events[index].type == "run_started":
            return events[index:]
    return events


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
    if events:
        return "live", None, None
    lifecycle = meta.get("lifecycle_state")
    if lifecycle == "terminal":
        return "terminal", None, None
    if isinstance(lifecycle, str) and ("waiting" in lifecycle or "halt" in lifecycle):
        return "halted", None, "resume"
    if lifecycle == "running":
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
    meta, has_metadata = _read_optional_json(
        run_dir / "run.json", root=root, label="saved run metadata"
    )
    events = _latest_invocation(_read_events(run_dir / "events.jsonl", root=root))
    if not has_metadata and not events:
        raise UsageError(f"{run_dir} is not a run directory: no run.json or valid lifecycle events")
    claims = _claim_counts(_read_ledger(run_dir / "claims.jsonl", root=root))
    started = next((event for event in events if event.type == "run_started"), None)
    mode = meta.get("mode") if isinstance(meta.get("mode"), str) else None
    profile = meta.get("profile") if isinstance(meta.get("profile"), str) else None
    if started is not None:
        if isinstance(started.payload.get("mode"), str):
            mode = started.payload["mode"]
        if isinstance(started.payload.get("profile"), str):
            profile = started.payload["profile"]
    state, outcome, reported_action = _state(meta, events)
    downgrades = meta.get("downgrades")
    friends = _friends(meta, events)
    saved_friends = meta.get("friends")
    review_completeness = from_friends(saved_friends if isinstance(saved_friends, list) else [])
    rows = friends["rows"]
    assert isinstance(rows, list)
    started_scope = started.payload.get("scope") if started is not None else None
    if isinstance(started_scope, str) and started_scope in {"doc", "repo"}:
        scope = started_scope
    elif rows:
        scopes = {row.get("scope") for row in rows if isinstance(row, dict)}
        scope = "repo" if "repo" in scopes else "doc" if "doc" in scopes else "unknown"
    else:
        scope = "unknown"
    return {
        "version": STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "path": str(run_dir),
        "state": state,
        "outcome": outcome,
        "mode": mode,
        "profile": profile,
        "claims": claims,
        "scope": scope,
        "rounds": _rounds(meta, events, state),
        "friends": friends,
        "review_completeness": review_completeness,
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
            events = _read_events(path, root=root)
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
            if any(event.type == "run_finished" for event in _latest_invocation(events)):
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
        f"mode: {summary['mode'] or 'unknown'}  profile: {summary['profile'] or 'legacy'}  scope: {summary['scope']}",
        f"claims: {claims['total']} ({claim_text})",
        f"next: {summary['next_action']}",
    ]
    rounds = summary["rounds"]
    if isinstance(rounds, dict):
        lines.append(f"rounds: current={rounds['current']} final={rounds['final']}")
    friends = summary["friends"]
    if isinstance(friends, dict) and isinstance(friends.get("rows"), list):
        for row in friends["rows"]:
            if isinstance(row, dict) and row.get("status") in {"succeeded", "failed"}:
                lines.append(
                    f"friend: {row['name']} {row['status']} scope={row['scope']} round={row['round']}"
                )
    review_completeness = summary["review_completeness"]
    if isinstance(review_completeness, dict):
        message = review_completeness.get("message")
        if isinstance(message, str):
            lines.append(f"review completeness: {message}")
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
    events_path = run_dir / "events.jsonl"
    if getattr(args, "watch", False) and not secure_regular_exists(events_path, root=root):
        print("afriend: live events unavailable; status cannot watch this run.", file=sys.stderr)
    elif getattr(args, "watch", False) and summary["state"] == "live":
        for event in watch_events(events_path, root=root, start_at_end=True):
            print(f"afriend: {event.type} {dict(event.payload)}", file=sys.stderr)
    return 0

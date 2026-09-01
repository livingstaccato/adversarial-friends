"""Strict normalization for attacker-editable resume checkpoint metadata."""

from pathlib import Path
from typing import Any

from ..dispatch import STDERR_TAIL_CHARS, _stderr_tail, failure_summary
from ..errors import UsageError
from ..jsonio import MAX_JSON_FILE_BYTES, decode_json_object
from ..orchestrator import QUESTION_EXTRACT, QUESTION_MERGE, REQUEST_NAME, RESPONSE_NAME
from ..outcomes import MAX_JSON_SAFE_INTEGER
from ..secureio import secure_read_bytes, secure_regular_exists
from ..snapshots import SnapshotIdentity, history_from_meta
from ..verdicts import CONTESTED, INCOMPLETE, TERMINAL_STATES, UNPROVEN

_SUCCESS_STATUSES = frozenset({"ok", "ok [orphans suspected]"})
_OPTIONAL_STRINGS = frozenset(
    {
        "transport",
        "declared_scope",
        "scope",
        "external_tool_policy",
        "external_tools",
        "diagnostics",
        "diagnostics_path",
    }
)
_OPTIONAL_BOOLS = frozenset({"write_protected", "readonly", "os_confined"})
_OPTIONAL_STRING_LISTS = frozenset({"external_tool_sources", "deny_external_tools_argv"})
_CLAIM_STATES = TERMINAL_STATES | {CONTESTED, UNPROVEN, INCOMPLETE}
MAX_AUDIT_TEXT_CHARS = 8192


def _friend_error(index: int, detail: str) -> UsageError:
    return UsageError(f"cannot resume: saved friends[{index}] {detail}")


def _success_status(status: str) -> bool:
    return status in _SUCCESS_STATUSES or status.startswith("ok (diagnostics: ")


def _validate_status(index: int, row: dict[str, Any], status: str) -> None:
    """Validate status even when an attacker removes its supporting fields.

    Safe legacy rows use the same compact ``ok``/``failed: reason`` grammar;
    current rows additionally carry an exact diagnostics/path pair. Merely
    omitting that pair must never disable validation of the remaining text.
    """
    has_diagnostics = "diagnostics" in row or "diagnostics_path" in row
    orphan_suffix = " [orphans suspected]"
    orphan = status.endswith(orphan_suffix)
    body = status.removesuffix(orphan_suffix) if orphan else status

    if not has_diagnostics:
        if body.startswith("ok (diagnostics: "):
            raise _friend_error(index, "status has no bounded diagnostic summary fields")
        if body == "ok":
            return
        if body.startswith("failed: "):
            payload = body[len("failed: ") :]
            legacy_marker = " (stderr: "
            legacy_suffix = f"; full text in round-{row['round']}/{row['name']}.err)"
            if legacy_marker in payload:
                before_suffix, separator, trailing = payload.rpartition(legacy_suffix)
                if not separator or trailing:
                    raise _friend_error(index, "legacy diagnostic reference is malformed")
                reason, marker, legacy_diagnostics = before_suffix.partition(legacy_marker)
                if not marker or not reason or failure_summary(reason) != reason:
                    raise _friend_error(index, "failure reason is not a bounded sanitized summary")
                if (
                    not legacy_diagnostics
                    or len(legacy_diagnostics) > STDERR_TAIL_CHARS
                    or _stderr_tail(legacy_diagnostics) != legacy_diagnostics
                ):
                    raise _friend_error(
                        index, "legacy diagnostics is not a bounded sanitized summary"
                    )
                return
            reason = payload
            if not reason or failure_summary(reason) != reason:
                raise _friend_error(index, "failure reason is not a bounded sanitized summary")
            return
        if body.startswith("skipped: "):
            reason = body[len("skipped: ") :]
            if not reason or _stderr_tail(reason) != reason:
                raise _friend_error(index, "skip reason is not a bounded sanitized summary")
            return
        raise _friend_error(index, "status is not a recognized friend result")

    diagnostics = row.get("diagnostics")
    path = row.get("diagnostics_path")
    if type(diagnostics) is not str or len(diagnostics) > STDERR_TAIL_CHARS:
        raise _friend_error(index, "diagnostics must be a bounded string")
    if diagnostics and _stderr_tail(diagnostics) != diagnostics:
        raise _friend_error(index, "diagnostics is not a sanitized summary")
    if status.startswith("ok (diagnostics: ") and not diagnostics:
        raise _friend_error(index, "status has no bounded diagnostic summary")
    expected_path = f"round-{row['round']}/{row['name']}.err"
    if type(path) is not str or path != expected_path:
        raise _friend_error(index, "diagnostics_path does not match the friend capture")
    suffix = orphan_suffix if orphan else ""
    if body == "ok" or body.startswith("ok (diagnostics: "):
        expected = "ok"
        if diagnostics:
            expected += f" (diagnostics: {diagnostics}; full text in {path})"
        if status != expected + suffix:
            raise _friend_error(index, "status disagrees with its diagnostic summary")
        return
    if body.startswith("failed: "):
        reason_and_diagnostics = body[len("failed: ") :]
        reason = reason_and_diagnostics.split(" (stderr: ", 1)[0]
        if not reason or failure_summary(reason) != reason:
            raise _friend_error(index, "failure reason is not a bounded sanitized summary")
        expected = f"failed: {reason}"
        if diagnostics:
            expected += f" (stderr: {diagnostics}; full text in {path})"
        if status != expected + suffix:
            raise _friend_error(index, "status disagrees with its diagnostic summary")
        return
    raise _friend_error(index, "diagnostic fields are unsupported for this status")


def normalize_friend_rows(value: object, roster_names: set[str]) -> list[dict[str, Any]]:
    """Validate every saved row before it can reach resume logic or report rendering."""
    if type(value) is not list:
        raise UsageError("cannot resume: saved friends must be a list")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if type(raw) is not dict:
            raise _friend_error(index, "must be an object")
        row = dict(raw)
        name = row.get("name")
        if type(name) is not str or not name:
            raise _friend_error(index, "name must be a nonempty string")
        if name not in roster_names:
            raise _friend_error(index, "name is outside the frozen roster")
        round_no = row.get("round")
        if type(round_no) is not int or not 1 <= round_no <= MAX_JSON_SAFE_INTEGER:
            raise _friend_error(index, "round must be a positive integer")
        status = row.get("status")
        if type(status) is not str or not (
            _success_status(status)
            or (status.startswith("failed: ") and len(status) > len("failed: "))
            or (status.startswith("skipped: ") and len(status) > len("skipped: "))
        ):
            raise _friend_error(index, "status is not a recognized friend result")
        for field in ("model", "effort"):
            if field not in row or (row[field] is not None and type(row[field]) is not str):
                raise _friend_error(index, f"{field} must be a string or null")
        for field in _OPTIONAL_STRINGS:
            if field in row and type(row[field]) is not str:
                raise _friend_error(index, f"{field} must be a string")
        for field in _OPTIONAL_BOOLS:
            if field in row and type(row[field]) is not bool:
                raise _friend_error(index, f"{field} must be a boolean")
        for field in _OPTIONAL_STRING_LISTS:
            item = row.get(field, [])
            if type(item) is not list or any(type(entry) is not str for entry in item):
                raise _friend_error(index, f"{field} must be a list of strings")
        if (
            "write_protected" in row
            and "readonly" in row
            and row["write_protected"] != row["readonly"]
        ):
            raise _friend_error(index, "has ambiguous write-protection fields")
        if "declared_scope" in row and "scope" in row and row["declared_scope"] != row["scope"]:
            raise _friend_error(index, "has ambiguous scope fields")
        _validate_status(index, row, status)
        normalized.append(row)
    return normalized


def legacy_successful_friend_ids(rows: list[dict[str, Any]], critique_round: int) -> list[str]:
    """Recover quorum only from the pending iteration's completed critique round."""
    if rows and not any(row["round"] == critique_round for row in rows):
        raise UsageError("cannot resume: saved friends have no rows for the pending critique round")
    status_by_name: dict[str, str] = {}
    ordered_names: list[str] = []
    for row in rows:
        if row["round"] != critique_round:
            continue
        name = row["name"]
        status = row["status"]
        prior = status_by_name.get(name)
        if prior is not None and prior != status:
            raise UsageError(
                "cannot resume: saved friends contain ambiguous duplicate statuses "
                f"for {name!r} in critique round {critique_round}"
            )
        if name not in status_by_name:
            ordered_names.append(name)
        status_by_name[name] = status
    return [name for name in ordered_names if _success_status(status_by_name[name])]


def normalize_resume_report_state(meta: dict[str, Any]) -> dict[str, object]:
    """Validate saved values consumed by carry-over and its eventual report."""
    normalized: dict[str, object] = {}
    if "downgrades" in meta:
        downgrades = meta["downgrades"]
        if type(downgrades) is not list or any(
            type(note) is not str or len(note) > MAX_AUDIT_TEXT_CHARS for note in downgrades
        ):
            raise UsageError("cannot resume: saved downgrades must be a bounded list of strings")
        seen: set[str] = set()
        unique: list[str] = []
        for note in downgrades:
            if note not in seen:
                seen.add(note)
                unique.append(note)
        normalized["downgrades"] = unique
    if "claim_states" in meta:
        states = meta["claim_states"]
        if type(states) is not dict or any(
            type(claim_id) is not str
            or not claim_id
            or type(state) is not str
            or state not in _CLAIM_STATES
            for claim_id, state in states.items()
        ):
            raise UsageError(
                "cannot resume: saved claim_states must map claim ids to recognized states"
            )
        normalized["claim_states"] = dict(states)
    if "amendment_notes" in meta:
        notes = meta["amendment_notes"]
        if type(notes) is not list or any(type(note) is not str for note in notes):
            raise UsageError("cannot resume: saved amendment_notes must be a list of strings")
        normalized["amendment_notes"] = list(notes)
    for field in ("incomplete", "halted_round_dry", "halted_round_failed"):
        if field in meta:
            if type(meta[field]) is not bool:
                raise UsageError(f"cannot resume: saved {field} must be a boolean")
            normalized[field] = meta[field]
    return normalized


def normalize_repeat_tracker(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) - {"last", "count", "disabled"}:
        raise UsageError("cannot resume: saved repeat_tracker has an invalid shape")
    normalized: dict[str, object] = {}
    for section in ("last", "disabled"):
        entries = value.get(section, {})
        if type(entries) is not dict or any(
            type(key) is not str or type(item) is not str for key, item in entries.items()
        ):
            raise UsageError(
                f"cannot resume: saved repeat_tracker.{section} must map strings to strings"
            )
        normalized[section] = dict(entries)
    counts = value.get("count", {})
    if type(counts) is not dict or any(
        type(key) is not str or type(item) is not int or not 0 <= item <= MAX_JSON_SAFE_INTEGER
        for key, item in counts.items()
    ):
        raise UsageError(
            "cannot resume: saved repeat_tracker.count must map strings to nonnegative integers"
        )
    normalized["count"] = dict(counts)
    return normalized


def _legacy_halt_is_outstanding(meta: dict[str, Any], run_dir: Path) -> bool:
    saved = meta.get("invocation")
    if type(saved) is not dict:
        return False
    iteration = meta.get("resume_iteration", meta.get("iterations_run", 1))
    max_rounds = saved.get("max_rounds", 1)
    if type(iteration) is not int or iteration < 1 or type(max_rounds) is not int:
        return False
    round_dir = run_dir / f"round-{(iteration - 1) * max_rounds + 1}"
    request_path = round_dir / REQUEST_NAME
    try:
        request_exists = secure_regular_exists(request_path, root=run_dir)
        applied_exists = secure_regular_exists(round_dir / f"{RESPONSE_NAME}.applied", root=run_dir)
    except OSError:
        return False
    if not request_exists or applied_exists:
        return False
    try:
        payload = secure_read_bytes(request_path, root=run_dir, max_bytes=MAX_JSON_FILE_BYTES)
        request = decode_json_object(payload, path=request_path, label="orchestrator request")
    except (OSError, UsageError):
        return False
    return type(request) is dict and request.get("question") in {QUESTION_MERGE, QUESTION_EXTRACT}


def validate_lifecycle_and_snapshot(meta: dict[str, Any], *, run_dir: Path, legacy: bool) -> None:
    lifecycle = meta.get("lifecycle_state")
    if not legacy and lifecycle not in {
        "waiting-for-orchestrator",
        "response-applying",
        "response-applied",
    }:
        raise UsageError(
            "cannot resume: saved lifecycle_state must be waiting-for-orchestrator "
            "or a response-applying/response-applied recovery state"
        )
    if legacy and lifecycle not in (None, "waiting-for-orchestrator"):
        raise UsageError("cannot resume: saved lifecycle_state must be waiting-for-orchestrator")
    if legacy and not _legacy_halt_is_outstanding(meta, run_dir):
        raise UsageError("cannot resume: legacy metadata has no outstanding orchestrator halt")
    current = SnapshotIdentity.from_current_meta(meta)
    history_from_meta(meta, current)

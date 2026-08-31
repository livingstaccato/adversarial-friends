"""Strict normalization for attacker-editable resume checkpoint metadata."""

from typing import Any

from ..dispatch import STDERR_TAIL_CHARS, _stderr_tail
from ..errors import UsageError
from ..outcomes import MAX_JSON_SAFE_INTEGER
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


def _friend_error(index: int, detail: str) -> UsageError:
    return UsageError(f"cannot resume: saved friends[{index}] {detail}")


def _success_status(status: str) -> bool:
    return status in _SUCCESS_STATUSES or status.startswith("ok (diagnostics: ")


def _validate_diagnostics(index: int, row: dict[str, Any], status: str) -> None:
    if "diagnostics" not in row and "diagnostics_path" not in row:
        if status.startswith("ok (diagnostics: "):
            raise _friend_error(index, "status has no bounded diagnostic summary fields")
        return
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
    if diagnostics and (diagnostics not in status or path not in status):
        raise _friend_error(index, "status disagrees with its diagnostic summary")


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
        _validate_diagnostics(index, row, status)
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

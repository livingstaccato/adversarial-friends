"""One immutable projection from observed facts to terminal run state.

Precedence is deliberately centralized and ordered: interruption, runtime
failure, an explicit budget ceiling, natural loop exhaustion, authentication
abort, incomplete/quorum failure, gate blockers, then ordinary completion.
The runner observes facts; this module alone gives them terminal meaning.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, cast

_MODES = frozenset({"report", "crossexam", "gate", "loop"})
_UTC_RFC3339 = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)\Z")

# JSON metadata is consumed across runtimes that commonly use IEEE-754 numbers.
# This bound preserves every accepted integer exactly and avoids Python's
# implementation-dependent integer-to-string digit limit during serialization.
MAX_JSON_SAFE_INTEGER = (1 << 53) - 1
# A deterministic nesting limit keeps validation comfortably below Python's
# recursion limit while leaving ample room for run metadata and checkpoints.
MAX_JSON_DEPTH = 64
# Expanded traversal work, not merely distinct container identities. A
# repeated acyclic DAG is valid JSON input, but expanding it by value can
# otherwise grow exponentially before the depth limit engages. 8,192 keeps
# ordinary metadata (including the deliberately wide tracker tests) ample
# while making the work bound deterministic in every supported Python.
MAX_JSON_NODES = 8_192
# One scalar remains small enough to diagnose and copy safely; the aggregate
# bound keeps a wide 8,192-node object from multiplying that allowance. Both
# are proportional to the 32 MiB captured-output ceiling.
MAX_JSON_STRING_BYTES = 4 * 1024 * 1024
MAX_JSON_SCALAR_BYTES = 32 * 1024 * 1024


def json_node_count(value: object, path: str = "metadata") -> int:
    """Validate JSON-safe structure and return its expanded node count."""
    nodes = [0]
    _freeze_json(value, path, set(), 0, nodes, [0])
    return nodes[0]


class StopReason(StrEnum):
    COMPLETED = "completed"
    GATE_BLOCKED = "gate-blocked"
    MAX_LOOP_ITERATIONS = "max-loop-iterations"
    MAX_CALLS = "max-calls"
    MAX_WALL_CLOCK = "max-wall-clock"
    AUTH_ABORT = "auth-abort"
    INCOMPLETE = "incomplete"
    INTERRUPTED = "interrupted"
    RUNTIME_ERROR = "runtime-error"


def _freeze_json(
    value: object,
    path: str,
    active: set[int],
    depth: int,
    nodes: list[int],
    scalar_bytes: list[int],
) -> object:
    """Freeze canonical JSON, duplicating repeated acyclic containers by value."""
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES:
        raise ValueError(f"{path} exceeds the maximum expanded JSON node count")
    value_type = type(value)
    if value_type is str:
        _count_scalar_bytes(cast(str, value), path, scalar_bytes)
        return value
    if value is None or value_type is bool:
        scalar_bytes[0] += 4 if value is None or value is True else 5
        _check_scalar_total(path, scalar_bytes)
        return value
    if value_type is int:
        integer = cast(int, value)
        if not -MAX_JSON_SAFE_INTEGER <= integer <= MAX_JSON_SAFE_INTEGER:
            raise ValueError(f"{path} integer is outside the interoperable JSON range")
        scalar_bytes[0] += len(str(integer))
        _check_scalar_total(path, scalar_bytes)
        return integer
    if value_type is float:
        number = cast(float, value)
        if not math.isfinite(number):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        scalar_bytes[0] += len(repr(number))
        _check_scalar_total(path, scalar_bytes)
        return number
    if value_type is not dict and value_type is not list and value_type is not tuple:
        raise ValueError(f"{path} contains unsupported value type {value_type.__name__}")
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{path} exceeds the maximum JSON container depth")

    identity = id(value)
    if identity in active:
        raise ValueError(f"{path} contains a cyclic JSON container")
    active.add(identity)
    try:
        if value_type is dict:
            frozen: dict[str, object] = {}
            for key, item in cast(dict[object, object], value).items():
                if type(key) is not str:
                    raise ValueError(f"{path} must contain only exact string mapping keys")
                _count_scalar_bytes(key, f"{path} key", scalar_bytes)
                frozen[key] = _freeze_json(
                    item, f"{path}.{key}", active, depth + 1, nodes, scalar_bytes
                )
            return MappingProxyType(frozen)
        items = cast(list[object] | tuple[object, ...], value)
        return tuple(
            _freeze_json(item, f"{path}[{index}]", active, depth + 1, nodes, scalar_bytes)
            for index, item in enumerate(items)
        )
    finally:
        active.remove(identity)


def _check_scalar_total(path: str, scalar_bytes: list[int]) -> None:
    if scalar_bytes[0] > MAX_JSON_SCALAR_BYTES:
        raise ValueError(f"{path} exceeds the aggregate scalar byte limit")


def _count_scalar_bytes(value: str, path: str, scalar_bytes: list[int]) -> None:
    if len(value) > MAX_JSON_STRING_BYTES:
        raise ValueError(f"{path} exceeds the per-string byte limit")
    size = len(value.encode("utf-8"))
    if size > MAX_JSON_STRING_BYTES:
        raise ValueError(f"{path} exceeds the per-string byte limit")
    scalar_bytes[0] += size
    _check_scalar_total(path, scalar_bytes)


def _freeze_json_mapping(value: object, path: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{path} must be an exact built-in dict")
    frozen = _freeze_json(value, path, set(), 0, [0], [0])
    if type(frozen) is not MappingProxyType:  # pragma: no cover - freeze invariant
        raise ValueError(f"{path} could not be frozen as a JSON mapping")
    return cast(Mapping[str, object], frozen)


def _thaw_json(
    value: object,
    path: str,
    active: set[int] | None = None,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> object:
    visited = [0] if nodes is None else nodes
    visited[0] += 1
    if visited[0] > MAX_JSON_NODES:
        raise ValueError(f"{path} frozen snapshot exceeds the maximum expanded JSON node count")
    value_type = type(value)
    if value is None or value_type is bool or value_type is str:
        return value
    if value_type is int:
        integer = cast(int, value)
        if not -MAX_JSON_SAFE_INTEGER <= integer <= MAX_JSON_SAFE_INTEGER:
            raise ValueError(
                f"{path} frozen snapshot integer is outside the interoperable JSON range"
            )
        return integer
    if value_type is float:
        number = cast(float, value)
        if not math.isfinite(number):
            raise ValueError(f"{path} frozen snapshot contains a nonfinite number")
        return number
    if value_type is not MappingProxyType and value_type is not tuple:
        raise ValueError(
            f"{path} frozen snapshot contains unsupported value type {value_type.__name__}"
        )
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{path} frozen snapshot exceeds the maximum JSON container depth")

    active_ids = set() if active is None else active
    identity = id(value)
    if identity in active_ids:
        raise ValueError(f"{path} frozen snapshot contains a cyclic container")
    active_ids.add(identity)
    try:
        if value_type is MappingProxyType:
            thawed: dict[str, object] = {}
            for key, item in cast(Mapping[object, object], value).items():
                if type(key) is not str:
                    raise ValueError(f"{path} frozen snapshot contains a non-string key")
                thawed[key] = _thaw_json(item, f"{path}.{key}", active_ids, depth + 1, visited)
            return thawed
        return [
            _thaw_json(item, f"{path}[{index}]", active_ids, depth + 1, visited)
            for index, item in enumerate(cast(tuple[object, ...], value))
        ]
    finally:
        active_ids.remove(identity)


def _utc_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str) or _UTC_RFC3339.fullmatch(value) is None:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp")
    return parsed


def _nonnegative_count(name: str, value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_JSON_SAFE_INTEGER:
        raise ValueError(f"{name} must be a nonnegative interoperable JSON integer")


def _finite_duration(value: object) -> float:
    if type(value) is int:
        number: int | float = value
    elif type(value) is float:
        number = value
    else:
        raise ValueError("duration_s must be a finite nonnegative number")
    if number < 0:
        raise ValueError("duration_s must be a finite nonnegative number")
    try:
        normalized = float(number)
    except (OverflowError, ValueError) as exc:
        raise ValueError("duration_s must be a finite nonnegative number") from exc
    if not math.isfinite(normalized):
        raise ValueError("duration_s must be a finite nonnegative number")
    return normalized


def _require_bool(name: str, value: object) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _stable_blocker_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError("blocking_ids must be an ordered sequence")
    blockers: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("blocking_ids must contain nonempty strings")
        if item not in seen:
            seen.add(item)
            blockers.append(item)
    return tuple(blockers)


@dataclass(frozen=True)
class RunOutcome:
    started_at: str
    finished_at: str
    duration_s: float
    stop_reason: StopReason
    exit_code: int
    converged: bool
    gate_decision: str | None
    blocker_ids: tuple[str, ...]
    ceiling_hit: str | None
    attempted_calls: int
    spent_calls: int
    iterations_run: int
    rounds_run: int
    dry_streak: int
    repeat_tracker: Mapping[str, object]

    def __post_init__(self) -> None:
        _utc_timestamp("started_at", self.started_at)
        _utc_timestamp("finished_at", self.finished_at)
        object.__setattr__(self, "duration_s", _finite_duration(self.duration_s))
        if not isinstance(self.stop_reason, StopReason):
            raise ValueError("stop_reason must be a StopReason")
        _nonnegative_count("exit_code", self.exit_code)
        _require_bool("converged", self.converged)
        if self.gate_decision not in {None, "blocked", "clear"}:
            raise ValueError("gate_decision must be blocked, clear, or null")
        if type(self.blocker_ids) is not tuple or any(
            not isinstance(item, str) or not item for item in self.blocker_ids
        ):
            raise ValueError("blocker_ids must be a tuple of nonempty strings")
        if self.ceiling_hit not in {None, "max-calls", "max-wall-clock", "max-loop-iterations"}:
            raise ValueError("ceiling_hit must identify a supported ceiling or be null")
        for name in (
            "attempted_calls",
            "spent_calls",
            "iterations_run",
            "rounds_run",
            "dry_streak",
        ):
            _nonnegative_count(name, getattr(self, name))
        object.__setattr__(
            self,
            "repeat_tracker",
            _freeze_json_mapping(self.repeat_tracker, "repeat_tracker"),
        )

    def apply(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Return a terminal metadata copy without mutating either input."""
        applied = cast(
            dict[str, Any],
            _thaw_json(_freeze_json_mapping(meta, "meta"), "meta"),
        )
        applied.update(
            {
                "schema_version": 2,
                "lifecycle_state": "terminal",
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_s": self.duration_s,
                "stop_reason": self.stop_reason.value,
                "exit_code": self.exit_code,
                "converged": self.converged,
                "gate_decision": self.gate_decision,
                "gate_blocked": self.gate_decision == "blocked",
                "gate_blocking_claims": list(self.blocker_ids),
                "ceiling_hit": self.ceiling_hit,
                "attempted_calls": self.attempted_calls,
                "spent_calls": self.spent_calls,
                "iterations_run": self.iterations_run,
                "rounds_run": self.rounds_run,
                "dry_streak": self.dry_streak,
                "repeat_tracker": _thaw_json(self.repeat_tracker, "repeat_tracker"),
            }
        )
        return applied


def terminal_outcome(
    *,
    mode: str,
    converged: bool,
    loop_exhausted: bool,
    budget_reason: str | None,
    blocking_ids: Sequence[str],
    any_success: bool,
    unresolved: bool,
    auth_abort: bool = False,
    abort_signum: int | None = None,
    runtime_error: bool = False,
    quorum_failed: bool = False,
    started_at: str = "1970-01-01T00:00:00Z",
    finished_at: str = "1970-01-01T00:00:00Z",
    duration_s: float = 0.0,
    attempted_calls: int = 0,
    spent_calls: int = 0,
    iterations_run: int = 0,
    rounds_run: int = 0,
    dry_streak: int = 0,
    repeat_tracker: Mapping[str, object] | None = None,
) -> RunOutcome:
    """Project already-observed facts into the authoritative terminal state."""
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("mode must be report, crossexam, gate, or loop")
    for name, value in (
        ("converged", converged),
        ("loop_exhausted", loop_exhausted),
        ("any_success", any_success),
        ("unresolved", unresolved),
        ("auth_abort", auth_abort),
        ("runtime_error", runtime_error),
        ("quorum_failed", quorum_failed),
    ):
        _require_bool(name, value)
    if budget_reason is not None and not isinstance(budget_reason, str):
        raise ValueError("budget_reason must be a supported ceiling description or null")
    if abort_signum is not None and (
        type(abort_signum) is not int
        or abort_signum <= 0
        or abort_signum > MAX_JSON_SAFE_INTEGER - 128
    ):
        raise ValueError("abort_signum must be a positive integer")

    blocker_tuple = _stable_blocker_ids(blocking_ids)
    gate_decision = None if mode != "gate" else ("blocked" if blocker_tuple else "clear")

    if abort_signum is not None:
        reason, exit_code, ceiling = StopReason.INTERRUPTED, 128 + abort_signum, None
    elif runtime_error:
        reason, exit_code, ceiling = StopReason.RUNTIME_ERROR, 1, None
    elif budget_reason is not None:
        if "max-wall-clock" in budget_reason:
            reason = StopReason.MAX_WALL_CLOCK
        elif "max-calls" in budget_reason:
            reason = StopReason.MAX_CALLS
        else:
            raise ValueError("budget_reason must identify max-calls or max-wall-clock")
        exit_code, ceiling = 11, reason.value
    elif mode == "loop" and loop_exhausted and not converged:
        reason, exit_code, ceiling = (
            StopReason.MAX_LOOP_ITERATIONS,
            11,
            StopReason.MAX_LOOP_ITERATIONS.value,
        )
    elif auth_abort:
        reason, exit_code, ceiling = StopReason.AUTH_ABORT, 1, None
    elif not any_success:
        reason, exit_code, ceiling = StopReason.INCOMPLETE, 1, None
    elif quorum_failed:
        reason, exit_code, ceiling = StopReason.INCOMPLETE, 12, None
    elif unresolved:
        reason, exit_code, ceiling = StopReason.INCOMPLETE, 1, None
    elif mode == "gate" and blocker_tuple:
        reason, exit_code, ceiling = StopReason.GATE_BLOCKED, 1, None
    else:
        reason, exit_code, ceiling = StopReason.COMPLETED, 0, None

    return RunOutcome(
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        stop_reason=reason,
        exit_code=exit_code,
        converged=converged,
        gate_decision=gate_decision,
        blocker_ids=blocker_tuple,
        ceiling_hit=ceiling,
        attempted_calls=attempted_calls,
        spent_calls=spent_calls,
        iterations_run=iterations_run,
        rounds_run=rounds_run,
        dry_streak=dry_streak,
        repeat_tracker={} if repeat_tracker is None else repeat_tracker,
    )

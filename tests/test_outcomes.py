"""The one pure projection from observed run facts to terminal state."""

from collections import UserDict, UserList
from dataclasses import FrozenInstanceError
from decimal import Decimal
import json
import sys
from types import MappingProxyType

import pytest

from afriend import outcomes as outcomes_module
from afriend.outcomes import (
    StopReason,
    terminal_outcome,
)

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 8_192
MAX_JSON_SAFE_INTEGER = (1 << 53) - 1

BASE = {
    "mode": "report",
    "converged": False,
    "loop_exhausted": False,
    "budget_reason": None,
    "blocking_ids": [],
    "any_success": True,
    "unresolved": False,
}


class IntSubclass(int):
    pass


class FloatSubclass(float):
    pass


class StringSubclass(str):
    pass


class DictSubclass(dict):
    pass


class ListSubclass(list):
    pass


class TupleSubclass(tuple):
    pass


class FloatableNumber:
    def __float__(self):
        return 1.0


class BoolLike:
    def __bool__(self):
        return True


def nested_json(kind, depth):
    value = "leaf"
    for _ in range(depth):
        if kind == "dict":
            value = {"child": value}
        elif kind == "list":
            value = [value]
        else:
            value = (value,)
    return {"value": value}


def frozen_json(kind, depth):
    value = "leaf"
    for _ in range(depth):
        value = MappingProxyType({"child": value}) if kind == "mapping" else (value,)
    return MappingProxyType({"value": value})


def shared_binary_json(depth):
    value = "leaf"
    for _ in range(depth):
        value = [value, value]
    return {"value": value}


def frozen_shared_binary_json(depth):
    value = "leaf"
    for _ in range(depth):
        value = (value, value)
    return MappingProxyType({"value": value})


def outcome(**facts):
    return terminal_outcome(**{**BASE, **facts})


def test_json_safety_bounds_are_explicit_public_contracts():
    assert getattr(outcomes_module, "MAX_JSON_DEPTH", None) == MAX_JSON_DEPTH
    assert getattr(outcomes_module, "MAX_JSON_NODES", None) == MAX_JSON_NODES
    assert getattr(outcomes_module, "MAX_JSON_SAFE_INTEGER", None) == MAX_JSON_SAFE_INTEGER


@pytest.mark.parametrize(
    ("facts", "reason", "exit_code", "ceiling"),
    [
        ({}, StopReason.COMPLETED, 0, None),
        (
            {"mode": "gate", "blocking_ids": ["c-0002@1"]},
            StopReason.GATE_BLOCKED,
            1,
            None,
        ),
        (
            {"mode": "loop", "loop_exhausted": True},
            StopReason.MAX_LOOP_ITERATIONS,
            11,
            "max-loop-iterations",
        ),
        (
            {"budget_reason": "--max-calls=4 reached before round 3"},
            StopReason.MAX_CALLS,
            11,
            "max-calls",
        ),
        (
            {"budget_reason": "--max-wall-clock reached before iteration 2"},
            StopReason.MAX_WALL_CLOCK,
            11,
            "max-wall-clock",
        ),
        ({"auth_abort": True}, StopReason.AUTH_ABORT, 1, None),
        ({"any_success": False}, StopReason.INCOMPLETE, 1, None),
        ({"abort_signum": 15}, StopReason.INTERRUPTED, 143, None),
        ({"runtime_error": True}, StopReason.RUNTIME_ERROR, 1, None),
    ],
)
def test_every_stop_reason_is_reachable(facts, reason, exit_code, ceiling):
    got = outcome(**facts)
    assert got.stop_reason == reason
    assert got.exit_code == exit_code
    assert got.ceiling_hit == ceiling


def test_iteration_exhaustion_is_a_ceiling_exit():
    got = terminal_outcome(
        mode="loop",
        converged=False,
        loop_exhausted=True,
        budget_reason=None,
        blocking_ids=[],
        any_success=True,
        unresolved=False,
    )
    assert got.stop_reason == StopReason.MAX_LOOP_ITERATIONS
    assert got.ceiling_hit == "max-loop-iterations"
    assert got.exit_code == 11


def test_gate_blockers_are_part_of_the_outcome():
    got = terminal_outcome(
        mode="gate",
        converged=True,
        loop_exhausted=False,
        budget_reason=None,
        blocking_ids=["c-0002@1"],
        any_success=True,
        unresolved=False,
    )
    assert got.gate_decision == "blocked"
    assert got.blocker_ids == ("c-0002@1",)
    assert got.exit_code == 1


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (
            {
                "abort_signum": 2,
                "runtime_error": True,
                "budget_reason": "--max-calls reached",
                "loop_exhausted": True,
                "auth_abort": True,
                "any_success": False,
                "mode": "gate",
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.INTERRUPTED,
        ),
        (
            {
                "runtime_error": True,
                "budget_reason": "--max-calls reached",
                "loop_exhausted": True,
                "auth_abort": True,
                "any_success": False,
                "mode": "gate",
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.RUNTIME_ERROR,
        ),
        (
            {
                "budget_reason": "--max-wall-clock reached",
                "loop_exhausted": True,
                "auth_abort": True,
                "any_success": False,
                "mode": "gate",
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.MAX_WALL_CLOCK,
        ),
        (
            {
                "mode": "loop",
                "loop_exhausted": True,
                "auth_abort": True,
                "any_success": False,
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.MAX_LOOP_ITERATIONS,
        ),
        (
            {
                "auth_abort": True,
                "any_success": False,
                "mode": "gate",
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.AUTH_ABORT,
        ),
        (
            {
                "any_success": False,
                "mode": "gate",
                "blocking_ids": ["c-0001@1"],
            },
            StopReason.INCOMPLETE,
        ),
        (
            {"mode": "gate", "blocking_ids": ["c-0001@1"]},
            StopReason.GATE_BLOCKED,
        ),
    ],
)
def test_overlapping_facts_follow_the_documented_precedence(facts, reason):
    assert outcome(**facts).stop_reason == reason


def test_completed_loop_is_not_reclassified_as_exhausted():
    got = outcome(mode="loop", converged=True, loop_exhausted=True)
    assert got.stop_reason == StopReason.COMPLETED
    assert got.ceiling_hit is None


def test_blocker_ids_are_deduplicated_in_stable_observed_order():
    got = outcome(mode="gate", blocking_ids=["c-0002@1", "c-0001@1", "c-0002@1"])
    assert got.blocker_ids == ("c-0002@1", "c-0001@1")


def test_outcome_is_frozen_and_tracker_input_is_copied():
    tracker = {"count": {"codex-ops": 2}}
    got = outcome(repeat_tracker=tracker)
    tracker["count"]["codex-ops"] = 99
    assert got.repeat_tracker["count"]["codex-ops"] == 2
    with pytest.raises(FrozenInstanceError):
        got.exit_code = 99
    with pytest.raises(TypeError):
        got.repeat_tracker["new"] = "value"


def test_repeat_tracker_is_a_deeply_immutable_snapshot():
    tracker = {"nested": {"items": [{"score": 1}]}}
    got = outcome(repeat_tracker=tracker)
    tracker["nested"]["items"][0]["score"] = 99

    nested = got.repeat_tracker["nested"]
    assert nested["items"][0]["score"] == 1
    with pytest.raises(TypeError):
        nested["new"] = "value"
    with pytest.raises(TypeError):
        nested["items"][0]["score"] = 2


def test_repeat_tracker_snapshot_and_apply_use_only_canonical_container_types():
    got = outcome(repeat_tracker={"nested": {"items": [1, (True, None)]}})
    frozen_nested = got.repeat_tracker["nested"]

    assert type(got.repeat_tracker) is MappingProxyType
    assert type(frozen_nested) is MappingProxyType
    assert type(frozen_nested["items"]) is tuple
    assert type(frozen_nested["items"][1]) is tuple

    applied = got.apply({})
    assert type(applied["repeat_tracker"]) is dict
    assert type(applied["repeat_tracker"]["nested"]) is dict
    assert type(applied["repeat_tracker"]["nested"]["items"]) is list
    assert type(applied["repeat_tracker"]["nested"]["items"][1]) is list


def test_apply_returns_fresh_deterministic_json_structures():
    got = outcome(repeat_tracker={"values": (None, True, 1, 1.5, "ok", {"n": 2})})

    first = got.apply({"artifact": "spec.md", "nested": {"items": [1]}})
    second = got.apply({"artifact": "spec.md", "nested": {"items": [1]}})

    assert first == second
    assert first["repeat_tracker"] is not second["repeat_tracker"]
    assert first["repeat_tracker"]["values"] == [None, True, 1, 1.5, "ok", {"n": 2}]
    json.dumps(first, allow_nan=False)
    first["repeat_tracker"]["values"][-1]["n"] = 99
    first["nested"]["items"].append(2)
    assert second["repeat_tracker"]["values"][-1]["n"] == 2
    assert second["nested"]["items"] == [1]
    assert got.repeat_tracker["values"][-1]["n"] == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        -MAX_JSON_SAFE_INTEGER,
        0,
        MAX_JSON_SAFE_INTEGER,
        -1.25,
        sys.float_info.max,
        "text",
        {"nested": 1},
        [1, "two"],
        (True, None),
    ],
)
def test_every_accepted_tracker_value_serializes_with_stdlib_json(value):
    applied = outcome(repeat_tracker={"value": value}).apply({})
    json.dumps(applied, allow_nan=False)


def test_apply_returns_a_copy_with_plain_json_safe_state():
    base = {"artifact": "spec.md"}
    got = outcome(
        started_at="2026-08-31T10:00:00Z",
        finished_at="2026-08-31T10:00:01Z",
        duration_s=1.0,
        attempted_calls=3,
        spent_calls=2,
        iterations_run=1,
        rounds_run=2,
        dry_streak=0,
        repeat_tracker={"count": {"codex-ops": 2}},
    )
    applied = got.apply(base)
    assert applied is not base
    assert base == {"artifact": "spec.md"}
    assert applied["schema_version"] == 2
    assert applied["stop_reason"] == "completed"
    assert applied["lifecycle_state"] == "terminal"
    assert applied["exit_code"] == 0
    assert applied["attempted_calls"] == 3
    assert applied["spent_calls"] == 2
    assert applied["repeat_tracker"] == {"count": {"codex-ops": 2}}
    applied["repeat_tracker"]["count"]["codex-ops"] = 7
    assert got.repeat_tracker["count"]["codex-ops"] == 2


def test_terminal_meta_keeps_checkpoint_spend_and_tracker():
    checkpoint = {
        "spent_calls": 7,
        "repeat_tracker": {
            "last": {"codex-ops": "1:exit 1"},
            "count": {"codex-ops": 2},
            "disabled": {"codex-ops": "exit 1"},
        },
    }
    got = outcome(
        attempted_calls=7,
        spent_calls=checkpoint["spent_calls"],
        repeat_tracker=checkpoint["repeat_tracker"],
    )
    meta = got.apply(checkpoint)
    assert meta["spent_calls"] == checkpoint["spent_calls"]
    assert meta["repeat_tracker"] == checkpoint["repeat_tracker"]


@pytest.mark.parametrize(
    "field",
    ["attempted_calls", "spent_calls", "iterations_run", "rounds_run", "dry_streak"],
)
@pytest.mark.parametrize("value", [1.5, True, "1", -1])
def test_counts_require_exact_nonnegative_integers(field, value):
    with pytest.raises(ValueError, match=field):
        outcome(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["attempted_calls", "spent_calls", "iterations_run", "rounds_run", "dry_streak"],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(MAX_JSON_SAFE_INTEGER + 1, id="one-over-safe-integer"),
        pytest.param(10**10000, id="integer-string-limit"),
    ],
)
def test_counts_must_fit_the_canonical_json_integer_range(field, value):
    with pytest.raises(ValueError, match=field):
        outcome(**{field: value})


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(10**10000, id="huge-positive-int"),
        pytest.param(-(10**10000), id="huge-negative-int"),
        pytest.param(Decimal("1.25"), id="decimal"),
        pytest.param(FloatableNumber(), id="custom-floatable"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(IntSubclass(1), id="int-subclass"),
        pytest.param(FloatSubclass(1.0), id="float-subclass"),
        pytest.param(True, id="bool"),
        pytest.param("1", id="string"),
        pytest.param(-0.1, id="negative-float"),
    ],
)
def test_duration_requires_an_exact_safely_representable_builtin_real(value):
    with pytest.raises(ValueError, match="duration_s"):
        outcome(duration_s=value)


@pytest.mark.parametrize("value", [0, 1, 1.25, sys.float_info.max, 10**300])
def test_duration_normalizes_accepted_builtin_reals_to_finite_float(value):
    got = outcome(duration_s=value)
    assert type(got.duration_s) is float
    assert got.duration_s >= 0
    assert got.duration_s != float("inf")


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        [],
        "not-a-timestamp",
        "2026-08-31T10:00:00",
        "2026-08-31 10:00:00Z",
        "2026-08-31T10:00:00+01:00",
    ],
)
def test_timestamps_require_utc_rfc3339_strings(field, value):
    with pytest.raises(ValueError, match=field):
        outcome(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "converged",
        "loop_exhausted",
        "any_success",
        "unresolved",
        "auth_abort",
        "runtime_error",
        "quorum_failed",
    ],
)
@pytest.mark.parametrize("value", [None, 0, "false"])
def test_observed_boolean_facts_require_exact_booleans(field, value):
    with pytest.raises(ValueError, match=field):
        outcome(**{field: value})


@pytest.mark.parametrize("value", [None, 1, "unknown"])
def test_mode_must_be_a_known_mode(value):
    with pytest.raises(ValueError, match="mode"):
        outcome(mode=value)


@pytest.mark.parametrize("value", [1, [], "unknown ceiling"])
def test_budget_reason_is_validated_at_the_boundary(value):
    with pytest.raises(ValueError, match="budget_reason"):
        outcome(budget_reason=value)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "2"])
def test_abort_signum_requires_a_positive_integer(value):
    with pytest.raises(ValueError, match="abort_signum"):
        outcome(abort_signum=value)


def test_abort_signum_must_leave_room_for_the_signal_exit_offset():
    with pytest.raises(ValueError, match="abort_signum"):
        outcome(abort_signum=MAX_JSON_SAFE_INTEGER)


@pytest.mark.parametrize("value", ["c-0001@1", 1, {"c-0001@1"}, {"id": "c-0001@1"}])
def test_blocking_ids_require_an_ordered_sequence(value):
    with pytest.raises(ValueError, match="blocking_ids"):
        outcome(blocking_ids=value)


@pytest.mark.parametrize("value", [[1], [""], [["c-0001@1"]]])
def test_blocking_ids_require_nonempty_string_members(value):
    with pytest.raises(ValueError, match="blocking_ids"):
        outcome(blocking_ids=value)


class UnsupportedTrackerValue:
    pass


@pytest.mark.parametrize(
    "value",
    [
        {"bad": {1, 2}},
        {"bad": frozenset({1, 2})},
        {"bad": UnsupportedTrackerValue()},
        {1: "non-string key"},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": float("-inf")},
    ],
)
def test_repeat_tracker_rejects_values_outside_the_json_domain(value):
    with pytest.raises(ValueError, match="repeat_tracker"):
        outcome(repeat_tracker=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(MAX_JSON_SAFE_INTEGER + 1, id="positive-one-over"),
        pytest.param(-(MAX_JSON_SAFE_INTEGER + 1), id="negative-one-over"),
        pytest.param(10**10000, id="integer-string-limit"),
    ],
)
def test_repeat_tracker_rejects_integers_outside_the_interoperable_json_range(value):
    with pytest.raises(ValueError, match=r"repeat_tracker.*integer"):
        outcome(repeat_tracker={"value": value})


@pytest.mark.parametrize("value", [[], (), "tracker", 1, False, {1, 2}])
def test_repeat_tracker_requires_a_mapping(value):
    with pytest.raises(ValueError, match="repeat_tracker"):
        outcome(repeat_tracker=value)


@pytest.mark.parametrize(
    "value",
    [
        DictSubclass({"value": 1}),
        UserDict({"value": 1}),
    ],
)
def test_repeat_tracker_requires_an_exact_builtin_dict(value):
    with pytest.raises(ValueError, match="repeat_tracker"):
        outcome(repeat_tracker=value)


@pytest.mark.parametrize(
    "value",
    [
        {"bad": DictSubclass({"value": 1})},
        {"bad": ListSubclass([1])},
        {"bad": TupleSubclass((1,))},
        {"bad": StringSubclass("value")},
        {"bad": IntSubclass(1)},
        {"bad": FloatSubclass(1.0)},
        {"bad": UserDict({"value": 1})},
        {"bad": UserList([1])},
        {"bad": BoolLike()},
        {StringSubclass("bad-key"): "value"},
    ],
)
def test_repeat_tracker_rejects_container_scalar_and_key_subclasses(value):
    with pytest.raises(ValueError, match="repeat_tracker"):
        outcome(repeat_tracker=value)


def test_repeat_tracker_rejects_a_direct_cycle_contextually():
    tracker = {}
    tracker["self"] = tracker
    with pytest.raises(ValueError, match=r"repeat_tracker.*cyclic"):
        outcome(repeat_tracker=tracker)


def test_repeat_tracker_rejects_an_indirect_cycle_contextually():
    items = []
    nested = {"items": items}
    items.append(nested)
    with pytest.raises(ValueError, match=r"repeat_tracker.*cyclic"):
        outcome(repeat_tracker={"nested": nested})


def test_repeated_noncyclic_tracker_containers_are_duplicated_by_value():
    shared = [{"score": 1}]
    got = outcome(repeat_tracker={"left": shared, "right": shared})
    shared[0]["score"] = 99

    assert got.repeat_tracker["left"] == got.repeat_tracker["right"]
    assert got.repeat_tracker["left"] is not got.repeat_tracker["right"]
    assert got.repeat_tracker["left"][0]["score"] == 1


@pytest.mark.parametrize("kind", ["dict", "list", "tuple"])
@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(MAX_JSON_DEPTH - 1, True, id="below-limit"),
        pytest.param(MAX_JSON_DEPTH, True, id="at-limit"),
        pytest.param(MAX_JSON_DEPTH + 1, False, id="above-limit"),
        pytest.param(sys.getrecursionlimit() + 10, False, id="far-above-limit"),
    ],
)
def test_repeat_tracker_has_a_deterministic_container_depth_limit(kind, depth, accepted):
    tracker = nested_json(kind, depth)
    if not accepted:
        with pytest.raises(ValueError, match=r"repeat_tracker.*depth"):
            outcome(repeat_tracker=tracker)
        return

    applied = outcome(repeat_tracker=tracker).apply({})
    json.dumps(applied, allow_nan=False)


def test_wide_repeat_tracker_remains_allowed():
    tracker = {"items": [{"value": index} for index in range(2_000)]}
    applied = outcome(repeat_tracker=tracker).apply({})
    json.dumps(applied, allow_nan=False)


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(11, True, id="below-limit"),
        pytest.param(12, True, id="at-limit"),
        pytest.param(13, False, id="above-limit"),
    ],
)
def test_repeat_tracker_limits_expanded_json_dag_work(depth, accepted):
    tracker = shared_binary_json(depth)
    if not accepted:
        with pytest.raises(ValueError, match=r"repeat_tracker.*node"):
            outcome(repeat_tracker=tracker)
        return

    applied = outcome(repeat_tracker=tracker).apply({})
    json.dumps(applied, allow_nan=False)


@pytest.mark.parametrize(
    "hostile",
    [
        StringSubclass("value"),
        IntSubclass(1),
        FloatSubclass(1.0),
        {"raw": "dict"},
        {1, 2},
        float("nan"),
    ],
)
def test_apply_rejects_impossible_corrupt_frozen_tracker_values(hostile):
    got = outcome()
    object.__setattr__(got, "repeat_tracker", MappingProxyType({"bad": hostile}))
    with pytest.raises(ValueError, match=r"repeat_tracker\.bad"):
        got.apply({})


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(MAX_JSON_SAFE_INTEGER + 1, id="one-over-safe-integer"),
        pytest.param(10**10000, id="integer-string-limit"),
    ],
)
def test_apply_defensively_rejects_corrupt_frozen_tracker_integers(hostile):
    got = outcome()
    object.__setattr__(got, "repeat_tracker", MappingProxyType({"bad": hostile}))
    with pytest.raises(ValueError, match=r"repeat_tracker\.bad.*integer"):
        got.apply({})


def test_apply_rejects_an_impossible_corrupt_frozen_tracker_cycle():
    got = outcome()
    backing = {}
    corrupt = MappingProxyType(backing)
    backing["self"] = corrupt
    object.__setattr__(got, "repeat_tracker", corrupt)
    with pytest.raises(ValueError, match=r"repeat_tracker.*cyclic"):
        got.apply({})


@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(11, True, id="below-limit"),
        pytest.param(12, True, id="at-limit"),
        pytest.param(13, False, id="above-limit"),
    ],
)
def test_defensive_thaw_limits_expanded_json_dag_work(depth, accepted):
    got = outcome()
    object.__setattr__(got, "repeat_tracker", frozen_shared_binary_json(depth))
    if not accepted:
        with pytest.raises(ValueError, match=r"repeat_tracker.*node"):
            got.apply({})
        return

    json.dumps(got.apply({}), allow_nan=False)


@pytest.mark.parametrize("kind", ["mapping", "tuple"])
@pytest.mark.parametrize(
    ("depth", "accepted"),
    [
        pytest.param(MAX_JSON_DEPTH - 1, True, id="below-limit"),
        pytest.param(MAX_JSON_DEPTH, True, id="at-limit"),
        pytest.param(MAX_JSON_DEPTH + 1, False, id="above-limit"),
        pytest.param(sys.getrecursionlimit() + 10, False, id="far-above-limit"),
    ],
)
def test_defensive_thaw_has_the_same_deterministic_depth_limit(kind, depth, accepted):
    got = outcome()
    object.__setattr__(got, "repeat_tracker", frozen_json(kind, depth))
    if not accepted:
        with pytest.raises(ValueError, match=r"repeat_tracker.*depth"):
            got.apply({})
        return

    json.dumps(got.apply({}), allow_nan=False)


@pytest.mark.parametrize("value", [{1: "non-string key"}, {"bad": float("nan")}, {"bad": {1}}])
def test_apply_rejects_non_json_metadata(value):
    with pytest.raises(ValueError, match="meta"):
        outcome().apply(value)


def test_apply_rejects_metadata_outside_the_integer_and_depth_contract():
    with pytest.raises(ValueError, match=r"meta\.value.*integer"):
        outcome().apply({"value": 10**10000})
    with pytest.raises(ValueError, match=r"meta.*depth"):
        outcome().apply(nested_json("dict", MAX_JSON_DEPTH + 1))


def test_apply_accepts_boundary_metadata_and_the_result_is_serializable():
    meta = nested_json("dict", MAX_JSON_DEPTH)
    meta["integer"] = MAX_JSON_SAFE_INTEGER
    applied = outcome().apply(meta)
    json.dumps(applied, allow_nan=False)

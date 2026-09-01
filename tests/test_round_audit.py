"""Audit records for dispatched and deliberately skipped friends."""

from pathlib import Path
import threading
import time

import pytest

from adversarial_friends import rounds as rounds_mod
from adversarial_friends.adapters import Capability, FriendSpec
from adversarial_friends.authority import ExternalToolPolicy
from adversarial_friends.ceilings import KILL_GRACE_S, Budget
from adversarial_friends.commands import critique as critique_mod, crossexam as crossexam_mod
from adversarial_friends.commands.checkpoint import (
    legacy_successful_friend_ids,
    normalize_friend_rows,
)
from adversarial_friends.commands.critique import run_critique
from adversarial_friends.commands.crossexam import run_rounds
from adversarial_friends.dispatch import _stderr_tail
from adversarial_friends.errors import UsageError
from adversarial_friends.failures import RepeatTracker
from adversarial_friends.ledger import Claim
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.rounds import persist_result
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult


def _spec(name: str = "friend-ops-0", lens: str = "ops") -> FriendSpec:
    return FriendSpec(
        name=name,
        cli="fake",
        lens=lens,
        model=None,
        effort=None,
        scope="doc",
        timeout=30,
    )


def _success(stderr: str = "") -> SpawnResult:
    return SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout='{"no_findings": true}',
        stderr=stderr,
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({"findings": None, "no_findings": True}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )


def _claim() -> Claim:
    return Claim(
        id="c-0001@1",
        supersedes=None,
        origin=["other/ops"],
        lens="ops",
        round=1,
        advisory=False,
        severity="high",
        claim="guard missing",
        location="src/a.py:1",
        evidence="evidence",
        failure_scenario="failure",
        suggested_fix="fix",
    )


def test_successful_stderr_is_visible_bounded_and_references_full_capture(tmp_path):
    store = RunStore(tmp_path, "run-audit")
    stderr = "`danger` [click](javascript:bad) https://example.test/" + "x" * 400
    summary = _stderr_tail(stderr)

    row = persist_result(
        store,
        1,
        _spec(),
        Capability(False, True, "none"),
        _success(stderr),
        "exec",
        ExternalToolPolicy.DENY,
    )

    assert row["status"] == (f"ok (diagnostics: {summary}; full text in round-1/friend-ops-0.err)")
    assert row["diagnostics"] == summary
    assert row["diagnostics_path"] == "round-1/friend-ops-0.err"
    assert len(row["diagnostics"]) <= 200
    assert "javascript:" not in row["diagnostics"]
    assert "https://" not in row["diagnostics"]
    assert store.friend_err_path(1, "friend-ops-0").read_text() == stderr


def test_repeat_disabled_friend_is_partitioned_and_persisted_as_a_skip(tmp_path):
    assert hasattr(rounds_mod, "partition_dispatchable")
    tracker = RepeatTracker()
    tracker._last["broken-ops-0"] = "1:exit 1"
    tracker._count["broken-ops-0"] = 2
    tracker.disabled["broken-ops-0"] = "exit 1"
    ready_spec = _spec("ready-ops-0")
    broken_spec = _spec("broken-ops-0")

    ready, skipped = rounds_mod.partition_dispatchable([ready_spec, broken_spec], tracker)

    assert ready == [ready_spec]
    assert len(skipped) == 1
    assert skipped[0].spec == broken_spec
    assert "will not be dispatched again" in skipped[0].reason

    store = RunStore(tmp_path, "run-skips")
    row = rounds_mod.persist_skip(store, 3, skipped[0])
    meta_path = store.friend_paths(3, "broken-ops-0")[2]
    assert meta_path.read_text().startswith("status=skipped\n")
    assert not store.friend_prompt_path(3, "broken-ops-0").exists()
    assert row["transport"] == "not-dispatched"
    assert row["status"].startswith("skipped: ")


def test_repeat_skip_reason_is_bounded_and_safe_for_metadata():
    tracker = RepeatTracker()
    tracker._count["broken-ops-0"] = 2
    tracker.disabled["broken-ops-0"] = (
        "`danger` [click](javascript:bad) https://example.test/" + "x" * 400
    )

    _ready, skipped = rounds_mod.partition_dispatchable([_spec("broken-ops-0")], tracker)

    reason = skipped[0].reason
    assert len(reason) <= 200
    assert "`" not in reason
    assert "javascript:" not in reason
    assert "https://" not in reason


def test_critique_persists_repeat_skip_before_prompt_construction(tmp_path):
    spec = _spec("broken-ops-0")
    tracker = RepeatTracker()
    tracker._last[spec.name] = "1:exit 1"
    tracker._count[spec.name] = 2
    tracker.disabled[spec.name] = "exit 1"
    store = RunStore(tmp_path, "run-critique-skip")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome, claims, counter = run_critique(
        [spec],
        3,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        Path("schema.json"),
        artifact,
        None,
        None,
        threading.Event(),
        tracker=tracker,
    )

    assert claims == [] and counter == 0 and outcome.calls == 0
    assert [row["name"] for row in outcome.friends_meta] == [spec.name]
    assert outcome.friends_meta[0]["status"].startswith("skipped: ")
    assert store.friend_paths(3, spec.name)[2].read_text().startswith("status=skipped\n")
    assert not store.friend_prompt_path(3, spec.name).exists()
    assert len(outcome.downgrades) == 1


def test_repeat_skip_is_not_a_fresh_critique_failure(tmp_path):
    spec = _spec("broken-ops-0")
    tracker = RepeatTracker()
    tracker._count[spec.name] = 2
    tracker.disabled[spec.name] = "exit 1"
    store = RunStore(tmp_path, "run-skip-is-policy")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome, _claims, _counter = run_critique(
        [spec],
        3,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        Path("schema.json"),
        artifact,
        None,
        None,
        threading.Event(),
        tracker=tracker,
    )

    assert outcome.any_failed is False


def test_interrupted_critique_setup_leaves_no_prompt_without_result(tmp_path):
    spec = _spec("ready-ops-0", lens="missing")
    store = RunStore(tmp_path, "run-interrupted-setup")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    interrupted = threading.Event()
    interrupted.set()

    outcome, _claims, _counter = run_critique(
        [spec],
        1,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        Path("schema.json"),
        artifact,
        None,
        None,
        interrupted,
    )

    assert outcome.friends_meta == []
    assert outcome.downgrades == []
    assert not store.friend_prompt_path(1, spec.name).exists()


def test_failed_isolation_setup_leaves_no_prompt_without_result(monkeypatch, tmp_path):
    spec = _spec("ready-ops-0")
    store = RunStore(tmp_path, "run-broken-setup")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    def fail_setup(_dest, _artifact):
        raise RuntimeError("setup interrupted")

    monkeypatch.setattr(rounds_mod.isolation, "doc_scope_dir", fail_setup)

    with pytest.raises(RuntimeError, match="setup interrupted"):
        run_critique(
            [spec],
            1,
            [],
            0,
            artifact.read_text(),
            store,
            ReviewState(),
            {},
            None,
            Path("schema.json"),
            artifact,
            None,
            None,
            threading.Event(),
        )

    assert not store.friend_prompt_path(1, spec.name).exists()


@pytest.mark.parametrize(
    "raised",
    [UsageError("refused unsafe dispatch"), KeyboardInterrupt("interrupted dispatch")],
    ids=("af-error", "interruption"),
)
def test_dispatch_round_returns_completed_and_refused_attempts_when_one_friend_errors(
    monkeypatch, tmp_path, raised
):
    good = _spec("good-ops-0")
    refused = _spec("refused-ops-0")
    undispatched = _spec("undispatched-ops-0")
    specs = [good, refused, undispatched]
    store = RunStore(tmp_path, "run-partial-dispatch")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    prompts = {}
    for spec in specs:
        prompt = store.friend_prompt_path(1, spec.name)
        prompt.write_text(f"prompt for {spec.name}\n")
        prompts[spec.name] = prompt
    started = []

    def fake_dispatch(spec, *_args, **_kwargs):
        started.append(spec.name)
        if spec is refused:
            raise raised
        if spec is undispatched:
            pytest.fail("queued friend should be cancelled after the refusal")
        return spec, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY

    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)

    batch = rounds_mod.dispatch_round(
        specs,
        1,
        prompts,
        store,
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
        max_concurrency=1,
    )

    assert [result[0].name for result in batch.results] == [good.name, refused.name]
    assert batch.results[0][2].result.succeeded is True
    assert str(raised) in (batch.results[1][2].failure_reason or "")
    assert isinstance(batch.error, type(raised))
    assert batch.auth_abort is None
    assert started == [good.name, refused.name]


def test_critique_keeps_only_prompts_and_rows_for_actual_partial_dispatches(monkeypatch, tmp_path):
    good = _spec("good-ops-0")
    refused = _spec("refused-ops-0")
    undispatched = _spec("undispatched-ops-0")
    specs = [good, refused, undispatched]
    store = RunStore(tmp_path, "run-partial-critique")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    real_dispatch_round = rounds_mod.dispatch_round

    def serial_dispatch(*args, **kwargs):
        kwargs["max_concurrency"] = 1
        return real_dispatch_round(*args, **kwargs)

    def fake_dispatch(spec, *_args, **_kwargs):
        if spec.name == refused.name:
            raise UsageError("refused unsafe dispatch")
        if spec.name == undispatched.name:
            pytest.fail("queued friend should not be dispatched")
        return spec, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY

    monkeypatch.setattr(critique_mod, "dispatch_round", serial_dispatch)
    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)

    outcome, _claims, _counter = run_critique(
        specs,
        1,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert isinstance(outcome.dispatch_error, UsageError)
    assert [row["name"] for row in outcome.friends_meta] == [good.name, refused.name]
    assert outcome.friends_meta[1]["external_tool_policy"] == "unknown"
    assert store.friend_prompt_path(1, good.name).exists()
    assert store.friend_prompt_path(1, refused.name).exists()
    assert not store.friend_prompt_path(1, undispatched.name).exists()


@pytest.mark.parametrize(
    "raised",
    [RuntimeError("submit failed"), KeyboardInterrupt("submit interrupted")],
    ids=("failure", "interruption"),
)
def test_critique_recovers_actual_attempt_when_later_future_submission_stops(
    monkeypatch, tmp_path, raised
):
    begun = threading.Event()
    first = _spec("first-ops-0")
    never_submitted = _spec("never-submitted-ops-0")
    also_never_submitted = _spec("also-never-submitted-ops-0")
    specs = [first, never_submitted, also_never_submitted]
    store = RunStore(tmp_path, "run-submit-interruption")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    real_submit = rounds_mod.concurrent.futures.ThreadPoolExecutor.submit
    submit_count = 0

    def interrupted_submit(pool, function, spec):
        nonlocal submit_count
        submit_count += 1
        if submit_count == 2:
            assert begun.wait(timeout=1)
            raise raised
        return real_submit(pool, function, spec)

    def fake_dispatch(spec, *_args, **_kwargs):
        begun.set()
        return spec, Capability(False, True, "none"), _success(), ExternalToolPolicy.DENY

    monkeypatch.setattr(
        rounds_mod.concurrent.futures.ThreadPoolExecutor,
        "submit",
        interrupted_submit,
    )
    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)

    outcome, _claims, _counter = run_critique(
        specs,
        1,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
    )

    assert isinstance(outcome.dispatch_error, type(raised))
    assert [row["name"] for row in outcome.friends_meta] == [first.name]
    assert store.friend_prompt_path(1, first.name).exists()
    assert not store.friend_prompt_path(1, never_submitted.name).exists()
    assert not store.friend_prompt_path(1, also_never_submitted.name).exists()


def test_judging_keeps_only_prompts_and_rows_for_actual_partial_dispatches(monkeypatch, tmp_path):
    good = _spec("good-ops-0")
    refused = _spec("refused-ops-0")
    undispatched = _spec("undispatched-ops-0")
    specs = [good, refused, undispatched]
    claim = _claim()
    store = RunStore(tmp_path, "run-partial-judging")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    real_dispatch_round = rounds_mod.dispatch_round

    def serial_dispatch(*args, **kwargs):
        kwargs["max_concurrency"] = 1
        return real_dispatch_round(*args, **kwargs)

    verdict = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="{}",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(
            {
                "verdicts": [
                    {
                        "claim_id": claim.id,
                        "verdict": "upheld",
                        "confidence": "high",
                        "reasoning": "checked",
                    }
                ]
            },
            [],
            True,
        ),
        failure_reason=None,
        orphans_suspected=False,
    )

    def fake_dispatch(spec, *_args, **_kwargs):
        if spec.name == refused.name:
            raise UsageError("refused unsafe dispatch")
        if spec.name == undispatched.name:
            pytest.fail("queued judge should not be dispatched")
        return spec, Capability(False, True, "none"), verdict, ExternalToolPolicy.DENY

    monkeypatch.setattr(crossexam_mod, "dispatch_round", serial_dispatch)
    monkeypatch.setattr(rounds_mod, "_dispatch", fake_dispatch)

    outcome = run_rounds(
        specs,
        [claim],
        store,
        ReviewState.replay([claim]),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        artifact.read_text(),
        None,
        None,
        threading.Event(),
        Budget(max_calls=10, started=0.0),
        2,
        now=lambda: 0.0,
    )

    assert isinstance(outcome.dispatch_error, UsageError)
    assert [row["name"] for row in outcome.friends_meta] == [good.name, refused.name]
    assert store.friend_prompt_path(2, good.name).exists()
    assert store.friend_prompt_path(2, refused.name).exists()
    assert not store.friend_prompt_path(2, undispatched.name).exists()


def test_judging_persists_repeat_skip_before_prompt_construction(tmp_path):
    spec = _spec("broken-ops-0")
    tracker = RepeatTracker()
    tracker._last[spec.name] = "1:exit 1"
    tracker._count[spec.name] = 2
    tracker.disabled[spec.name] = "exit 1"
    claim = _claim()
    store = RunStore(tmp_path, "run-judge-skip")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome = run_rounds(
        [spec],
        [claim],
        store,
        ReviewState.replay([claim]),
        {},
        None,
        Path("schema.json"),
        artifact,
        artifact.read_text(),
        None,
        None,
        threading.Event(),
        Budget(max_calls=10, started=time.monotonic()),
        2,
        tracker=tracker,
    )

    assert [row["name"] for row in outcome.friends_meta] == [spec.name]
    assert outcome.friends_meta[0]["status"].startswith("skipped: ")
    assert store.friend_paths(2, spec.name)[2].read_text().startswith("status=skipped\n")
    assert not store.friend_prompt_path(2, spec.name).exists()
    assert len([note for note in outcome.downgrades if spec.name in note]) == 1


@pytest.mark.parametrize(
    ("budget", "now"),
    [
        (Budget(max_calls=0, started=0.0), lambda: 0.0),
        (
            Budget(max_calls=10, max_wall_clock_s=float(KILL_GRACE_S), started=0.0),
            lambda: 0.0,
        ),
    ],
    ids=("max-calls", "no-usable-wall-clock"),
)
def test_judging_ceiling_refusal_writes_no_prompt_or_friend_row(tmp_path, budget, now):
    spec = _spec("judge-ops-0", lens="missing")
    claim = _claim()
    store = RunStore(tmp_path, "run-judge-refused")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome = run_rounds(
        [spec],
        [claim],
        store,
        ReviewState.replay([claim]),
        {},
        None,
        Path("schema.json"),
        artifact,
        artifact.read_text(),
        None,
        None,
        threading.Event(),
        budget,
        2,
        now=now,
    )

    assert outcome.friends_meta == []
    assert not any("judged with the generic prompt" in note for note in outcome.downgrades)
    assert not store.friend_prompt_path(2, spec.name).exists()
    assert budget.exhausted_by is not None


def test_checkpoint_accepts_audited_success_and_skip_rows(tmp_path):
    store = RunStore(tmp_path, "run-checkpoint-audit")
    spec = _spec()
    success = persist_result(
        store,
        1,
        spec,
        Capability(False, True, "none"),
        _success("safe warning"),
        "exec",
        ExternalToolPolicy.DENY,
    )
    skipped = rounds_mod.SkippedFriend(_spec("broken-ops-0"), "repeat-disabled")
    skip_row = rounds_mod.persist_skip(store, 2, skipped)

    normalized = normalize_friend_rows(
        [success, skip_row],
        {"friend-ops-0", "broken-ops-0"},
    )

    assert normalized == [success, skip_row]
    assert legacy_successful_friend_ids(normalized, 1) == ["friend-ops-0"]


def test_checkpoint_accepts_exact_safe_legacy_failure_with_inline_stderr_reference():
    diagnostics = "x" * 200
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": (
            f"failed: exit 17 (stderr: {diagnostics}; full text in round-1/friend-ops-0.err)"
        ),
    }

    assert normalize_friend_rows([row], {"friend-ops-0"}) == [row]


def test_legacy_failure_shape_does_not_legalize_hostile_stripped_current_status():
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": (
            "failed: [click](javascript:bad) (stderr: safe; full text in round-1/friend-ops-0.err)"
        ),
    }

    with pytest.raises(UsageError, match="failure reason"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_checkpoint_refuses_a_diagnostic_status_without_bounded_summary_fields():
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "ok (diagnostics: [click](javascript:bad)" + "x" * 10_000,
    }

    with pytest.raises(UsageError, match="diagnostic summary"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_hostile_http_failure_keeps_raw_text_only_in_err_and_resumes_safely(tmp_path):
    store = RunStore(tmp_path, "run-http-failure")
    hostile = "http 500: `danger` [click](javascript:bad) https://example.test/" + "x" * 800
    outcome = SpawnResult(
        argv=["POST", "http://localhost/api", "model"],
        exit_code=500,
        stdout="",
        stderr=hostile,
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(None, [hostile], False),
        failure_reason=hostile,
        orphans_suspected=True,
    )

    row = persist_result(
        store,
        1,
        _spec("ollama-ops-0"),
        Capability(False, False, "denied"),
        outcome,
        "http",
        ExternalToolPolicy.DENY,
    )

    assert hostile not in row["status"]
    assert len(row["status"]) < 600
    assert "javascript:" not in row["status"]
    assert "https://" not in row["status"]
    assert "http 500" in row["status"]
    assert "orphans suspected" in row["status"]
    assert store.friend_err_path(1, "ollama-ops-0").read_text() == hostile
    assert normalize_friend_rows([row], {"ollama-ops-0"}) == [row]


def test_checkpoint_rejects_unsanitized_new_failure_status():
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "failed: [click](javascript:bad) (stderr: safe; full text in "
        "round-1/friend-ops-0.err)",
        "diagnostics": "safe",
        "diagnostics_path": "round-1/friend-ops-0.err",
    }

    with pytest.raises(UsageError, match="failure reason"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_checkpoint_rejects_hostile_failure_status_when_diagnostic_fields_are_stripped():
    row = {
        "name": "friend-ops-0",
        "model": None,
        "effort": None,
        "round": 1,
        "status": "failed: [click](javascript:bad) https://example.test/" + "x" * 10_000,
    }

    with pytest.raises(UsageError, match="failure reason"):
        normalize_friend_rows([row], {"friend-ops-0"})


def test_stderr_summary_strips_ansi_and_terminal_controls_before_persistence(tmp_path):
    store = RunStore(tmp_path, "run-terminal-controls")
    raw = "\x1b[31mERROR\x1b[0m before\bafter \x00nul \x9b32mgreen\x9b0m"

    row = persist_result(
        store,
        1,
        _spec(),
        Capability(False, True, "none"),
        _success(raw),
        "exec",
        ExternalToolPolicy.DENY,
    )

    summary = row["diagnostics"]
    assert "ERROR" in summary and "green" in summary
    assert "\x1b" not in summary and "\x9b" not in summary
    assert "\b" not in summary and "\x00" not in summary
    assert store.friend_err_path(1, "friend-ops-0").read_text() == raw

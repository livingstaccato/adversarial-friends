import json
import threading

import pytest

from afriend import rounds as rounds_mod
from afriend.adapters import Capability, FriendSpec
from afriend.authority import ExternalToolPolicy
from afriend.ceilings import Budget
from afriend.commands import crossexam as crossexam_mod
from afriend.commands.crossexam import run_rounds
from afriend.errors import UsageError
from afriend.judgebatch import persist_judging_batch, recover_judging_batch
from afriend.ledger import Claim, Verdict
from afriend.normalize import NormalizeResult
from afriend.reviewstate import ReviewState
from afriend.rounds import persist_result
from afriend.runstore import RunStore
from afriend.spawn import SpawnResult
from afriend.verdicts import build_successor


def _spec(name: str, lens: str) -> FriendSpec:
    return FriendSpec(name, "fake", lens, None, None, "doc", 30)


def _host_spec() -> FriendSpec:
    return FriendSpec(
        "codex-ops-0",
        "codex",
        "ops",
        None,
        None,
        "doc",
        30,
        independent=False,
        host_self_review=True,
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


def _rewrite_audit_version(store: RunStore, spec: FriendSpec, version: int) -> dict[str, object]:
    path = store.friend_audit_path(2, spec.name)
    data = json.loads(path.read_text())
    if version == 2:
        persist_judging_batch(store, 2, spec, data["row"], [], [], [])
        data = json.loads(path.read_text())
    return data


@pytest.mark.parametrize("version", [1, 2])
def test_judging_sidecar_recovery_injects_frozen_advisory_host_role(tmp_path, version):
    spec = _host_spec()
    store = RunStore(tmp_path, f"run-host-role-v{version}")
    store.write_sensitive(store.friend_prompt_path(2, spec.name), "judge prompt")
    persist_result(
        store,
        2,
        spec,
        Capability(False, True, "none"),
        SpawnResult(
            argv=["codex"],
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_s=0.1,
            timed_out=False,
            result=NormalizeResult({}, [], True),
            failure_reason=None,
            orphans_suspected=False,
        ),
        "exec",
        ExternalToolPolicy.DENY,
    )
    data = _rewrite_audit_version(store, spec, version)
    data["row"].pop("independent")
    data["row"].pop("host_self_review")
    store.write_sensitive_atomic(
        store.friend_audit_path(2, spec.name), json.dumps(data, sort_keys=True)
    )

    recovered = rounds_mod.recover_result_audit(store, 2, spec)

    assert recovered["independent"] is False
    assert recovered["host_self_review"] is True


@pytest.mark.parametrize("version", [1, 2])
def test_judging_sidecar_recovery_rejects_role_conflicting_with_frozen_host(tmp_path, version):
    spec = _host_spec()
    store = RunStore(tmp_path, f"run-host-role-conflict-v{version}")
    store.write_sensitive(store.friend_prompt_path(2, spec.name), "judge prompt")
    persist_result(
        store,
        2,
        spec,
        Capability(False, True, "none"),
        SpawnResult(
            argv=["codex"],
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_s=0.1,
            timed_out=False,
            result=NormalizeResult({}, [], True),
            failure_reason=None,
            orphans_suspected=False,
        ),
        "exec",
        ExternalToolPolicy.DENY,
    )
    data = _rewrite_audit_version(store, spec, version)
    data["row"]["independent"] = True
    store.write_sensitive_atomic(
        store.friend_audit_path(2, spec.name), json.dumps(data, sort_keys=True)
    )

    with pytest.raises(UsageError, match="independent conflicts with the frozen roster"):
        rounds_mod.recover_result_audit(store, 2, spec)


def test_judging_retry_reuses_durable_verdicts_and_dispatches_only_missing_work(
    monkeypatch, tmp_path
):
    first = _spec("first-ops-0", "first")
    second = _spec("second-ops-0", "second")
    claim = _claim()
    durable = Verdict(
        claim.id,
        "fake/first",
        2,
        "upheld",
        "high",
        "verified",
        "durable first vote",
        None,
        None,
    )
    store = RunStore(tmp_path, "run-recover-judging")
    store.ledger.append(claim)
    prompt = store.friend_prompt_path(2, first.name)
    store.write_sensitive(prompt, "judge prompt for durable first vote")
    durable_result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout='{"verdicts":[{"claim_id":"c-0001@1","verdict":"upheld"}]}',
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    durable_row = persist_result(
        store,
        2,
        first,
        Capability(False, True, "none"),
        durable_result,
        "fake",
        ExternalToolPolicy.DENY,
    )
    store.ledger.append(durable)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    dispatched: list[str] = []

    def dispatch(specs, *_args, **_kwargs):
        dispatched.extend(spec.name for spec in specs)
        result = SpawnResult(
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
                            "evidence_assessment": "verified",
                            "reasoning": "second vote",
                        }
                    ]
                },
                [],
                True,
            ),
            failure_reason=None,
            orphans_suspected=False,
        )
        return rounds_mod.DispatchRoundOutcome(
            [(second, Capability(False, True, "none"), result, ExternalToolPolicy.DENY)]
        )

    monkeypatch.setattr(crossexam_mod, "dispatch_round", dispatch)
    budget = Budget(max_calls=10, started=0.0)
    outcome = run_rounds(
        [first, second],
        [claim],
        store,
        ReviewState.replay(store.ledger.records()),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        artifact.read_text(),
        None,
        None,
        threading.Event(),
        budget,
        2,
        now=lambda: 0.0,
    )

    assert dispatched == [second.name]
    assert list(store.ledger.verdicts_for(claim.id)) == [durable, outcome.verdicts[-1]]
    assert outcome.states[claim.id] == "settled-upheld"
    assert budget.calls == 2
    assert [row for row in outcome.friends_meta if row["name"] == first.name] == [durable_row]


@pytest.mark.parametrize("crash_on_append", [1, 2])
def test_judging_retry_replays_a_complete_captured_batch_without_redispatch(
    monkeypatch, tmp_path, crash_on_append
):
    spec = _spec("first-ops-0", "first")
    claims = [_claim(), Claim(**{**_claim().__dict__, "id": "c-0002@1"})]
    store = RunStore(tmp_path, f"run-complete-batch-{crash_on_append}")
    for claim in claims:
        store.ledger.append(claim)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact")
    payload = {
        "verdicts": [
            {
                "claim_id": claim.id,
                "verdict": "upheld",
                "confidence": "high",
                "evidence_assessment": "confirmed",
                "reasoning": f"checked {claim.id}",
                "counter_evidence": None,
                "amended_claim": None,
            }
            for claim in claims
        ]
    }
    result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="captured",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(payload, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: rounds_mod.DispatchRoundOutcome(
            [(spec, Capability(False, True, "none"), result, ExternalToolPolicy.DENY)]
        ),
    )
    original_append = store.ledger.append
    verdict_appends = 0

    def crash_during_batch(record):
        nonlocal verdict_appends
        if isinstance(record, Verdict):
            verdict_appends += 1
            if verdict_appends == crash_on_append:
                raise RuntimeError("injected verdict append crash")
        original_append(record)

    monkeypatch.setattr(store.ledger, "append", crash_during_batch)
    with pytest.raises(RuntimeError, match="injected verdict append crash"):
        run_rounds(
            [spec],
            claims,
            store,
            ReviewState.replay(store.ledger.records()),
            {},
            None,
            tmp_path / "schema.json",
            artifact,
            "artifact",
            None,
            None,
            threading.Event(),
            Budget(max_calls=10, started=0.0),
            2,
            now=lambda: 0.0,
        )

    audit = store.friend_audit_path(2, spec.name)
    assert __import__("json").loads(audit.read_text())["version"] == 2
    monkeypatch.setattr(store.ledger, "append", original_append)
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: pytest.fail("captured batch was redispatched"),
    )
    budget = Budget(max_calls=10, started=0.0)
    outcome = run_rounds(
        [spec],
        claims,
        store,
        ReviewState.replay(store.ledger.records()),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        "artifact",
        None,
        None,
        threading.Event(),
        budget,
        2,
        now=lambda: 0.0,
    )

    assert [
        verdict.claim_id for verdict in store.ledger.records() if isinstance(verdict, Verdict)
    ] == [claim.id for claim in claims]
    assert budget.calls == 1
    assert [row["name"] for row in outcome.friends_meta] == [spec.name]


def test_judging_retry_reuses_a_successor_persisted_before_the_crash(monkeypatch, tmp_path):
    first = _spec("first-ops-0", "first")
    second = _spec("second-ops-0", "second")
    claim = _claim()
    verdicts = [
        Verdict(
            claim.id,
            f"fake/{lens}",
            2,
            "amended",
            "high",
            "verified",
            "rewrite it",
            None,
            "guard is conditionally missing",
        )
        for lens in ("first", "second")
    ]
    successor, _note = build_successor(claim, verdicts, 2)
    store = RunStore(tmp_path, "run-recover-successor")
    for record in [claim, *verdicts, successor]:
        store.ledger.append(record)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: pytest.fail("durable judging work was redispatched"),
    )

    outcome = run_rounds(
        [first, second],
        [claim, successor],
        store,
        ReviewState.replay(store.ledger.records()),
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

    assert [saved.id for saved in store.ledger.claims()] == [claim.id, successor.id]
    assert not any(saved.id.endswith("@3") for saved in outcome.claims)
    assert [row["transport"] for row in outcome.friends_meta] == [
        "legacy-unknown",
        "legacy-unknown",
    ]


def test_recovered_verdict_refuses_a_tampered_audit_capture(tmp_path):
    spec = _spec("first-ops-0", "first")
    claim = _claim()
    durable = Verdict(claim.id, "fake/first", 2, "upheld", "high", "verified", "vote", None, None)
    store = RunStore(tmp_path, "run-tampered-judge-audit")
    store.ledger.append(claim)
    store.write_sensitive(store.friend_prompt_path(2, spec.name), "prompt")
    result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="original",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    persist_result(
        store,
        2,
        spec,
        Capability(False, True, "none"),
        result,
        "fake",
        ExternalToolPolicy.DENY,
    )
    store.ledger.append(durable)
    store.write_sensitive(store.friend_paths(2, spec.name)[0], "tampered")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact")

    with pytest.raises(UsageError, match="raw capture was modified"):
        run_rounds(
            [spec],
            [claim],
            store,
            ReviewState.replay(store.ledger.records()),
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


def test_incomplete_legacy_judging_audit_fails_closed_instead_of_redispatching(
    monkeypatch, tmp_path
):
    spec = _spec("first-ops-0", "first")
    claim = _claim()
    store = RunStore(tmp_path, "run-incomplete-legacy-audit")
    store.ledger.append(claim)
    store.write_sensitive(store.friend_prompt_path(2, spec.name), "legacy prompt")
    result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="captured but not committed",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    persist_result(
        store,
        2,
        spec,
        Capability(False, True, "none"),
        result,
        "fake",
        ExternalToolPolicy.DENY,
    )
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact")
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: pytest.fail("incomplete prior call was redispatched"),
    )

    with pytest.raises(UsageError, match="incomplete legacy judging audit"):
        run_rounds(
            [spec],
            [claim],
            store,
            ReviewState.replay(store.ledger.records()),
            {},
            None,
            tmp_path / "schema.json",
            artifact,
            "artifact",
            None,
            None,
            threading.Event(),
            Budget(max_calls=10, started=0.0),
            2,
            now=lambda: 0.0,
        )


def test_judging_sidecar_verdict_tamper_disagrees_with_bound_parsed_batch(tmp_path):
    spec = _spec("first-ops-0", "first")
    claim = _claim()
    verdict = Verdict(
        claim.id, "fake/first", 2, "upheld", "high", "confirmed", "original", None, None
    )
    store = RunStore(tmp_path, "run-sidecar-verdict-tamper")
    store.write_sensitive(store.friend_prompt_path(2, spec.name), "prompt")
    result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="raw",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult({}, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    row = persist_result(
        store,
        2,
        spec,
        Capability(False, True, "none"),
        result,
        "fake",
        ExternalToolPolicy.DENY,
    )
    persist_judging_batch(store, 2, spec, row, [claim.id], [], [verdict])
    audit = store.friend_audit_path(2, spec.name)
    data = json.loads(audit.read_text())
    data["judging"]["verdicts"][0]["reasoning"] = "tampered"
    audit.write_text(json.dumps(data))

    with pytest.raises(UsageError, match="parsed batch"):
        recover_judging_batch(store, 2, spec, [claim.id], "prompt")


def test_judging_replay_does_not_let_future_votes_rewrite_an_earlier_successor(
    monkeypatch, tmp_path
):
    """A durable round 3 can exist when run.json lagged the append-only
    ledger. Replaying round 2 must settle it from round-2 votes alone: future
    votes are not prior history and cannot change the successor it minted."""
    first = _spec("first-ops-0", "first")
    second = _spec("second-ops-0", "second")
    claim = _claim()
    round_two = [
        Verdict(
            claim.id,
            f"fake/{lens}",
            2,
            "amended",
            "high",
            "verified",
            "round two reasoning",
            None,
            "round two wording",
        )
        for lens in ("first", "second")
    ]
    successor, _note = build_successor(claim, round_two, 2)
    round_three = [
        Verdict(
            claim.id,
            f"fake/{lens}",
            3,
            "amended",
            "high",
            "verified",
            "future reasoning",
            None,
            "future wording",
        )
        for lens in ("first", "second")
    ]
    store = RunStore(tmp_path, "run-future-judging")
    for record in [claim, *round_two, successor, *round_three]:
        store.ledger.append(record)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")
    monkeypatch.setattr(
        crossexam_mod,
        "dispatch_round",
        lambda *_args, **_kwargs: pytest.fail("durable judging work was redispatched"),
    )

    outcome = run_rounds(
        [first, second],
        [claim, successor],
        store,
        ReviewState.replay(store.ledger.records()),
        {},
        None,
        tmp_path / "schema.json",
        artifact,
        artifact.read_text(),
        None,
        None,
        threading.Event(),
        Budget(max_calls=10, started=0.0),
        3,
        first_round=2,
        now=lambda: 0.0,
    )

    assert successor in outcome.claims
    assert not any(saved.id.endswith("@3") for saved in outcome.claims)
    assert outcome.verdicts[:2] == round_two

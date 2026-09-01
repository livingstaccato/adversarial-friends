import threading

import pytest

from adversarial_friends import rounds as rounds_mod
from adversarial_friends.adapters import Capability, FriendSpec
from adversarial_friends.ceilings import Budget
from adversarial_friends.commands import crossexam as crossexam_mod
from adversarial_friends.commands.crossexam import run_rounds
from adversarial_friends.ledger import Claim, Verdict
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult
from adversarial_friends.verdicts import build_successor


def _spec(name: str, lens: str) -> FriendSpec:
    return FriendSpec(name, "fake", lens, None, None, "doc", 30)


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
        return rounds_mod.DispatchRoundOutcome([(second, Capability(False, True, "none"), result)])

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

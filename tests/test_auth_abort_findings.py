"""dispatch_round used to `raise AfError` the instant ANY friend in a round
classified as a deterministic auth failure -- before the caller had a
chance to persist a single result. A round with a broken friend and a
succeeding friend lost BOTH: `persist_result` never ran for either one, and
`cmd_run`'s only local `except` catches `NeedsOrchestrator`, not a plain
`AfError`, so the exception reached `cli.py`'s top-level handler, which
prints a message and exits -- run.json and report.md never written at all,
and the succeeding friend's findings gone with them.

Each test here fails against the previous behaviour: dispatch_round raising
instead of returning `(results, auth_abort)` is checked, not assumed.
"""

from pathlib import Path
import threading

from adversarial_friends import rounds as rounds_mod
from adversarial_friends.adapters import Adapter, AuthMarkers, FriendSpec
from adversarial_friends.ceilings import Budget
from adversarial_friends.commands import critique as critique_mod, crossexam as crossexam_mod
from adversarial_friends.commands.critique import run_critique
from adversarial_friends.commands.crossexam import run_rounds
from adversarial_friends.commands.exits import decide_exit
from adversarial_friends.failures import RepeatTracker
from adversarial_friends.ids import format_claim_id
from adversarial_friends.ledger import Claim
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.outcomes import terminal_outcome
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.runstore import RunStore
from adversarial_friends.spawn import SpawnResult

AUTH_MESSAGE = "broken-friend: authentication required -- run 'brokencli login'."


def _spec(name: str, cli: str = "fake", lens: str = "good") -> FriendSpec:
    return FriendSpec(
        name=name, cli=cli, lens=lens, model=None, effort=None, scope="doc", timeout=60
    )


def _ok_result(claim_text: str) -> SpawnResult:
    payload = {
        "findings": [
            {
                "severity": "high",
                "claim": claim_text,
                "location": None,
                "evidence": "e",
                "failure_scenario": "f",
                "suggested_fix": "s",
            }
        ]
    }
    return SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="{}",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(payload, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )


def _auth_failed_result() -> SpawnResult:
    return SpawnResult(
        argv=["brokencli"],
        exit_code=41,
        stdout="",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(None, ["exit 41"], False),
        failure_reason="exit 41",
        orphans_suspected=False,
    )


def _claim(number: int, origin: str, text: str = "a finding") -> Claim:
    return Claim(
        id=format_claim_id(number),
        supersedes=None,
        origin=[origin],
        lens="ops",
        round=1,
        advisory=False,
        severity="medium",
        claim=text,
        location=None,
        evidence="e",
        failure_scenario="f",
        suggested_fix="s",
    )


# --- dispatch_round itself: returns, never raises ---------------------------


def test_dispatch_round_returns_every_result_instead_of_raising_on_an_auth_failure(
    monkeypatch, tmp_path
):
    """The defect exactly, at its source. Also proves the tracker records
    EVERY friend's outcome, not just the ones the old code reached before it
    raised -- the raise used to sit inside the recording loop itself, so a
    friend later in iteration order than the auth failure never got
    `tracker.record` called on it either."""
    good = _spec("good-friend")
    broken = _spec("broken-friend", cli="brokencli")
    outcomes = {good.name: _ok_result("the guard is missing"), broken.name: _auth_failed_result()}

    def _fake_dispatch(spec, *_args, **_kwargs):
        return spec, rounds_mod._UNKNOWN_CAPABILITY, outcomes[spec.name]

    monkeypatch.setattr(rounds_mod, "_dispatch", _fake_dispatch)

    artifact = tmp_path / "artifact.md"
    artifact.write_text("spec text", encoding="utf-8")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    prompt_for = {}
    for spec in (good, broken):
        path = prompt_dir / f"{spec.name}.prompt"
        path.write_text("prompt", encoding="utf-8")
        prompt_for[spec.name] = path

    registry = {
        "brokencli": Adapter(
            name="brokencli",
            binary="brokencli",
            base_argv=[],
            prompt_mode="stdin",
            prompt_flag="",
            readonly_argv=[],
            schema_flag="",
            model_flag="",
            internal_timeout_flag="",
            effort_kind="none",
            auth=AuthMarkers(exit_codes=(41,)),
        )
    }
    tracker = RepeatTracker()
    store = RunStore(tmp_path / "run", "run-auth")
    store.lock()

    results, auth_abort = rounds_mod.dispatch_round(
        [good, broken],
        1,
        prompt_for,
        store,
        registry,
        None,
        Path("schema.json"),
        artifact,
        None,
        None,
        threading.Event(),
        tracker=tracker,
    )

    assert {spec.name for spec, _cap, _outcome in results} == {"good-friend", "broken-friend"}
    assert auth_abort is not None
    assert "broken-friend" in auth_abort
    # Both friends were recorded -- proof the loop did not stop at the
    # first auth hit and skip whatever came after it.
    assert set(tracker.snapshot()["last"]) == {"good-friend", "broken-friend"}


# --- run_critique: persists and merges before surfacing the abort ----------


def test_run_critique_persists_every_friend_before_surfacing_an_auth_abort(monkeypatch, tmp_path):
    good = _spec("good-friend")
    broken = _spec("broken-friend")
    results = [
        (good, rounds_mod._UNKNOWN_CAPABILITY, _ok_result("the guard is missing")),
        (broken, rounds_mod._UNKNOWN_CAPABILITY, _auth_failed_result()),
    ]

    def _fake_dispatch_round(*_args, **_kwargs):
        return results, AUTH_MESSAGE

    monkeypatch.setattr(critique_mod, "dispatch_round", _fake_dispatch_round)

    store = RunStore(tmp_path, "run-auth")
    store.lock()
    review = ReviewState()

    outcome, all_claims, counter = run_critique(
        [good, broken],
        1,
        [],
        0,
        "artifact text",
        store,
        review,
        {},
        None,
        Path("schema.json"),
        Path("artifact.md"),
        None,
        None,
        threading.Event(),
    )

    assert outcome.auth_abort == AUTH_MESSAGE
    # Both friends' raw/meta/err files were written -- not just the one
    # ahead of the auth failure in the results list.
    assert len(outcome.friends_meta) == 2
    assert (store.round_dir(1) / "good-friend.raw").read_text(encoding="utf-8") == "{}"
    assert (store.round_dir(1) / "broken-friend.raw").exists()
    # The successful friend's claim survived, not just its raw output.
    assert any(c.claim == "the guard is missing" for c in all_claims)
    assert counter == 1


# --- run_rounds: settles the round it found the abort in, then stops -------


def test_run_rounds_settles_the_current_round_then_stops_scheduling_more(monkeypatch, tmp_path):
    judge_a = _spec("judge-a")
    judge_b = _spec("judge-b")
    claim = _claim(1, origin="other/x")
    verdict_payload = {
        "verdicts": [
            {
                "claim_id": claim.id,
                "verdict": "upheld",
                "confidence": "high",
                "reasoning": "checked",
            }
        ]
    }
    judge_a_result = SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout="{}",
        stderr="",
        duration_s=0.1,
        timed_out=False,
        result=NormalizeResult(verdict_payload, [], True),
        failure_reason=None,
        orphans_suspected=False,
    )
    results = [
        (judge_a, rounds_mod._UNKNOWN_CAPABILITY, judge_a_result),
        (judge_b, rounds_mod._UNKNOWN_CAPABILITY, _auth_failed_result()),
    ]
    calls = []

    def _fake_dispatch_round(specs, round_no, *_args, **_kwargs):
        calls.append(round_no)
        return results, AUTH_MESSAGE

    monkeypatch.setattr(crossexam_mod, "dispatch_round", _fake_dispatch_round)

    store = RunStore(tmp_path, "run-auth")
    store.lock()
    store.ledger.append(claim)
    review = ReviewState.replay(store.ledger.records())
    budget = Budget(max_calls=100, max_rounds=5, max_wall_clock_s=1e9, started=0.0)

    outcome = run_rounds(
        [judge_a, judge_b],
        [claim],
        store,
        review,
        {},
        None,
        Path("schema.json"),
        Path("artifact.md"),
        "artifact text",
        None,
        None,
        threading.Event(),
        budget,
        4,
        on_pool=lambda _p: None,
        now=lambda: 0.0,
        first_round=2,
    )

    # Only the round that found the abort ran -- max_rounds=4 would
    # otherwise have scheduled two more.
    assert calls == [2]
    assert outcome.auth_abort == AUTH_MESSAGE
    # The round that found the abort still settled: judge-a's verdict was
    # kept and the claim is no longer sitting at its `contested` seed.
    assert len(outcome.verdicts) == 1
    assert outcome.states[claim.id] != "contested"


# --- decide_exit: auth_abort outranks a partial success ---------------------


def test_an_auth_abort_forces_a_failing_exit_even_when_some_friends_succeeded():
    """The exact shape of the bug report: 2 of 4 friends answered, so
    `any_success` is True -- but the roster is broken and the operator
    needs to know, not see a run that looks like it completed."""
    run_outcome = terminal_outcome(
        mode="report",
        converged=False,
        loop_exhausted=False,
        budget_reason=None,
        blocking_ids=[],
        any_success=True,
        unresolved=False,
        auth_abort=True,
    )
    code = decide_exit(run_outcome, detail=AUTH_MESSAGE)
    assert code == 1


def test_with_no_auth_abort_a_full_success_still_exits_zero():
    """The precedence check does not fire when nothing aborted."""
    run_outcome = terminal_outcome(
        mode="report",
        converged=False,
        loop_exhausted=False,
        budget_reason=None,
        blocking_ids=[],
        any_success=True,
        unresolved=False,
    )
    code = decide_exit(run_outcome)
    assert code == 0

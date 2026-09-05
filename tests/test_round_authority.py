"""Provider-local authority is carried from dispatch into audit sidecars."""

import threading

from afriend import rounds as rounds_mod
from afriend.adapters import Capability, FriendSpec, load_adapters
from afriend.authority import AuthorityPolicy, ExternalToolPolicy
from afriend.ceilings import Budget
from afriend.commands import critique as critique_mod, crossexam as crossexam_mod
from afriend.commands.critique import run_critique
from afriend.commands.crossexam import run_rounds
from afriend.ledger import Claim
from afriend.normalize import NormalizeResult
from afriend.paths import ADAPTER_DIR
from afriend.reviewstate import ReviewState
from afriend.runstore import RunStore
from afriend.spawn import SpawnResult


def _success() -> SpawnResult:
    return SpawnResult(
        argv=["fake"],
        exit_code=0,
        stdout='{"no_findings": true}',
        stderr="",
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


def _verdict_success(claim: Claim) -> SpawnResult:
    payload = {
        "verdicts": [
            {
                "claim_id": claim.id,
                "verdict": "upheld",
                "confidence": "high",
                "reasoning": "checked",
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


def test_critique_persists_carried_scoped_provider_policies(monkeypatch, tmp_path):
    registry = load_adapters(ADAPTER_DIR)
    specs = [
        FriendSpec("agy-ops-0", "agy", "ops", None, None, "doc", 30),
        FriendSpec("codex-ops-0", "codex", "ops", None, None, "doc", 30),
        FriendSpec("claude-ops-0", "claude", "ops", None, None, "doc", 30),
    ]
    policy = AuthorityPolicy.from_grants(["agy"], registry)
    carried = {
        "agy": ExternalToolPolicy.ALLOW,
        "codex": ExternalToolPolicy.DENY,
        "claude": ExternalToolPolicy.DENY,
    }

    def fake_dispatch_round(*_args, **_kwargs):
        return rounds_mod.DispatchRoundOutcome(
            [
                (spec, Capability(False, True, "none"), _success(), carried[spec.cli])
                for spec in specs
            ]
        )

    monkeypatch.setattr(critique_mod, "dispatch_round", fake_dispatch_round)
    store = RunStore(tmp_path, "run-scoped-sidecars")
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome, _claims, _counter = run_critique(
        specs,
        1,
        [],
        0,
        artifact.read_text(),
        store,
        ReviewState(),
        registry,
        None,
        tmp_path / "schema.json",
        artifact,
        None,
        None,
        threading.Event(),
        authority_policy=policy,
    )

    assert {row["name"]: row["external_tool_policy"] for row in outcome.friends_meta} == {
        "agy-ops-0": "allow",
        "codex-ops-0": "deny",
        "claude-ops-0": "deny",
    }
    for spec in specs:
        meta_path = store.friend_paths(1, spec.name)[2]
        assert f"external_tool_policy={carried[spec.cli].value}\n" in meta_path.read_text()


def test_crossexam_persists_carried_scoped_provider_policies(monkeypatch, tmp_path):
    registry = load_adapters(ADAPTER_DIR)
    specs = [
        FriendSpec("agy-ops-0", "agy", "ops", None, None, "doc", 30),
        FriendSpec("codex-ops-0", "codex", "ops", None, None, "doc", 30),
        FriendSpec("claude-ops-0", "claude", "ops", None, None, "doc", 30),
    ]
    policy = AuthorityPolicy.from_grants(["agy"], registry)
    carried = {
        "agy": ExternalToolPolicy.ALLOW,
        "codex": ExternalToolPolicy.DENY,
        "claude": ExternalToolPolicy.DENY,
    }
    claim = _claim()

    def fake_dispatch_round(*_args, **_kwargs):
        return rounds_mod.DispatchRoundOutcome(
            [
                (
                    spec,
                    Capability(False, True, "none"),
                    _verdict_success(claim),
                    carried[spec.cli],
                )
                for spec in specs
            ]
        )

    monkeypatch.setattr(crossexam_mod, "dispatch_round", fake_dispatch_round)

    def forbid_recomputation(_self, provider):
        raise AssertionError(f"sidecar recomputed authority for {provider}")

    monkeypatch.setattr(AuthorityPolicy, "for_provider", forbid_recomputation)
    store = RunStore(tmp_path, "run-scoped-judging-sidecars")
    store.ledger.append(claim)
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact\n")

    outcome = run_rounds(
        specs,
        [claim],
        store,
        ReviewState.replay([claim]),
        registry,
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
        authority_policy=policy,
    )

    assert {row["name"]: row["external_tool_policy"] for row in outcome.friends_meta} == {
        "agy-ops-0": "allow",
        "codex-ops-0": "deny",
        "claude-ops-0": "deny",
    }
    for spec in specs:
        meta_path = store.friend_paths(2, spec.name)[2]
        assert f"external_tool_policy={carried[spec.cli].value}\n" in meta_path.read_text()

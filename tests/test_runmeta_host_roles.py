"""Legacy host-role restoration and independent-authority replay tests."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from afriend import verdicts as vd
from afriend.errors import UsageError
from afriend.ledger import Claim, Ledger, Verdict
from afriend.reviewstate import ReviewState

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _resume_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        resume=str(run_dir),
        out=None,
        artifact=None,
        friend=[],
        allow_external_tools=[],
        allow_unsandboxed_friend=False,
        unsafe_extra_args=None,
        i_accept_unsandboxed=False,
        pass_env=[],
    )


def _run_dir(tmp_path: Path, meta: dict[str, object]) -> Path:
    run_dir = tmp_path / "run-v020"
    run_dir.mkdir()
    round_dir = run_dir / "round-1"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_dir.name,
                "round": 1,
                "question": "merge",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def _resume_meta() -> dict[str, object]:
    meta = load_fixture("run_meta_v020_halted.json")
    meta["invocation"].update(
        {
            "allow_unsandboxed_friend": False,
            "i_accept_unsandboxed": False,
            "unsafe_extra_args": None,
            "pass_env": [],
        }
    )
    return meta


def _legacy_host_resume_meta(mode: str, *, frozen_host: bool) -> dict[str, object]:
    meta = _resume_meta()
    meta["repo_root"] = None
    meta["snapshot_sha"] = None
    meta["invocation"].update(
        {
            "mode": mode,
            "include_self": None,
            "host_provider": None,
        }
    )
    meta["mode"] = mode
    meta["roster"] = [
        {
            "name": "codex-ops",
            "cli": "codex",
            "lens": "ops",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        },
        {
            "name": "fake-security",
            "cli": "fake",
            "lens": "security",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        },
    ]
    meta["friends"] = [
        {
            "name": "codex-ops",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        },
        {
            "name": "fake-security",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        },
    ]
    if frozen_host:
        meta["detected_host"] = "codex"
        meta["effective_include_self"] = True
    return meta


def _legacy_judging_meta(mode: str) -> dict[str, object]:
    meta = _legacy_host_resume_meta(mode, frozen_host=True)
    meta["invocation"]["max_rounds"] = 3
    meta["roster"].append(
        {
            "name": "fake-author",
            "cli": "fake",
            "lens": "author",
            "model": None,
            "effort": None,
            "scope": "doc",
            "timeout": 900,
        }
    )
    meta["friends"].append(
        {
            "name": "fake-author",
            "model": None,
            "effort": None,
            "round": 1,
            "status": "ok",
        }
    )
    return meta


def _legacy_claim() -> Claim:
    return Claim(
        id="c-0001@1",
        supersedes=None,
        origin=["fake/author"],
        lens="author",
        round=1,
        advisory=False,
        severity="high",
        claim="unsafe default",
        location="src/app.py:1",
        evidence="the guard is absent",
        failure_scenario="the operation proceeds",
        suggested_fix="add the guard",
    )


def _append_ledger(run_dir: Path, *records: object) -> None:
    ledger = Ledger(run_dir / "claims.jsonl", root=run_dir.parent)
    for record in records:
        ledger.append(record)


def _friend_audit(name: str, round_no: int, status: str) -> dict[str, object]:
    return {
        "name": name,
        "model": None,
        "effort": None,
        "round": round_no,
        "status": status,
    }


def _write_outstanding_request(run_dir: Path, round_no: int) -> None:
    round_dir = run_dir / f"round-{round_no}"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_dir.name,
                "round": round_no,
                "question": "merge",
            }
        ),
        encoding="utf-8",
    )


def _make_second_loop_halt(meta: dict[str, object]) -> None:
    meta["invocation"]["mode"] = "loop"
    meta["mode"] = "loop"
    meta.update(
        {
            "iterations_run": 2,
            "resume_iteration": 2,
            "rounds_run": 4,
        }
    )
    meta["friends"].extend(
        [
            _friend_audit("codex-ops", 4, "ok"),
            _friend_audit("fake-security", 4, "ok"),
            _friend_audit("fake-author", 4, "ok"),
        ]
    )


def test_legacy_host_roles_are_restored_from_frozen_host_and_audit_rows_follow(tmp_path):
    from afriend.commands.runmeta import _restore_args
    from afriend.report import render

    run_dir = _run_dir(tmp_path, _legacy_host_resume_meta("report", frozen_host=True))

    restored = _restore_args(_resume_args(run_dir))

    host = restored._resume_roster[0]
    assert host.host_self_review is True
    assert host.independent is False
    assert restored._resume_roster[1].independent is True
    assert restored._resume_meta["roster"][0]["host_self_review"] is True
    assert restored._resume_meta["roster"][0]["independent"] is False
    host_row = restored._resume_meta["friends"][0]
    assert host_row["host_self_review"] is True
    assert host_row["independent"] is False
    report = render(ReviewState(), restored._resume_meta)
    rendered_host = next(line for line in report.splitlines() if line.startswith("| codex-ops |"))
    assert "host-self-review (advisory)" in rendered_host
    assert "False" in rendered_host


def test_legacy_resume_leaves_profile_absent_without_reading_session_default(tmp_path, monkeypatch):
    from afriend.cliargs import build_parser
    from afriend.commands import runmeta

    run_dir = _run_dir(tmp_path, _legacy_host_resume_meta("report", frozen_host=True))

    direct = runmeta._restore_args(_resume_args(run_dir))
    assert direct.profile is None

    def session_default_must_not_be_read():
        raise AssertionError("a resume must not read the current session profile default")

    monkeypatch.setattr(runmeta, "load_session_config", session_default_must_not_be_read)
    parsed = build_parser().parse_args(["run", "--resume", str(run_dir)])
    restored, _ = runmeta.validate_run_args(parsed)

    assert restored.profile is None
    assert restored.mode == "report"


@pytest.mark.parametrize("mode", ["crossexam", "gate", "loop"])
def test_legacy_frozen_host_cannot_satisfy_judging_admission(monkeypatch, tmp_path, mode):
    from afriend.commands import friends as friends_module
    from afriend.commands.runmeta import _restore_args
    from afriend.errors import NoFriendsError

    run_dir = _run_dir(tmp_path, _legacy_host_resume_meta(mode, frozen_host=True))
    restored = _restore_args(_resume_args(run_dir))
    monkeypatch.setattr(friends_module, "validate_resume_capabilities", lambda *args: None)

    with pytest.raises(NoFriendsError, match="two independent friends"):
        friends_module.roster_for_run(restored, {}, None, [])


@pytest.mark.parametrize("mode", ["crossexam", "gate", "loop"])
@pytest.mark.parametrize("host_cli", ["codex", "agy"])
def test_ambiguous_legacy_host_role_fails_closed_for_judging_resume(tmp_path, mode, host_cli):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_host_resume_meta(mode, frozen_host=False)
    meta["roster"][0]["cli"] = host_cli
    run_dir = _run_dir(tmp_path, meta)

    with pytest.raises(UsageError, match=r"frozen host-role metadata.*rerun"):
        _restore_args(_resume_args(run_dir))


def test_saved_friend_audit_role_must_match_frozen_roster(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_host_resume_meta("report", frozen_host=True)
    meta["friends"][0].update({"independent": True, "host_self_review": False})
    run_dir = _run_dir(tmp_path, meta)

    with pytest.raises(UsageError, match=r"friends\[0\].*frozen roster"):
        _restore_args(_resume_args(run_dir))


def test_resumed_participation_floor_excludes_successful_legacy_host(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_host_resume_meta("report", frozen_host=True)
    meta["invocation"]["require_friends"] = 1
    meta["friends"][1]["status"] = "failed: exit 1"
    meta["successful_friend_ids"] = ["codex-ops"]
    meta["succeeded_friends"] = 1
    meta["required_friends"] = 1
    run_dir = _run_dir(tmp_path, meta)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_successful_friend_ids == []
    assert restored._resume_meta["succeeded_friends"] == 0


def test_known_legacy_host_verdict_is_reduced_out_of_carried_claim_state(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    meta.update(
        {
            "claim_states": {"c-0001@1": "settled-refuted"},
            "incomplete": False,
        }
    )
    meta["friends"].extend(
        [
            _friend_audit("codex-ops", 2, "ok"),
            _friend_audit("fake-security", 2, "ok"),
        ]
    )
    claim = _legacy_claim()
    host_verdict = Verdict(
        claim_id=claim.id,
        judge="codex/ops",
        round=2,
        verdict="refuted",
        confidence="high",
        evidence_assessment="verified",
        reasoning="host agrees with itself",
        counter_evidence=None,
        amended_claim=None,
    )
    independent_verdict = Verdict(
        claim_id=claim.id,
        judge="fake/security",
        round=2,
        verdict="unproven",
        confidence="low",
        evidence_assessment="unverifiable",
        reasoning="independent evidence was insufficient",
        counter_evidence=None,
        amended_claim=None,
    )
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)
    _append_ledger(run_dir, claim, host_verdict, independent_verdict)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["claim_states"] == {claim.id: "unproven"}


def test_known_legacy_host_failure_does_not_keep_run_incomplete(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    meta.update(
        {
            "claim_states": {"c-0001@1": "settled-upheld"},
            "incomplete": True,
        }
    )
    meta["friends"].extend(
        [
            _friend_audit("codex-ops", 2, "failed: exit 1"),
            _friend_audit("fake-security", 2, "ok"),
        ]
    )
    claim = _legacy_claim()
    independent_verdict = Verdict(
        claim_id=claim.id,
        judge="fake/security",
        round=2,
        verdict="upheld",
        confidence="high",
        evidence_assessment="verified",
        reasoning="independent check",
        counter_evidence=None,
        amended_claim=None,
    )
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)
    _append_ledger(run_dir, claim, independent_verdict)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["claim_states"] == {claim.id: "settled-upheld"}
    assert restored._resume_meta["incomplete"] is False


def test_known_legacy_loop_halt_recomputes_independent_state_and_streak_inputs(tmp_path):
    from afriend.commands.haltstate import resumed_streak
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    meta.update(
        {
            "iterations_run": 2,
            "resume_iteration": 2,
            "rounds_run": 4,
            "dry_streak": 1,
            "halted_round_dry": True,
            "halted_round_failed": False,
            "claim_states": {"c-0001@1": "settled-refuted"},
            "incomplete": False,
        }
    )
    meta["friends"].extend(
        [
            _friend_audit("codex-ops", 2, "ok"),
            _friend_audit("fake-security", 2, "failed: exit 1"),
            _friend_audit("codex-ops", 4, "ok"),
            _friend_audit("fake-security", 4, "failed: exit 1"),
            _friend_audit("fake-author", 4, "failed: exit 1"),
        ]
    )
    claim = _legacy_claim()
    host_verdict = Verdict(
        claim_id=claim.id,
        judge="codex/ops",
        round=2,
        verdict="refuted",
        confidence="high",
        evidence_assessment="verified",
        reasoning="host-only decision",
        counter_evidence=None,
        amended_claim=None,
    )
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)
    _append_ledger(run_dir, claim, host_verdict)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["claim_states"] == {claim.id: "incomplete"}
    assert restored._resume_meta["incomplete"] is True
    assert restored._resume_meta["dry_streak"] == 0
    assert restored._resume_meta["halted_round_dry"] is False
    assert restored._resume_meta["halted_round_failed"] is True
    assert resumed_streak(restored, restored._resume_streak) == 0


def test_legacy_host_only_amendment_successor_fails_closed(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    claim = replace(_legacy_claim(), origin=["fake/author", "fake/security"])
    host_amendment = Verdict(
        claim_id=claim.id,
        judge="codex/ops",
        round=2,
        verdict="amended",
        confidence="high",
        evidence_assessment="verified",
        reasoning="host rewrote its peer-authored claim",
        counter_evidence=None,
        amended_claim="host-authored rewrite",
    )
    successor, _note = vd.build_successor(claim, [host_amendment], 2)
    meta.update(
        {
            "claim_states": {
                claim.id: vd.SUPERSEDED,
                successor.id: vd.CONTESTED,
            },
            "incomplete": False,
        }
    )
    meta["friends"].append(_friend_audit("codex-ops", 2, "ok"))
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)
    _append_ledger(run_dir, claim, host_amendment, successor)

    with pytest.raises(UsageError, match=r"persisted successor.*independent amendments.*rerun"):
        _restore_args(_resume_args(run_dir))


def test_legacy_independent_amendment_successor_is_authenticated(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    claim = replace(_legacy_claim(), origin=["codex/ops"])

    def amendment(judge: str, wording: str) -> Verdict:
        return Verdict(
            claim_id=claim.id,
            judge=judge,
            round=2,
            verdict="amended",
            confidence="high",
            evidence_assessment="verified",
            reasoning="rewrite",
            counter_evidence=None,
            amended_claim=wording,
        )

    host_amendment = amendment("codex/ops", "host-only wording")
    independent_amendments = [
        amendment("fake/security", "independently agreed rewrite"),
        amendment("fake/author", "independently agreed rewrite"),
    ]
    successor, _note = vd.build_successor(claim, independent_amendments, 2)
    meta.update(
        {
            "claim_states": {
                claim.id: vd.SUPERSEDED,
                successor.id: vd.CONTESTED,
            },
            "incomplete": False,
        }
    )
    meta["friends"].extend(
        _friend_audit(name, 2, "ok") for name in ("codex-ops", "fake-security", "fake-author")
    )
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)
    _append_ledger(run_dir, claim, host_amendment, *independent_amendments, successor)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["claim_states"] == {
        claim.id: vd.SUPERSEDED,
        successor.id: vd.CONTESTED,
    }


def test_legacy_loop_health_ignores_failed_host_when_independent_reviewers_succeed(
    tmp_path,
):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    meta["dry_streak"] = 0
    for row in meta["friends"]:
        if row["name"] == "codex-ops":
            row["status"] = "failed: exit 1"
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["dry_streak"] == 1
    assert restored._resume_meta["halted_round_dry"] is True
    assert restored._resume_meta["halted_round_failed"] is False


def test_legacy_loop_health_rejects_host_only_success_when_independents_are_skipped(
    tmp_path,
):
    from afriend.commands.haltstate import resumed_streak
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    meta["dry_streak"] = 1
    for row in meta["friends"]:
        if row["round"] == 1 and row["name"] != "codex-ops":
            row["status"] = "failed: exit 1"
        if row["round"] == 4 and row["name"] != "codex-ops":
            row["status"] = "skipped: repeated failure"
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)

    restored = _restore_args(_resume_args(run_dir))

    assert restored._resume_meta["dry_streak"] == 0
    assert restored._resume_meta["halted_round_dry"] is False
    assert restored._resume_meta["halted_round_failed"] is True
    assert resumed_streak(restored, restored._resume_streak) == 0


def test_known_legacy_judging_fails_closed_without_durable_replay_evidence(tmp_path):
    from afriend.commands.runmeta import _restore_args

    meta = _legacy_judging_meta("loop")
    _make_second_loop_halt(meta)
    meta.update(
        {
            "claim_states": {"c-0001@1": "settled-refuted"},
            "incomplete": False,
        }
    )
    run_dir = _run_dir(tmp_path, meta)
    _write_outstanding_request(run_dir, 4)

    with pytest.raises(UsageError, match=r"durable ledger.*rerun"):
        _restore_args(_resume_args(run_dir))


def test_ambiguous_legacy_report_labels_possible_host_role_unknown(tmp_path):
    from afriend.commands.runmeta import _restore_args
    from afriend.report import render

    run_dir = _run_dir(tmp_path, _legacy_host_resume_meta("report", frozen_host=False))

    restored = _restore_args(_resume_args(run_dir))

    host = restored._resume_roster[0]
    assert host.independent is False
    assert host.host_self_review is False
    host_row = restored._resume_meta["friends"][0]
    assert host_row["independent"] is False
    assert host_row["host_self_review"] is False
    report = render(ReviewState(), restored._resume_meta)
    rendered_host = next(line for line in report.splitlines() if line.startswith("| codex-ops |"))
    assert "legacy role unknown (advisory)" in rendered_host
    assert "independent reviewer" not in rendered_host

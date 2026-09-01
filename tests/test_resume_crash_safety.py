"""c-0004 / c-0008: a crash between applying an orchestrator response and
`_mark_response_consumed` renaming it used to be permanent damage.

`resume_round_one` used to apply-then-mark as two separate steps with no
memory of partial progress between them. A crash in that window -- process
killed, machine loses power, mid-write -- left RESPONSE.json exactly as it
was, still asking to be applied, but the ledger already reflecting SOME of
it. The next `--resume` re-read the identical file from the start:

* Extraction re-appended every finding, including the ones already in the
  ledger, under fresh ids -- permanent duplicate content.
* Merge crashed outright with UsageError, because `canonical_claims` had
  already folded away the `duplicate` id a prior partial application
  removed, and the response still names it. Every subsequent retry hit the
  identical refusal: a transient crash turned into a run permanently stuck.

These tests simulate the crash directly -- write the ledger records an
earlier, interrupted call would have written, leave RESPONSE.json in place
exactly as it would be after a kill -9 -- and call `resume_round_one` as
the retry. Not a mock of the failure: the actual file state a real crash
leaves behind.
"""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import threading

import pytest

from adversarial_friends import isolation, orchestrator
from adversarial_friends.ceilings import Budget
from adversarial_friends.commands import resume as resume_mod
from adversarial_friends.commands.crossexam import CrossexamOutcome
from adversarial_friends.commands.resume import resume_round_one
from adversarial_friends.errors import UsageError
from adversarial_friends.ids import format_claim_id
from adversarial_friends.ledger import Alias, Claim, Resolution, Verdict
from adversarial_friends.merge import canonical_claims
from adversarial_friends.reviewstate import ReviewState
from adversarial_friends.runstore import RunStore
from adversarial_friends.snapshots import SnapshotIdentity

_FINDING = {
    "severity": "high",
    "location": None,
    "evidence": "e",
    "failure_scenario": "f",
    "suggested_fix": "s",
}


def _write_extract_request(round_dir):
    run_id = round_dir.parent.name
    round_no = int(round_dir.name.removeprefix("round-"))
    orchestrator.request_path(round_dir).write_text(
        json.dumps(
            {
                "version": orchestrator.SCHEMA_VERSION,
                "run_id": run_id,
                "round": round_no,
                "question": orchestrator.QUESTION_EXTRACT,
            }
        )
    )


def _write_extract_response(round_dir, claim_texts, friend="codex/ops"):
    orchestrator.response_path(round_dir).write_text(
        json.dumps(
            {
                "version": orchestrator.SCHEMA_VERSION,
                "unparseable": [
                    {
                        "friend": friend,
                        "findings": [{**_FINDING, "claim": text} for text in claim_texts],
                    }
                ],
            }
        )
    )


def _claim(number, text="a finding", lens="ops"):
    return Claim(
        id=format_claim_id(number),
        supersedes=None,
        origin=["codex/ops"],
        lens=lens,
        round=1,
        advisory=False,
        severity="medium",
        claim=text,
        location=None,
        evidence="e",
        failure_scenario="f",
        suggested_fix="s",
    )


def _store(tmp_path, name):
    store = RunStore(tmp_path, name)
    store.lock()
    return store


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _args(mode="crossexam"):
    return argparse.Namespace(
        mode=mode,
        max_rounds=3,
        attributed=False,
        allow_unsandboxed_friend=False,
        _resume_meta={},
    )


def _call_resume_round_one(store, base_round):
    """mode="report" is deliberately NOT in JUDGING_MODES, so
    resume_round_one returns right after applying the response instead of
    going on to dispatch a real judging round -- these tests are about the
    application, not what follows it."""
    return resume_round_one(
        _args(mode="report"),
        store,
        ReviewState.replay(store.ledger.records()),
        [],
        {},
        None,
        None,
        "",
        None,
        None,
        threading.Event(),
        Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0),
        base_round,
        lambda _p: None,
    )


def _assert_reducer_matches_existing_reconstruction(store):
    records = list(store.ledger.records())
    review = ReviewState.replay(records)
    assert review.claims == canonical_claims(records)
    assert review.verdicts == [record for record in records if isinstance(record, Verdict)]
    assert review.aliases == [record for record in records if isinstance(record, Alias)]
    assert review.resolutions == [record for record in records if isinstance(record, Resolution)]
    return review


# --- extraction --------------------------------------------------------------


def test_a_retry_after_a_crash_mid_extraction_does_not_reappend_what_landed(tmp_path):
    store = _store(tmp_path, "run-extract-crash")
    store.ledger.append(_claim(1))
    round_dir = store.round_dir(2)
    _write_extract_request(round_dir)
    _write_extract_response(round_dir, ["finding A", "finding B"])
    # The crash: finding A already landed in the ledger, RESPONSE.json is
    # untouched (never renamed), exactly what kill -9 between the append and
    # the rename leaves behind.
    store.ledger.append(
        Claim(
            id=format_claim_id(2),
            supersedes=None,
            origin=["codex/ops"],
            lens="extracted",
            round=2,
            advisory=False,
            severity="high",
            claim="finding A",
            location=None,
            evidence="e",
            failure_scenario="f",
            suggested_fix="s",
        )
    )

    resumed = _call_resume_round_one(store, 2)

    texts = [c.claim for c in resumed.claims]
    assert texts.count("finding A") == 1, "finding A was re-appended"
    assert "finding B" in texts, "finding B, the actually-remaining one, was dropped"
    assert any("already applied by an earlier" in d for d in resumed.downgrades)


def test_extraction_retry_refuses_a_nonmatching_partial_ledger(tmp_path):
    store = _store(tmp_path, "run-extract-mismatch")
    round_dir = store.round_dir(2)
    _write_extract_request(round_dir)
    _write_extract_response(round_dir, ["finding A", "finding B"])
    store.ledger.append(
        Claim(
            id=format_claim_id(1),
            supersedes=None,
            origin=["codex/ops"],
            lens="extracted",
            round=2,
            advisory=False,
            severity="high",
            claim="different finding",
            location=None,
            evidence="e",
            failure_scenario="f",
            suggested_fix="s",
        )
    )

    with pytest.raises(UsageError, match=r"partial extraction.*does not match"):
        _call_resume_round_one(store, 2)

    assert [record.claim for record in store.ledger.records() if isinstance(record, Claim)] == [
        "different finding"
    ]


def test_a_clean_extraction_retry_reports_no_earlier_attempt(tmp_path):
    """The downgrade addition must not fire when nothing was actually
    interrupted -- the common case, not the crash."""
    store = _store(tmp_path, "run-extract-clean")
    round_dir = store.round_dir(2)
    _write_extract_request(round_dir)
    _write_extract_response(round_dir, ["finding A"])

    resumed = _call_resume_round_one(store, 2)

    assert [c.claim for c in resumed.claims] == ["finding A"]
    assert not any("already applied by an earlier" in d for d in resumed.downgrades)


# --- merge -------------------------------------------------------------------


def test_a_retry_after_a_crash_mid_merge_does_not_crash(tmp_path):
    """The other half of c-0008: without this, the retry raised UsageError
    -- 'c-0002@1 ... is not a claim in this run' -- on the exact id a prior
    attempt had already, correctly, merged away. The run could never be
    resumed again."""
    store = _store(tmp_path, "run-merge-crash")
    store.ledger.append(_claim(1, "defect A"))
    store.ledger.append(_claim(2, "defect A, reworded"))
    store.ledger.append(_claim(3, "defect B"))
    round_dir = store.round_dir(2)
    claims = [_claim(1, "defect A"), _claim(2, "defect A, reworded"), _claim(3, "defect B")]
    orchestrator.write_request(round_dir, "run-merge-crash", 2, claims)
    orchestrator.response_path(round_dir).write_text(
        json.dumps(
            {
                "version": orchestrator.SCHEMA_VERSION,
                "merges": [
                    {"canonical": "c-0001@1", "duplicate": "c-0002@1", "rationale": "same"},
                    {"canonical": "c-0001@1", "duplicate": "c-0003@1", "rationale": "same too"},
                ],
            }
        )
    )
    # The crash: the first merge already landed as an Alias, the second
    # never ran, RESPONSE.json is untouched.
    store.ledger.append(
        Alias(
            canonical="c-0001@1",
            duplicate="c-0002@1",
            round=2,
            source="orchestrator",
            rationale="same",
        )
    )
    partial = _assert_reducer_matches_existing_reconstruction(store)
    assert [alias.duplicate for alias in partial.aliases] == ["c-0002@1"]

    resumed = _call_resume_round_one(store, 2)

    live_ids = {c.id for c in resumed.claims}
    assert "c-0002@1" not in live_ids, "already-merged claim resurfaced"
    assert "c-0003@1" not in live_ids, "the remaining merge was never applied"
    # Only the FRESH alias comes back from this call -- the earlier one was
    # already in the ledger before this call ever ran.
    assert [a.duplicate for a in resumed.aliases] == ["c-0003@1"]
    assert any("already applied by an earlier" in d for d in resumed.downgrades)
    _assert_reducer_matches_existing_reconstruction(store)


def test_merge_retry_refuses_a_nonmatching_partial_alias(tmp_path):
    store = _store(tmp_path, "run-merge-mismatch")
    store.ledger.append(_claim(1, "defect A"))
    store.ledger.append(_claim(2, "defect B"))
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [_claim(1), _claim(2)])
    orchestrator.response_path(round_dir).write_text(
        json.dumps(
            {
                "version": orchestrator.SCHEMA_VERSION,
                "merges": [
                    {
                        "canonical": "c-0001@1",
                        "duplicate": "c-0002@1",
                        "rationale": "same defect",
                    }
                ],
            }
        )
    )
    store.ledger.append(
        Alias(
            canonical="c-9999@1",
            duplicate="c-0002@1",
            round=2,
            source="orchestrator",
            rationale="different decision",
        )
    )

    with pytest.raises(UsageError, match=r"partial merge.*does not match"):
        _call_resume_round_one(store, 2)

    aliases = [record for record in store.ledger.records() if isinstance(record, Alias)]
    assert aliases == [
        Alias(
            canonical="c-9999@1",
            duplicate="c-0002@1",
            round=2,
            source="orchestrator",
            rationale="different decision",
        )
    ]


def test_a_clean_merge_retry_reports_no_earlier_attempt(tmp_path):
    store = _store(tmp_path, "run-merge-clean")
    store.ledger.append(_claim(1, "defect A"))
    store.ledger.append(_claim(2, "defect A, reworded"))
    round_dir = store.round_dir(2)
    claims = [_claim(1, "defect A"), _claim(2, "defect A, reworded")]
    orchestrator.write_request(round_dir, "run-merge-clean", 2, claims)
    orchestrator.response_path(round_dir).write_text(
        json.dumps(
            {
                "version": orchestrator.SCHEMA_VERSION,
                "merges": [
                    {"canonical": "c-0001@1", "duplicate": "c-0002@1", "rationale": "same"},
                ],
            }
        )
    )

    resumed = _call_resume_round_one(store, 2)

    assert [a.duplicate for a in resumed.aliases] == ["c-0002@1"]
    assert not any("already applied by an earlier" in d for d in resumed.downgrades)


def test_response_application_checkpoint_is_durable_before_materialization(tmp_path, monkeypatch):
    store = _store(tmp_path, "run-checkpoint-before-rename")
    store.ledger.append(_claim(1, "defect A"))
    store.ledger.append(_claim(2, "defect A reworded"))
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [_claim(1), _claim(2)])
    response = {
        "version": 1,
        "merges": [{"canonical": "c-0001@1", "duplicate": "c-0002@1", "rationale": "same"}],
    }
    response_path = orchestrator.response_path(round_dir)
    response_path.write_text(json.dumps(response), encoding="utf-8")
    expected_payload = response_path.read_bytes()
    expected_hash = "sha256:" + hashlib.sha256(expected_payload).hexdigest()
    request_hash = (
        "sha256:" + hashlib.sha256(orchestrator.request_path(round_dir).read_bytes()).hexdigest()
    )

    original_create = store.create_owned_bytes

    def fail_applied_create(target, payload):
        if Path(target).name == "RESPONSE.json.applied":
            raise RuntimeError("injected materialization failure")
        return original_create(target, payload)

    monkeypatch.setattr(store, "create_owned_bytes", fail_applied_create)
    with pytest.raises(RuntimeError, match="injected materialization failure"):
        _call_resume_round_one(store, 2)

    checkpoint = json.loads((store.run_dir / "run.json").read_text(encoding="utf-8"))
    assert checkpoint["lifecycle_state"] == "response-applying"
    assert checkpoint["applied_response"] == {
        "version": 1,
        "round": 2,
        "question": "merge",
        "request_sha256": request_hash,
        "sha256": expected_hash,
        "records": 1,
    }
    assert response_path.exists()
    assert not response_path.with_suffix(".json.applying").exists()
    assert not response_path.with_suffix(".json.applied").exists()

    monkeypatch.setattr(store, "create_owned_bytes", original_create)
    _call_resume_round_one(store, 2)
    assert response_path.with_suffix(".json.applied").read_bytes() == expected_payload
    assert not response_path.exists()


def test_response_application_and_digest_use_one_captured_snapshot(tmp_path, monkeypatch):
    store = _store(tmp_path, "run-single-response-snapshot")
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [])
    response = orchestrator.response_path(round_dir)
    original = b'{"version": 1, "merges": []}'
    response.write_bytes(original)
    real_read = store.read_owned_bytes
    response_reads = 0

    def swap_after_read(path, *, max_bytes=32 * 1024 * 1024):
        nonlocal response_reads
        payload = real_read(path, max_bytes=max_bytes)
        if Path(path).name == "RESPONSE.json":
            response_reads += 1
            Path(path).write_bytes(
                b'{"version": 1, "merges": [{"canonical": "x", "duplicate": "y"}]}'
            )
        return payload

    monkeypatch.setattr(store, "read_owned_bytes", swap_after_read)

    with pytest.raises(UsageError, match="changed after validation"):
        _call_resume_round_one(store, 2)

    applied = round_dir / "RESPONSE.json.applied"
    checkpoint = json.loads((store.run_dir / "run.json").read_text())
    assert response_reads == 2
    assert applied.read_bytes() == original
    assert checkpoint["applied_response"]["sha256"] == (
        "sha256:" + hashlib.sha256(original).hexdigest()
    )


def test_response_symlink_is_refused_without_a_state_transition(tmp_path):
    store = _store(tmp_path, "run-response-symlink")
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [])
    outside = tmp_path / "outside-response.json"
    outside.write_text('{"version": 1, "merges": []}', encoding="utf-8")
    response = orchestrator.response_path(round_dir)
    response.symlink_to(outside)

    with pytest.raises(UsageError, match="response artifact must be a regular file"):
        _call_resume_round_one(store, 2)

    assert response.is_symlink()
    assert not (round_dir / "RESPONSE.json.applying").exists()
    assert not (round_dir / "RESPONSE.json.applied").exists()
    assert not (store.run_dir / "run.json").exists()


def test_retry_recovers_matching_applied_response_after_rename(tmp_path):
    store = _store(tmp_path, "run-recover-applied")
    store.ledger.append(_claim(1, "defect A"))
    store.ledger.append(_claim(2, "defect A reworded"))
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [_claim(1), _claim(2)])
    orchestrator.response_path(round_dir).write_text(
        json.dumps({"version": 1, "merges": []}), encoding="utf-8"
    )
    response_path = orchestrator.response_path(round_dir)
    payload = response_path.read_bytes()
    applied_path = response_path.with_suffix(".json.applied")
    response_path.rename(applied_path)
    store.write_run_json(
        {
            "lifecycle_state": "response-applied",
            "applied_response": {
                "version": 1,
                "round": 2,
                "question": "merge",
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "records": 0,
            },
        }
    )
    args = _args(mode="report")
    args._resume_meta = json.loads((store.run_dir / "run.json").read_text())

    resumed = resume_round_one(
        args,
        store,
        ReviewState.replay(store.ledger.records()),
        [],
        {},
        None,
        None,
        "",
        None,
        None,
        threading.Event(),
        Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0),
        2,
        lambda _p: None,
    )

    assert resumed.aliases == []
    assert applied_path.read_bytes() == payload


def test_tampered_applied_response_is_refused_without_mutation(tmp_path):
    store = _store(tmp_path, "run-tampered-applied")
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [])
    applied_path = round_dir / "RESPONSE.json.applied"
    applied_path.write_text('{"version": 1, "merges": []}', encoding="utf-8")
    store.write_run_json(
        {
            "lifecycle_state": "response-applied",
            "applied_response": {
                "version": 1,
                "round": 2,
                "question": "merge",
                "sha256": "sha256:" + "0" * 64,
                "records": 0,
            },
        }
    )
    before = {
        "run": (store.run_dir / "run.json").read_bytes(),
        "applied": applied_path.read_bytes(),
        "ledger": (
            (store.run_dir / "claims.jsonl").read_bytes()
            if (store.run_dir / "claims.jsonl").exists()
            else b""
        ),
    }
    args = _args(mode="report")
    args._resume_meta = json.loads(before["run"])

    with pytest.raises(UsageError, match=r"applied response.*hash"):
        resume_round_one(
            args,
            store,
            ReviewState.replay(store.ledger.records()),
            [],
            {},
            None,
            None,
            "",
            None,
            None,
            threading.Event(),
            Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0),
            2,
            lambda _p: None,
        )

    assert (store.run_dir / "run.json").read_bytes() == before["run"]
    assert applied_path.read_bytes() == before["applied"]
    ledger = store.run_dir / "claims.jsonl"
    assert (ledger.read_bytes() if ledger.exists() else b"") == before["ledger"]


def test_hostile_request_is_refused_without_applying_or_rewriting_response(tmp_path):
    store = _store(tmp_path, "run-hostile-request")
    store.ledger.append(_claim(1))
    round_dir = store.round_dir(2)
    outside = tmp_path / "outside-request.json"
    outside.write_text('{"question": "merge"}', encoding="utf-8")
    orchestrator.request_path(round_dir).symlink_to(outside)
    response = orchestrator.response_path(round_dir)
    response.write_text('{"version": 1, "merges": []}', encoding="utf-8")
    before = response.read_bytes()

    with pytest.raises(UsageError, match=r"orchestrator request.*regular file"):
        _call_resume_round_one(store, 2)

    assert response.read_bytes() == before
    assert not response.with_suffix(".json.applied").exists()
    assert not (store.run_dir / "run.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"{malformed",
        b'{"version": 1, "merges": "not-an-array"}',
        b'{"version": 1, "merges": [{"canonical": "missing", "duplicate": "also-missing"}]}',
    ],
)
def test_invalid_response_is_refused_without_any_state_transition(tmp_path, payload):
    store = _store(tmp_path, "run-invalid-response")
    store.ledger.append(_claim(1))
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [_claim(1)])
    response = orchestrator.response_path(round_dir)
    response.write_bytes(payload)
    before = response.read_bytes()

    with pytest.raises(UsageError):
        _call_resume_round_one(store, 2)

    assert response.read_bytes() == before
    assert not (round_dir / "RESPONSE.json.applying").exists()
    assert not (round_dir / "RESPONSE.json.applied").exists()
    assert not (store.run_dir / "run.json").exists()


@pytest.mark.parametrize(
    "request_payload",
    [
        {},
        {"version": 1, "run_id": "other", "round": 2, "question": "merge"},
        {"version": 1, "run_id": "run-request-binding", "round": 3, "question": "merge"},
        {"version": 1, "run_id": "run-request-binding", "round": 2, "question": "unknown"},
        {"version": 1, "run_id": "run-request-binding", "round": 2, "question": 1},
    ],
)
def test_response_requires_the_exact_outstanding_request_before_mutation(tmp_path, request_payload):
    store = _store(tmp_path, "run-request-binding")
    store.ledger.append(_claim(1))
    round_dir = store.round_dir(2)
    orchestrator.request_path(round_dir).write_text(json.dumps(request_payload), encoding="utf-8")
    response = orchestrator.response_path(round_dir)
    response.write_text('{"version": 1, "merges": []}', encoding="utf-8")
    before = {
        "ledger": (store.run_dir / "claims.jsonl").read_bytes(),
        "request": orchestrator.request_path(round_dir).read_bytes(),
        "response": response.read_bytes(),
    }

    with pytest.raises(UsageError, match="outstanding orchestrator request"):
        _call_resume_round_one(store, 2)

    assert (store.run_dir / "claims.jsonl").read_bytes() == before["ledger"]
    assert orchestrator.request_path(round_dir).read_bytes() == before["request"]
    assert response.read_bytes() == before["response"]
    assert not list(round_dir.glob("RESPONSE.json.*"))
    assert not (store.run_dir / "run.json").exists()


def test_valid_resume_repairs_supported_legacy_run_permissions(tmp_path):
    store = _store(tmp_path, "run-permission-repair")
    round_dir = store.round_dir(2)
    _write_extract_request(round_dir)
    _write_extract_response(round_dir, ["private finding"])
    for path in [store.run_dir, round_dir]:
        path.chmod(0o755)
    for path in store.run_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o644)

    _call_resume_round_one(store, 2)

    assert stat.S_IMODE(store.run_dir.lstat().st_mode) == 0o700
    for path in store.run_dir.rglob("*"):
        if path.is_symlink():
            continue
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.lstat().st_mode) == expected


def test_judging_exception_leaves_an_authenticated_replayable_transition(tmp_path, monkeypatch):
    store = _store(tmp_path, "run-judging-interrupted")
    store.ledger.append(_claim(1))
    round_dir = store.round_dir(2)
    orchestrator.write_request(round_dir, store.run_id, 2, [_claim(1)])
    response = orchestrator.response_path(round_dir)
    response.write_text('{"version": 1, "merges": []}', encoding="utf-8")
    args = _args(mode="crossexam")

    monkeypatch.setattr(
        resume_mod,
        "run_rounds",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected judging interruption")
        ),
    )
    with pytest.raises(RuntimeError, match="judging interruption"):
        resume_round_one(
            args,
            store,
            ReviewState.replay(store.ledger.records()),
            [],
            {},
            None,
            None,
            "",
            None,
            None,
            threading.Event(),
            Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0),
            2,
            lambda _p: None,
        )

    applied = response.with_suffix(".json.applied")
    checkpoint = json.loads((store.run_dir / "run.json").read_text(encoding="utf-8"))
    assert checkpoint["lifecycle_state"] == "response-applied"
    assert applied.exists() and not response.exists()

    monkeypatch.setattr(
        resume_mod,
        "run_rounds",
        lambda _specs, claims, *_args, **_kwargs: CrossexamOutcome(claims=list(claims)),
    )
    retry_args = _args(mode="crossexam")
    retry_args._resume_meta = checkpoint
    resumed = resume_round_one(
        retry_args,
        store,
        ReviewState.replay(store.ledger.records()),
        [],
        {},
        None,
        None,
        "",
        None,
        None,
        threading.Event(),
        Budget(max_calls=100, max_wall_clock_s=3600.0, started=0.0),
        2,
        lambda _p: None,
    )

    assert resumed.cross is not None
    assert [record.id for record in store.ledger.claims()] == ["c-0001@1"]


def test_missing_snapshot_refusal_leaves_all_resume_state_untouched(tmp_path):
    """Snapshot verification precedes response application. If the saved
    commit vanished, a retry must leave the audit response, ledger, and
    metadata exactly as the operator supplied them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# contract\n")
    store = _store(tmp_path, "run-snapshot-refusal")
    frozen, digest = store.artifact_copy(artifact)
    commit = isolation.snapshot_commit(repo)
    meta = {
        "repo_root": str(repo),
        "snapshot_sha": commit,
        "artifact_path": str(artifact),
        "artifact_hash": digest,
    }
    store.write_run_json(meta)
    round_dir = store.round_dir(1)
    (round_dir / "RESPONSE.json").write_text('{"version": 1, "merges": []}')
    before = {
        "run": (store.run_dir / "run.json").read_bytes(),
        "ledger": (store.run_dir / "claims.jsonl").read_bytes()
        if (store.run_dir / "claims.jsonl").exists()
        else b"",
        "response": (round_dir / "RESPONSE.json").read_bytes(),
    }

    with pytest.raises(UsageError, match=r"saved snapshot.*missing"):
        SnapshotIdentity.from_meta({**meta, "snapshot_sha": "0" * 40}).verify(frozen)

    assert (store.run_dir / "run.json").read_bytes() == before["run"]
    ledger = store.run_dir / "claims.jsonl"
    assert (ledger.read_bytes() if ledger.exists() else b"") == before["ledger"]
    assert (round_dir / "RESPONSE.json").read_bytes() == before["response"]

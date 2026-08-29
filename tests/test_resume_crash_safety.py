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
import json
import threading

from adversarial_friends import orchestrator
from adversarial_friends.ceilings import Budget
from adversarial_friends.commands.resume import resume_round_one
from adversarial_friends.ids import format_claim_id
from adversarial_friends.ledger import Alias, Claim
from adversarial_friends.runstore import RunStore

_FINDING = {
    "severity": "high",
    "location": None,
    "evidence": "e",
    "failure_scenario": "f",
    "suggested_fix": "s",
}


def _write_extract_request(round_dir):
    orchestrator.request_path(round_dir).write_text(
        json.dumps(
            {"version": orchestrator.SCHEMA_VERSION, "question": orchestrator.QUESTION_EXTRACT}
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

    resumed = _call_resume_round_one(store, 2)

    live_ids = {c.id for c in resumed.claims}
    assert "c-0002@1" not in live_ids, "already-merged claim resurfaced"
    assert "c-0003@1" not in live_ids, "the remaining merge was never applied"
    # Only the FRESH alias comes back from this call -- the earlier one was
    # already in the ledger before this call ever ran.
    assert [a.duplicate for a in resumed.aliases] == ["c-0003@1"]
    assert any("already applied by an earlier" in d for d in resumed.downgrades)


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

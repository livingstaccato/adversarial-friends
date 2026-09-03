"""Safe, append-only lifecycle events for a run."""

import json

import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.events import EventRecord, read_events
from adversarial_friends.runstore import RunStore


def test_event_records_reject_unsafe_payload_fields():
    with pytest.raises(UsageError, match="not allowed"):
        EventRecord.create(
            "friend_finished", {"provider": "fake", "stdout": "secret"}, run_id="run-events"
        )


def test_event_records_are_versioned_and_bounded():
    record = EventRecord.create(
        "friend_finished",
        {
            "friend": "fake-security-0",
            "provider": "fake",
            "lens": "security",
            "round": 1,
            "duration_s": 1.25,
            "status": "succeeded",
        },
        run_id="run-events",
        timestamp="2026-09-03T12:34:56Z",
    )
    assert record.schema_version == 1
    assert record.to_dict() == {
        "schema_version": 1,
        "timestamp": "2026-09-03T12:34:56Z",
        "run_id": "run-events",
        "type": "friend_finished",
        "payload": {
            "friend": "fake-security-0",
            "provider": "fake",
            "lens": "security",
            "round": 1,
            "duration_s": 1.25,
            "status": "succeeded",
        },
    }
    with pytest.raises(UsageError, match="bounded"):
        EventRecord.create(
            "run_started",
            {"mode": "report", "profile": "x" * 257, "status": "started"},
            run_id="run-events",
        )


def test_writer_appends_private_jsonl_and_reader_ignores_only_torn_tail(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    writer = store.events_writer()
    writer.append(
        EventRecord.create(
            "run_started",
            {"mode": "report", "profile": "quick", "status": "started"},
            run_id="run-events",
        )
    )
    writer.append(
        EventRecord.create(
            "run_finished",
            {"status": "completed", "next_action": "inspect_report"},
            run_id="run-events",
        )
    )
    path = store.events_path()
    assert path.stat().st_mode & 0o777 == 0o600
    with path.open("ab") as handle:
        handle.write(b'{"version": 1')
    assert [item.type for item in read_events(path, root=store.root)] == [
        "run_started",
        "run_finished",
    ]


def test_reader_rejects_a_malformed_complete_record(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    path = store.events_path()
    path.write_text(
        '{"schema_version": 1, "timestamp": "2026-09-03T12:34:56Z", "run_id": "run-events", "type": "run_started", '
        '"payload": {"mode": "report", "profile": "quick", "status": "started"}}\n'
        "not-json\n"
    )
    path.chmod(0o600)
    with pytest.raises(UsageError, match=r"events\.jsonl line 2"):
        read_events(path, root=store.root)


def test_reader_rejects_complete_records_with_unsafe_payloads(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    path = store.events_path()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": "2026-09-03T12:34:56Z",
                "run_id": "run-events",
                "type": "run_started",
                "payload": {"prompt": "no"},
            }
        )
        + "\n"
    )
    path.chmod(0o600)
    with pytest.raises(UsageError, match="not allowed"):
        read_events(path, root=store.root)

"""Safe, append-only lifecycle events for a run."""

import json

import pytest

from adversarial_friends.commands.run import _validate_repository_scope_anchor
from adversarial_friends.errors import UsageError
from adversarial_friends.events import MAX_EVENT_BYTES, EventRecord, read_events
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


def test_run_started_accepts_only_declared_repository_scope_modes():
    record = EventRecord.create(
        "run_started",
        {
            "mode": "report",
            "profile": "quick",
            "status": "started",
            "repository_scope_mode": "explicit",
        },
        run_id="run-events",
    )

    assert record.payload["repository_scope_mode"] == "explicit"
    for invalid in ("inferred", True, ["automatic"]):
        with pytest.raises(UsageError, match="repository_scope_mode"):
            EventRecord.create(
                "run_started",
                {
                    "mode": "report",
                    "profile": "quick",
                    "status": "started",
                    "repository_scope_mode": invalid,
                },
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


def test_scope_anchor_is_the_first_run_started_not_a_later_resume_event(tmp_path):
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
            "run_started",
            {
                "mode": "report",
                "profile": "quick",
                "status": "started",
                "repository_scope_mode": "automatic",
            },
            run_id="run-events",
        )
    )

    with pytest.raises(UsageError, match=r"original run_started.*no repository_scope_mode"):
        _validate_repository_scope_anchor(store, "automatic")


def test_scope_anchor_allows_saved_and_anchored_modes_to_both_be_absent(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    store.events_writer().append(
        EventRecord.create(
            "run_started",
            {"mode": "report", "profile": "quick", "status": "started"},
            run_id=store.run_id,
        )
    )

    _validate_repository_scope_anchor(store, None)


def test_scope_anchor_allows_fieldless_run_with_no_event_file(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")

    _validate_repository_scope_anchor(store, None)


def test_scope_anchor_rejects_declared_mode_with_no_event_file(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")

    with pytest.raises(UsageError, match=r"cannot resume:.*lifecycle event"):
        _validate_repository_scope_anchor(store, "automatic")


@pytest.mark.parametrize(
    "later",
    [
        b"not-json\n",
        b"x" * (MAX_EVENT_BYTES + 1) + b"\n",
        b'{"torn":',
    ],
    ids=("malformed", "oversized", "torn"),
)
def test_scope_anchor_ignores_all_later_telemetry(tmp_path, later):
    store = RunStore(tmp_path / "runs", "run-events")
    store.events_writer().append(
        EventRecord.create(
            "run_started",
            {
                "mode": "report",
                "profile": "quick",
                "status": "started",
                "repository_scope_mode": "automatic",
            },
            run_id=store.run_id,
        )
    )
    with store.events_path().open("ab") as handle:
        handle.write(later)

    _validate_repository_scope_anchor(store, "automatic")


def test_scope_anchor_requires_physical_first_record_to_be_run_started(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    writer = store.events_writer()
    writer.append(
        EventRecord.create(
            "round_finished",
            {"round": 1, "status": "completed"},
            run_id=store.run_id,
        )
    )
    writer.append(
        EventRecord.create(
            "run_started",
            {
                "mode": "report",
                "profile": "quick",
                "status": "started",
                "repository_scope_mode": "automatic",
            },
            run_id=store.run_id,
        )
    )

    with pytest.raises(UsageError, match=r"first lifecycle event.*run_started"):
        _validate_repository_scope_anchor(store, "automatic")


def test_scope_anchor_requires_physical_first_record_to_match_store_run_id(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    copied = EventRecord.create(
        "run_started",
        {
            "mode": "report",
            "profile": "quick",
            "status": "started",
            "repository_scope_mode": "automatic",
        },
        run_id="run-copied",
    )
    store.events_path().write_text(json.dumps(copied.to_dict()) + "\n", encoding="utf-8")
    store.events_path().chmod(0o600)

    with pytest.raises(UsageError, match=r"first lifecycle event.*run_id"):
        _validate_repository_scope_anchor(store, "automatic")


@pytest.mark.parametrize("saved_mode", ["automatic", None])
def test_scope_anchor_normalizes_first_record_io_errors(tmp_path, saved_mode):
    store = RunStore(tmp_path / "runs", "run-events")
    store.events_path().mkdir()

    with pytest.raises(UsageError, match=r"cannot resume:.*lifecycle event"):
        _validate_repository_scope_anchor(store, saved_mode)


@pytest.mark.parametrize("saved_mode", ["automatic", None])
def test_scope_anchor_bounds_only_the_first_physical_record(tmp_path, saved_mode):
    store = RunStore(tmp_path / "runs", "run-events")
    store.events_path().write_bytes(b"x" * (MAX_EVENT_BYTES + 1) + b"\n")
    store.events_path().chmod(0o600)

    with pytest.raises(UsageError, match=r"cannot resume:.*first lifecycle event.*long"):
        _validate_repository_scope_anchor(store, saved_mode)


def test_scope_anchor_rejects_malformed_first_event_when_saved_mode_is_absent(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    store.events_path().write_bytes(b"not-json\n")
    store.events_path().chmod(0o600)

    with pytest.raises(UsageError, match=r"cannot resume:.*first lifecycle event.*invalid"):
        _validate_repository_scope_anchor(store, None)


def test_scope_anchor_rejects_symlinked_event_file_when_saved_mode_is_absent(tmp_path):
    store = RunStore(tmp_path / "runs", "run-events")
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text("not trusted\n", encoding="utf-8")
    store.events_path().symlink_to(outside)

    with pytest.raises(UsageError):
        _validate_repository_scope_anchor(store, None)

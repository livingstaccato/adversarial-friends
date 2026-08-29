"""Tests for failure classification and repeat detection (spec §14, §7.2).

The asymmetry between the two mechanisms is the point. Auth classification
aborts the whole run, so it fires only on a marker an adapter declared;
repeat detection merely disables one friend, so it is allowed to infer.
"""

from pathlib import Path

from adversarial_friends import failures
from adversarial_friends.adapters import Adapter, AuthMarkers
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.spawn import SpawnResult


def outcome(reason="exit 1", exit_code=1, payload=None, timed_out=False):
    return SpawnResult(
        argv=["x"],
        exit_code=exit_code,
        stdout="",
        stderr="",
        duration_s=0.0,
        timed_out=timed_out,
        result=NormalizeResult(payload, [], payload is not None),
        failure_reason=reason,
        orphans_suspected=False,
    )


def adapter(**auth):
    return Adapter(
        name="x",
        binary="x",
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        auth=AuthMarkers(**auth),
    )


# --- Classification (§14) --------------------------------------------------


def test_an_adapter_with_no_markers_never_classifies_auth():
    """The honest default. No shipped adapter has had a real auth failure
    captured, and inventing a marker is exactly the stderr-guessing §14
    rejects."""
    assert failures.classify(outcome(), adapter()) == failures.UNKNOWN


def test_a_declared_exit_code_classifies_auth():
    assert failures.classify(outcome(exit_code=41), adapter(exit_codes=(41,))) == failures.AUTH


def test_a_declared_payload_marker_classifies_auth():
    payload = {"error": {"type": "authentication_error"}}
    markers = adapter(paths=(("error.type", "authentication_error"),))
    assert failures.classify(outcome(payload=payload), markers) == failures.AUTH


def test_a_different_payload_value_does_not_classify():
    payload = {"error": {"type": "rate_limit"}}
    markers = adapter(paths=(("error.type", "authentication_error"),))
    assert failures.classify(outcome(payload=payload), markers) == failures.UNKNOWN


def test_a_success_is_never_auth():
    assert failures.classify(outcome(reason=None), adapter(exit_codes=(1,))) == failures.UNKNOWN


def test_a_timeout_is_never_auth():
    """§14 gives timeout precedence over output inspection: a killed
    friend's truncated output must not enter any interpretation path."""
    killed = outcome(exit_code=41, timed_out=True)
    assert failures.classify(killed, adapter(exit_codes=(41,))) == failures.UNKNOWN


def test_an_unknown_adapter_classifies_nothing():
    assert failures.classify(outcome(), None) == failures.UNKNOWN


# --- Repeat detection (§7.2's cost argument) -------------------------------


def test_two_identical_failures_disable_a_friend():
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    assert not tracker.is_disabled("codex-ops")
    tracker.record("codex-ops", outcome())
    assert tracker.is_disabled("codex-ops")


def test_a_different_failure_resets_the_count():
    """Two different failures are not evidence of a deterministic one."""
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome(reason="exit 1"))
    tracker.record("codex-ops", outcome(reason="timed out"))
    assert not tracker.is_disabled("codex-ops")


def test_a_success_clears_a_prior_failure():
    """A friend that failed once and then worked has told us the failure was
    transient -- which is exactly the case that must not be disabled."""
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    tracker.record("codex-ops", outcome(reason=None))
    tracker.record("codex-ops", outcome())
    assert not tracker.is_disabled("codex-ops")


def test_friends_are_tracked_independently():
    tracker = failures.RepeatTracker()
    for _ in range(2):
        tracker.record("broken", outcome())
        tracker.record("fine", outcome(reason=None))
    assert tracker.is_disabled("broken")
    assert not tracker.is_disabled("fine")


def test_a_restored_tracker_keeps_a_friend_disabled():
    """c-0002. A RepeatTracker lives only in the process that built it, and
    `--resume` is a new process -- so a friend disabled for repeated
    failure in an earlier iteration was silently un-disabled the moment
    that process exited for its orchestrator halt, and could be
    re-dispatched and re-announced as disabled after every resume."""
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    tracker.record("codex-ops", outcome())
    assert tracker.is_disabled("codex-ops")

    restored = failures.RepeatTracker.restore(tracker.snapshot())

    assert restored.is_disabled("codex-ops")
    assert restored.note("codex-ops") == tracker.note("codex-ops")


def test_a_restored_tracker_still_resets_on_success():
    """Restoring must not freeze a friend's history -- a friend that was
    disabled, then genuinely recovers, must be able to clear again exactly
    as it would have in the process that disabled it."""
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    tracker.record("codex-ops", outcome())
    restored = failures.RepeatTracker.restore(tracker.snapshot())

    restored.record("codex-ops", outcome(reason=None))

    assert not restored.is_disabled("codex-ops")


def test_a_restored_tracker_keeps_counting_toward_the_limit():
    """One prior failure, restored, plus one more after resume must still
    disable -- the count has to survive the round trip, not just the
    disabled set, or a friend one failure away from disabled loses that
    history on every halt."""
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    assert not tracker.is_disabled("codex-ops")
    restored = failures.RepeatTracker.restore(tracker.snapshot())

    restored.record("codex-ops", outcome())

    assert restored.is_disabled("codex-ops")


def test_restoring_empty_data_is_the_same_as_a_fresh_tracker():
    """A halt written by a version predating this field has no
    `repeat_tracker` key. Absent must not crash and must not disable
    anyone -- restoring nothing is restoring a clean slate."""
    restored = failures.RepeatTracker.restore({})
    assert not restored.is_disabled("codex-ops")
    restored.record("codex-ops", outcome())
    restored.record("codex-ops", outcome())
    assert restored.is_disabled("codex-ops")


def test_snapshot_round_trips_through_json():
    """The whole point: this dict is written to run.json and read back."""
    import json

    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    tracker.record("codex-ops", outcome())
    tracker.record("claude-security", outcome(reason="timed out"))

    reloaded = json.loads(json.dumps(tracker.snapshot()))
    restored = failures.RepeatTracker.restore(reloaded)

    assert restored.is_disabled("codex-ops")
    assert not restored.is_disabled("claude-security")


def test_the_signature_ignores_stderr():
    """A CLI printing a timestamp or request id would otherwise look like a
    different failure every round and never trip the rule."""
    first = outcome()
    second = outcome()
    object.__setattr__(second, "stderr", "request-id: abc123")
    assert failures.failure_signature(first) == failures.failure_signature(second)


def test_the_note_explains_why_it_stopped_dispatching():
    tracker = failures.RepeatTracker()
    tracker.record("codex-ops", outcome())
    tracker.record("codex-ops", outcome())
    note = tracker.note("codex-ops")
    assert "not be dispatched again" in note
    assert "broken, not" in note


def test_the_abort_message_carries_adapter_remediation():
    """§14: remediation is a message, not a command -- gemini's is a product
    migration behind a URL, not `gemini login`."""
    with_prose = adapter(exit_codes=(1,), remediation="Run `codex login` to sign in.")
    message = failures.auth_abort_message("codex-ops", with_prose)
    assert "codex login" in message
    assert "deterministic" in message


def test_the_abort_message_says_nothing_extra_without_remediation():
    message = failures.auth_abort_message("codex-ops", adapter(exit_codes=(1,)))
    assert message.endswith("spending them.")


# --- stderr markers ----------------------------------------------------------

AGY_CAPTURED = "Error: authentication required. Run 'agy' to log in, then retry.\n"


def _with_stderr(text, **kw):
    import dataclasses

    return dataclasses.replace(outcome(**kw), stderr=text)


def test_a_declared_stderr_marker_classifies_auth():
    """The first marker captured from a REAL auth failure was agy's, and it
    lives only on stderr: exit 1 (shared with unrelated errors) and empty
    stdout. Payload paths and exclusive exit codes could not express it."""
    a = adapter(stderr=("authentication required. Run 'agy' to log in",))
    assert failures.classify(_with_stderr(AGY_CAPTURED), a) == failures.AUTH


def test_the_network_timeout_string_is_not_an_auth_failure():
    """The trap recorded in failures.py: with the network unreachable agy
    says "authentication timed out". Under the real marker that must stay
    UNKNOWN, or every network-denied run aborts as an auth failure."""
    a = adapter(stderr=("authentication required. Run 'agy' to log in",))
    blocked = _with_stderr(
        "Error: authentication timed out.\nError: authentication failed or timed out\n"
    )
    assert failures.classify(blocked, a) == failures.UNKNOWN


def test_timeout_still_outranks_a_stderr_marker():
    a = adapter(stderr=("authentication required",))
    assert failures.classify(_with_stderr(AGY_CAPTURED, timed_out=True), a) == failures.UNKNOWN


def test_agy_ships_the_captured_marker():
    from adversarial_friends.adapters import load_adapters
    from adversarial_friends.paths import ADAPTER_DIR

    agy = load_adapters(ADAPTER_DIR)["agy"]
    assert agy.auth.declared()
    assert failures.classify(_with_stderr(AGY_CAPTURED), agy) == failures.AUTH
    assert "agy" in agy.auth.remediation


# --- opencode: the first structured auth marker (§14) -----------------------


def _opencode():
    from adversarial_friends.adapters import load_adapters
    from adversarial_friends.paths import ADAPTER_DIR

    return load_adapters(ADAPTER_DIR)["opencode"]


def _opencode_payload(name="ProviderAuthError"):
    """The captured envelope, normalized the way dispatch would deliver it."""
    import json

    raw = (Path(__file__).parent / "fixtures" / "opencode_provider_auth_error.ndjson").read_text(
        encoding="utf-8"
    )
    return json.loads(raw.strip().splitlines()[0].replace("ProviderAuthError", name))


def test_opencodes_provider_auth_error_is_classified_as_auth():
    """Captured from a real confined run: opencode reached its provider with
    no credential in its allowlisted environment and said so in its own
    structured output. §14 asks for exactly this shape -- the CLI naming the
    failure itself -- rather than a guessed stderr substring. It is the first
    marker of the kind §14 actually describes; agy's is a stderr sentence,
    allowed only as a recorded divergence."""
    import dataclasses

    result = NormalizeResult(payload=_opencode_payload(), errors=[], succeeded=False)
    cast = dataclasses.replace(outcome(exit_code=1), result=result)
    assert failures.classify(cast, _opencode()) == failures.AUTH


def test_an_ordinary_opencode_error_is_not_an_auth_failure():
    """The marker names one error type. A provider that is merely broken --
    the same envelope, a different `name` -- must stay unclassified, or a
    run aborts on a failure a retry would have survived."""
    import dataclasses

    result = NormalizeResult(payload=_opencode_payload("UnknownError"), errors=[], succeeded=False)
    cast = dataclasses.replace(outcome(exit_code=1), result=result)
    assert failures.classify(cast, _opencode()) != failures.AUTH

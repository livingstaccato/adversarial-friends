"""Tests for failure classification and repeat detection (spec §14, §7.2).

The asymmetry between the two mechanisms is the point. Auth classification
aborts the whole run, so it fires only on a marker an adapter declared;
repeat detection merely disables one friend, so it is allowed to infer.
"""

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

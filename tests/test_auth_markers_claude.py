"""claude's auth marker, checked against its real captured failure (§14).

Captured 2026-08-28. claude keeps credentials in the macOS Keychain, so there
is no file to remove or copy: the failure was provoked by running it under
this runner's own sandbox with no `~/Library/Keychains` grant, which denies
READ access for one child process. Nothing was logged out and nothing was
written.

That is also why claude is the one adapter this project declines to confine
in normal use -- granting the Keychain would hand a friend every credential
the operator has, which is worse than the gap it closes.
"""

import json

from afriend import failures
from afriend.adapters import load_adapters
from afriend.normalize import normalize
from afriend.paths import ADAPTER_DIR
from afriend.spawn import SpawnResult

# Verbatim shape from the capture, trimmed to the fields that matter.
CAPTURED = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": None,
        "result": "Not logged in · Please run /login",
    }
)
# Same envelope, a DIFFERENT API failure. Everything auth-shaped about the
# object above is still true here.
RATE_LIMITED = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 429,
        "result": "API Error: 429 rate limit exceeded",
    }
)


def _claude():
    return load_adapters(ADAPTER_DIR)["claude"]


def _outcome(raw: str) -> SpawnResult:
    adapter = _claude()
    result = normalize(raw, envelope=adapter.envelope, structured_output=True)
    return SpawnResult(
        argv=["claude"],
        exit_code=1,
        stdout=raw,
        stderr="",
        duration_s=1.0,
        timed_out=False,
        result=result,
        failure_reason="exit 1",
        orphans_suspected=False,
    )


def test_the_captured_failure_classifies_as_auth():
    assert failures.classify(_outcome(CAPTURED), _claude()) == failures.AUTH


def test_the_marker_survives_normalization_into_the_payload():
    """classify() reads the parsed payload, not raw stdout. The auth object
    fails the claim schema, so this checks the failing result still carries
    the payload the marker is keyed on."""
    outcome = _outcome(CAPTURED)
    assert outcome.result.succeeded is False
    assert outcome.result.payload is not None
    assert outcome.result.payload["result"] == "Not logged in · Please run /login"


def test_a_rate_limit_is_not_an_auth_failure():
    """The reason the marker is not `is_error` or `terminal_reason`: both are
    identical here. Classifying this as auth would stop dispatching a friend
    that only needed to wait."""
    assert failures.classify(_outcome(RATE_LIMITED), _claude()) == failures.UNKNOWN


def test_an_unparseable_failure_is_not_auth():
    assert failures.classify(_outcome("connection reset by peer"), _claude()) == failures.UNKNOWN


def test_a_timeout_is_never_auth_even_carrying_the_marker():
    outcome = _outcome(CAPTURED)
    outcome.timed_out = True
    assert failures.classify(outcome, _claude()) == failures.UNKNOWN


def test_the_remediation_names_the_keychain_constraint():
    """A confined claude cannot reach its own credentials, so "run /login"
    alone would send a reader in circles."""
    remediation = _claude().auth.remediation
    assert "login" in remediation and "Keychain" in remediation


def test_a_successful_run_is_never_classified():
    outcome = _outcome(CAPTURED)
    outcome.failure_reason = None
    assert failures.classify(outcome, _claude()) == failures.UNKNOWN

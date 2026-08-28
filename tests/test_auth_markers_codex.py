"""codex's auth markers, checked against its real captured failure (§14).

Captured 2026-08-28 from codex 0.150.1 by pointing CODEX_HOME at an empty
directory -- the CLI then finds no credentials of its own while the real
~/.codex is neither read nor written. That is the safe way to provoke an
auth failure: you need the ABSENCE of credentials, so copying real ones
somewhere else would both authenticate successfully and spread secrets.
"""

from adversarial_friends import failures
from adversarial_friends.adapters import load_adapters
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.paths import ADAPTER_DIR
from adversarial_friends.spawn import SpawnResult

# Verbatim, trimmed only for length.
CAPTURED = (
    "ERROR: unexpected status 401 Unauthorized: Missing bearer or basic "
    "authentication in header, url: https://api.openai.com/v1/responses, "
    "cf-ray: a3248fd8af5ddfd2-PDX, request id: req_9faeabaae2a54fc88b6fea48e524a918"
)
# Also verbatim from the same run: the retry noise around the real message.
RETRY_NOISE = "ERROR: Reconnecting... 3/5"
# A plain network failure, which must NOT classify as auth.
NETWORK = (
    "ERROR codex_api::endpoint::responses_websocket: failed to connect to "
    "websocket: error trying to connect: dns error"
)


def _outcome(stderr: str, exit_code: int = 1) -> SpawnResult:
    return SpawnResult(
        argv=["codex"],
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_s=1.0,
        timed_out=False,
        result=NormalizeResult(None, ["no output"], False),
        failure_reason=f"exit {exit_code}",
        orphans_suspected=False,
    )


def _codex():
    return load_adapters(ADAPTER_DIR)["codex"]


def test_the_captured_failure_classifies_as_auth():
    assert failures.classify(_outcome(CAPTURED), _codex()) == failures.AUTH


def test_a_network_failure_is_not_an_auth_failure():
    """The distinction agy's markers were written to preserve: matching what
    a CLI prints when it cannot REACH its endpoint would abort every
    network-denied run as an auth failure."""
    assert failures.classify(_outcome(NETWORK), _codex()) == failures.UNKNOWN


def test_retry_noise_alone_is_not_an_auth_failure():
    assert failures.classify(_outcome(RETRY_NOISE), _codex()) == failures.UNKNOWN


def test_exit_one_alone_is_not_an_auth_failure():
    """codex exits 1 for a rejected sandbox write and a refused git-repo
    check too. Declaring that exit code would classify both as auth."""
    assert failures.classify(_outcome("some other failure"), _codex()) == failures.UNKNOWN


def test_a_timeout_is_never_auth_even_carrying_the_marker():
    """§14 gives a timeout precedence: a killed friend's truncated output
    must not enter any interpretation path."""
    outcome = _outcome(CAPTURED)
    outcome.timed_out = True
    assert failures.classify(outcome, _codex()) == failures.UNKNOWN


def test_codex_declares_a_remediation_a_reader_can_act_on():
    assert "codex login" in _codex().auth.remediation

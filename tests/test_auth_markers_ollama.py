"""ollama's auth markers are HTTP statuses, not captured stderr (§14).

A local ollama has no credentials, so there is no auth failure to capture
from it -- listing it beside the CLIs awaiting a real capture meant waiting
for something that could never arrive. Behind an authenticating proxy it
answers 401/403, and those are specified by RFC 9110 rather than chosen by a
vendor, which is why declaring them is not the kind of guess §14 forbids.
"""

from adversarial_friends import failures
from adversarial_friends.adapters import load_adapters
from adversarial_friends.normalize import NormalizeResult
from adversarial_friends.paths import ADAPTER_DIR
from adversarial_friends.spawn import SpawnResult


def _outcome(status: int | None, reason: str = "http error") -> SpawnResult:
    return SpawnResult(
        argv=["POST", "http://127.0.0.1:11434/api/generate", "qwen3:0.6b"],
        exit_code=status,
        stdout="",
        stderr="",
        duration_s=1.0,
        timed_out=False,
        result=NormalizeResult(None, ["no output"], False),
        failure_reason=reason,
        orphans_suspected=False,
    )


def _ollama():
    return load_adapters(ADAPTER_DIR)["ollama"]


def test_401_classifies_as_auth():
    assert failures.classify(_outcome(401), _ollama()) == failures.AUTH


def test_403_classifies_as_auth():
    assert failures.classify(_outcome(403), _ollama()) == failures.AUTH


def test_429_is_not_auth():
    """Rate limiting is not an auth failure, and treating it as one would
    stop dispatching a friend that merely needs to wait."""
    assert failures.classify(_outcome(429), _ollama()) == failures.UNKNOWN


def test_500_is_not_auth():
    assert failures.classify(_outcome(500), _ollama()) == failures.UNKNOWN


def test_a_connection_failure_with_no_status_is_not_auth():
    """No response at all means no status; exit_code is None."""
    assert failures.classify(_outcome(None, "connection refused"), _ollama()) == failures.UNKNOWN


def test_a_successful_run_is_never_classified():
    outcome = _outcome(200)
    outcome.failure_reason = None
    assert failures.classify(outcome, _ollama()) == failures.UNKNOWN


def test_every_shipped_adapter_now_declares_auth_markers():
    """The gap this closes. Each adapter's markers came from a real captured
    failure except ollama's, which are RFC-specified statuses -- see its
    TOML for why that is not a guess."""
    registry = load_adapters(ADAPTER_DIR)
    missing = [name for name, a in registry.items() if not a.auth.declared()]
    assert not missing, f"adapters with no auth markers: {missing}"

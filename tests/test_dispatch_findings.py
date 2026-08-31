"""Two findings from cross-examining dispatch.py: c-0004 and c-0010."""

import threading
import time

import pytest

from adversarial_friends import http_transport, trust
from adversarial_friends.authority import ExternalToolPolicy, enforce
from adversarial_friends.dispatch import _FAKE_CAPABILITY, _UNKNOWN_CAPABILITY
from adversarial_friends.errors import UsageError


def test_denied_values_are_refused_when_they_arrive_via_extra_args():
    """c-0004. `check_denied_values` ran on the argv built from the adapter,
    and `--unsafe-extra-args` were appended AFTER it -- so the screened argv
    was never the executed one.

    `parse_unsafe_extra_args` refuses a DENIED_FLAG on those tokens but never
    looked at denied VALUES, so the flag-name spelling of "turn the sandbox
    off" was blocked while the value spelling of the same capability went
    straight through. The escape hatch is for "I need one more option", which
    is the line its own docstring draws.
    """
    with pytest.raises(UsageError, match="grants write access"):
        trust.check_denied_values(["codex", "exec", "--sandbox", "danger-full-access"])
    with pytest.raises(UsageError, match="grants write access"):
        trust.check_denied_values(["codex", "exec", "--sandbox=workspace-write"])


def test_an_ordinary_extra_arg_is_still_allowed():
    """The hatch must keep working: `codex -c`, `claude --settings` and
    `--add-dir` are exactly what it exists for."""
    trust.check_denied_values(["codex", "exec", "-c", "model=gpt-5", "--add-dir", "/tmp/x"])


def test_the_flag_screen_and_the_value_screen_now_agree():
    """Both spellings of "no guardrails" are refused, whichever screen sees
    them first."""
    with pytest.raises(UsageError):
        trust.parse_unsafe_extra_args("--dangerously-skip-permissions", accepted=True)
    with pytest.raises(UsageError):
        trust.check_denied_values(["claude", "--permission-mode", "bypassPermissions"])


def test_an_http_friend_stops_when_the_run_is_aborted(tmp_path):
    """c-0010. The exec transport has honoured abort_event since signal
    handling was added; this one ignored it, so Ctrl-C on a run with an
    ollama friend sat until the network deadline expired.

    Pointed at a port nothing answers on, with the flag already set: the call
    must come back as `aborted` rather than waiting out the timeout.
    """
    import dataclasses

    from adversarial_friends.adapters import FriendSpec, load_adapters
    from adversarial_friends.paths import ADAPTER_DIR

    # The shipped adapter, pointed at a port nothing answers on, so this
    # exercises the real HTTP transport rather than a stand-in.
    adapter = dataclasses.replace(
        load_adapters(ADAPTER_DIR)["ollama"],
        endpoint="http://127.0.0.1:9/api/generate",
    )
    spec = FriendSpec(
        name="ollama-ops-0",
        cli="ollama",
        lens="ops",
        model="qwen3:0.6b",
        effort=None,
        scope="doc",
        timeout=60,
    )
    prompt = tmp_path / "p.txt"
    prompt.write_text("review this")
    aborted = threading.Event()
    aborted.set()

    started = time.monotonic()
    result = http_transport.run_request(adapter, spec, prompt, 60, abort_event=aborted)
    elapsed = time.monotonic() - started

    assert result.failure_reason == "aborted"
    # The point of the fix: it returns promptly rather than at the deadline.
    assert elapsed < 10, elapsed


def test_a_bare_url_in_stderr_is_not_left_clickable():
    """c-0002/c-0007. The sanitizer stripped `` ` * _ [ ] < > `` and claimed
    to neutralize inline Markdown links -- but GFM autolinks a bare
    `scheme://host` with no delimiters at all, so nothing in that strip could
    reach it. A friend's stderr is attacker-influenced text: the artifact
    under review steers what the CLI prints.
    """
    from adversarial_friends.dispatch import _stderr_tail

    tail = _stderr_tail("auth failed, see https://evil.example/steal for help")
    assert "https://" not in tail
    # Defanged, not deleted: the host is the useful part of an auth error.
    assert "evil.example/steal" in tail


def test_a_www_autolink_is_defanged_too():
    from adversarial_friends.dispatch import _stderr_tail

    assert "www.evil.example" not in _stderr_tail("contact www.evil.example now")


def test_strikethrough_delimiters_are_stripped():
    """`~~` was outside the strip set, so it still rendered."""
    from adversarial_friends.dispatch import _stderr_tail

    assert "~" not in _stderr_tail("token ~~expired~~ actually fine")


def test_an_ordinary_diagnostic_survives_readable():
    from adversarial_friends.dispatch import _stderr_tail

    tail = _stderr_tail("Error: model 'qwen3' not found; run 'ollama pull qwen3'")
    assert "model 'qwen3' not found" in tail
    assert "ollama pull qwen3" in tail


def test_synthetic_capabilities_do_not_claim_enforcement():
    assert _FAKE_CAPABILITY.external_tools == "not-applicable"
    assert _UNKNOWN_CAPABILITY.external_tools == "unknown"


def test_http_capability_comes_from_the_actual_authority_decision(tmp_path):
    from adversarial_friends.adapters import load_adapters
    from adversarial_friends.paths import ADAPTER_DIR

    adapter = load_adapters(ADAPTER_DIR)["ollama"]
    denied = enforce(adapter, ExternalToolPolicy.DENY)
    allowed = enforce(adapter, ExternalToolPolicy.ALLOW)
    assert http_transport.capability_for(adapter, denied).external_tools == "denied"
    assert http_transport.capability_for(adapter, allowed).external_tools == "explicitly-allowed"

"""Tests for the HTTP transport (ollama).

Almost everything here runs against a stub server in-process rather than a
real ollama: CI has no ollama, and a test suite that silently skips its only
coverage of a transport is worse than one that has none. The single test
that does need a live server is marked and skipped explicitly, so its
absence is visible rather than implied.
"""

import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import threading
import time
from typing import ClassVar

import pytest

from adversarial_friends import adapters, http_transport

GOOD_PAYLOAD = {
    "findings": [
        {
            "severity": "high",
            "claim": "the guard is missing",
            "location": "src/auth.py:42",
            "evidence": "src/auth.py:38",
            "failure_scenario": "expired token reaches the handler",
            "suggested_fix": "check exp before dispatch",
        }
    ]
}


class _Stub(BaseHTTPRequestHandler):
    """Serves whatever the test class-attribute says to serve."""

    status = 200
    body = ""
    captured: ClassVar[dict] = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        type(self).captured = json.loads(raw)
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # keep pytest output clean


class _BlockingStub(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()

    def do_POST(self):
        type(self).started.set()
        type(self).release.wait(2)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    """A live HTTP server on a free port, torn down after the test."""
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _adapter(endpoint: str) -> adapters.Adapter:
    return adapters.Adapter(
        name="ollama",
        binary="",
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        transport="http",
        endpoint=endpoint,
    )


def _spec(model="qwen3:0.6b"):
    return adapters.FriendSpec(
        name="ollama-security-0",
        cli="ollama",
        lens="security",
        model=model,
        effort=None,
        scope="doc",
        timeout=10,
    )


def _endpoint(server) -> str:
    return f"http://127.0.0.1:{server.server_port}/api/generate"


def _prompt(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.txt"
    p.write_text("challenge this artifact", encoding="utf-8")
    return p


def test_successful_response_is_unwrapped_and_normalized(stub, tmp_path):
    """ollama wraps the model's text under `response`; claims must come out."""
    _Stub.status = 200
    _Stub.body = json.dumps({"response": json.dumps(GOOD_PAYLOAD), "done": True})
    result = http_transport.run_request(
        _adapter(_endpoint(stub)), _spec(), _prompt(tmp_path), timeout_s=10
    )
    assert result.failure_reason is None
    assert result.exit_code == 200
    assert result.result.succeeded
    assert result.result.payload["findings"][0]["claim"] == "the guard is missing"
    assert result.orphans_suspected is False


def test_request_asks_for_a_non_streaming_reply(stub, tmp_path):
    """A streamed reply arrives as one JSON object per token, which nothing
    downstream could parse without reassembly."""
    _Stub.status = 200
    _Stub.body = json.dumps({"response": json.dumps(GOOD_PAYLOAD)})
    http_transport.run_request(_adapter(_endpoint(stub)), _spec(), _prompt(tmp_path), timeout_s=10)
    assert _Stub.captured["stream"] is False
    assert _Stub.captured["model"] == "qwen3:0.6b"
    assert _Stub.captured["prompt"] == "challenge this artifact"


def test_http_error_is_a_failed_result_not_an_exception(stub, tmp_path):
    """commands.run dispatches friends concurrently; one raising would take
    down the whole run rather than marking a single friend failed."""
    _Stub.status = 500
    _Stub.body = json.dumps({"error": "model not found"})
    result = http_transport.run_request(
        _adapter(_endpoint(stub)), _spec(), _prompt(tmp_path), timeout_s=10
    )
    assert result.failure_reason == "http 500"
    assert "model not found" in result.stderr
    assert result.exit_code == 500
    assert result.result.succeeded is False


def test_unreachable_endpoint_is_a_failed_result(tmp_path):
    # Port 1 is reserved and never listening.
    result = http_transport.run_request(
        _adapter("http://127.0.0.1:1/api/generate"), _spec(), _prompt(tmp_path), timeout_s=5
    )
    assert result.failure_reason.startswith("endpoint unreachable")
    assert result.exit_code is None
    assert result.result.succeeded is False


def test_missing_model_fails_before_any_request(tmp_path):
    """ollama has no default model, and its 400 body explains nothing. The
    failure should name the fix instead of relaying that."""
    result = http_transport.run_request(
        _adapter("http://127.0.0.1:1/api/generate"), _spec(model=None), _prompt(tmp_path), 5
    )
    assert "requires an explicit model" in result.failure_reason
    # Never dispatched: the endpoint above is not listening, so a request
    # would have reported "unreachable" instead.
    assert result.exit_code is None


def test_non_http_scheme_is_refused(tmp_path):
    """Adapter records are repository-controlled data (spec §13's allowlist
    trust model). A file:// endpoint would be a local-read capability the
    roster was never meant to grant."""
    result = http_transport.run_request(
        _adapter("file:///etc/passwd"), _spec(), _prompt(tmp_path), timeout_s=5
    )
    assert "refusing non-http endpoint scheme" in result.failure_reason
    assert result.result.succeeded is False


def test_unexpected_body_shape_falls_back_to_scanning_the_whole_body(stub, tmp_path):
    """Same rule normalize() applies to envelopes: a body that plainly
    contains a valid findings object must not be discarded because its
    wrapper was not the expected shape."""
    _Stub.status = 200
    _Stub.body = json.dumps(GOOD_PAYLOAD)  # no `response` key at all
    result = http_transport.run_request(
        _adapter(_endpoint(stub)), _spec(), _prompt(tmp_path), timeout_s=10
    )
    assert result.result.succeeded
    assert result.result.payload["findings"][0]["claim"] == "the guard is missing"


def test_probe_reports_a_live_server(stub):
    assert http_transport.probe(_endpoint(stub)) is True


def test_probe_reports_a_dead_endpoint():
    assert http_transport.probe("http://127.0.0.1:1/api/generate") is False


def test_probe_rejects_a_non_http_scheme():
    assert http_transport.probe("file:///etc/passwd") is False


def test_capability_never_claims_readonly():
    """A bare model has no filesystem access to constrain, so no readonly
    flag was emitted and nothing was enforced. Claiming True would assert an
    enforcement that does not exist -- exactly the drift capability
    reporting exists to prevent."""
    from adversarial_friends.authority import AuthorityDecision, ExternalToolPolicy

    adapter = _adapter("http://127.0.0.1:11434/api/generate")
    decision = AuthorityDecision(ExternalToolPolicy.DENY, "denied", (), ("request",))
    cap = http_transport.capability_for(adapter, decision)
    assert cap.readonly is False
    assert cap.schema is False


@pytest.mark.skipif(
    not http_transport.probe("http://127.0.0.1:11434/api/generate"),
    reason="no ollama listening on 127.0.0.1:11434",
)
def test_against_a_real_local_ollama(tmp_path):
    """The stub above proves this module's own logic; it cannot prove the
    request shape matches what ollama actually accepts. This one does, and
    is the only test here that talks to a real server.

    Local models only -- never a metered endpoint. qwen3:0.6b is small
    enough to answer quickly and costs nothing to run.
    """
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Reply with the single word: ok", encoding="utf-8")
    result = http_transport.run_request(
        _adapter("http://127.0.0.1:11434/api/generate"),
        _spec(model="qwen3:0.6b"),
        prompt,
        timeout_s=120,
    )
    # Asserting on transport mechanics, not on what the model chose to say:
    # a 0.6b model's wording is not a stable thing to test against.
    assert result.exit_code == 200, result.failure_reason
    assert result.stdout, "ollama returned an empty body"
    assert json.loads(result.stdout)["response"], "no text under ollama's response key"


def test_an_oversized_response_body_is_refused_rather_than_held(stub, tmp_path):
    """c-0002, the HTTP half. `response.read()` had no ceiling, so a
    misbehaving or hostile endpoint could make this process hold an
    arbitrary amount of memory -- while the error path two lines below it
    had capped its body at 500 bytes all along.

    Note the endpoint is operator-configured, so this is not primarily an
    attack surface; it is the same runaway-output failure the subprocess
    path has, and a local model server looping on generation is the
    realistic way to hit it.
    """
    _Stub.status = 200
    _Stub.body = json.dumps({"response": "x" * (256 * 1024)})
    result = http_transport.run_request(
        _adapter(_endpoint(stub)),
        _spec(),
        _prompt(tmp_path),
        30,
        max_response_bytes=64 * 1024,
    )
    assert result.failure_reason is not None
    assert "exceeded" in result.failure_reason
    assert result.result.succeeded is False
    assert len(result.stdout) < 2 * 64 * 1024


def test_a_normal_sized_response_is_unaffected_by_the_ceiling(stub, tmp_path):
    _Stub.status = 200
    _Stub.body = json.dumps({"response": json.dumps(GOOD_PAYLOAD)})
    result = http_transport.run_request(
        _adapter(_endpoint(stub)), _spec(), _prompt(tmp_path), 30, max_response_bytes=64 * 1024
    )
    assert result.failure_reason is None
    assert result.result.succeeded is True


def test_abort_terminates_the_http_worker(tmp_path):
    _BlockingStub.started.clear()
    _BlockingStub.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockingStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    abort = threading.Event()
    timer = threading.Timer(0.1, abort.set)
    timer.start()
    started = time.monotonic()
    try:
        result = http_transport.run_request(
            _adapter(_endpoint(server)),
            _spec(),
            _prompt(tmp_path),
            timeout_s=10,
            abort_event=abort,
        )
    finally:
        _BlockingStub.release.set()
        server.shutdown()
        server.server_close()
        timer.cancel()

    assert result.failure_reason == "aborted"
    assert time.monotonic() - started < 1.0
    assert all(child.name != "afriend-http" for child in multiprocessing.active_children())

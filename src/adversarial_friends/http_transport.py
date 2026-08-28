"""Dispatch a friend over HTTP instead of by executing a CLI.

`ollama` is the reason this exists. It has a CLI, but `ollama run` writes
ANSI control codes into its own payload (see the note atop ollama.toml), so
the HTTP API is the only interface that returns clean text. There is no
process to spawn, so none of spawn.py applies: no process group, no
descendants, no reaping.

The result type is deliberately the same `SpawnResult` the exec transport
returns, so everything downstream -- normalize, the claim pipeline, the
report's friend table, run.json -- stays transport-agnostic. Fields that
have no meaning here are filled honestly rather than plausibly:

* `argv` records the request as `[POST, <endpoint>, <model>]`. It is not a
  command line and is never executed; it exists so run.json and the
  per-friend `.meta` file say what was actually dispatched.
* `exit_code` is the HTTP status on a response, and None when no response
  was obtained at all -- matching the exec transport, where None means the
  process never ran.
* `orphans_suspected` is always False. It is not "no orphans were detected";
  an HTTP request cannot leave a descendant behind, so the question does not
  arise.

Requests use urllib from the stdlib. The project ships no runtime
dependencies, and adding one for a single POST would be a poor trade.
"""

import json
from pathlib import Path
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request

from .adapters import Adapter, Capability, FriendSpec
from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .normalize import NormalizeResult, normalize
from .spawn import MAX_OUTPUT_BYTES, SpawnResult

# ollama returns the model's text under this key for a non-streaming
# /api/generate call. Declared here rather than as an Envelope on the
# adapter because it is a property of this transport's request shape --
# `stream: false` is what makes the reply a single object with this key --
# not of some CLI's output wrapper.
_OLLAMA_RESPONSE_KEY = "response"

# Only these schemes are ever dispatched to. An adapter record is
# repository-controlled data (§13's allowlist trust model): a `file://` or
# `gopher://` endpoint slipping through urllib would be a capability the
# roster was never supposed to grant.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _failure(
    argv: list[str], duration: float, reason: str, status: int | None = None
) -> SpawnResult:
    """A SpawnResult for a request that produced no usable answer.

    Mirrors spawn._early_failure: a friend that fails must come back as a
    result, never as an exception, because commands.run dispatches friends
    concurrently and one raising would take down the whole run.
    """
    return SpawnResult(
        argv=argv,
        exit_code=status,
        stdout="",
        stderr=reason,
        duration_s=duration,
        timed_out=reason == "timeout",
        result=NormalizeResult(None, [reason], False),
        failure_reason=reason,
        orphans_suspected=False,
    )


def probe(endpoint: str, timeout_s: float = 2.0) -> bool:
    """Return whether the endpoint's host answers at all.

    Used by roster discovery in place of the `shutil.which` check the exec
    transport uses: "is this friend available" means a reachable server
    here, not a binary on PATH. Deliberately requests the API root rather
    than the generate path -- a bare GET to /api/generate is a malformed
    request, and a 404 from a *live* server would otherwise read as
    unreachable.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        return False
    root = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        # Scheme is checked against _ALLOWED_SCHEMES above, so this can
        # never be handed a file:// or similar local-capability URL.
        with urllib.request.urlopen(root, timeout=timeout_s):
            return True
    except urllib.error.HTTPError:
        # Any HTTP status is proof something is listening and speaking HTTP,
        # which is the only question being asked here.
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def capability_for(adapter: Adapter) -> Capability:
    """Capability of an HTTP friend, reported honestly.

    `readonly=False` is the interesting one, and it is not an oversight. A
    read-only capability in this project means "the friend was given a flag
    that constrains what it may touch" -- something this runner emitted and
    can point at. A bare model behind /api/generate has no filesystem access
    to constrain, so there is no such flag and nothing was enforced. Saying
    True would claim an enforcement that does not exist, which is exactly
    the drift capability reporting exists to prevent. Containment for these
    friends comes from doc scope (they are handed only the artifact text),
    not from a capability.
    """
    return Capability(schema=False, readonly=False, effort=adapter.effort_kind)


def run_request(
    adapter: Adapter,
    spec: FriendSpec,
    prompt_file: Path,
    timeout_s: int,
    contract: PayloadContract = CLAIM_CONTRACT,
    max_response_bytes: int = MAX_OUTPUT_BYTES,
) -> SpawnResult:
    """POST the prompt to the adapter's endpoint and normalize the reply.

    `contract` selects which payload kind the reply is read as, exactly as it
    does for the exec transport -- an HTTP friend judges in a crossexam round
    like any other.

    `max_response_bytes` bounds the reply the same way spawn bounds a
    subprocess's stdout, and for the same reason: the timeout limits how
    long a request may take, not how much memory its answer may cost. A
    local model server looping on generation is the realistic way to reach
    it. The HTTPError path below has capped its body at 500 bytes all
    along -- it was the success path, the one that reads an unbounded body,
    that had no ceiling."""
    endpoint = adapter.endpoint
    model = spec.model or ""
    argv = ["POST", endpoint, model or "<default-model>"]
    started = time.monotonic()

    parsed = urlparse(endpoint)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return _failure(
            argv,
            time.monotonic() - started,
            f"refusing non-http endpoint scheme {parsed.scheme!r} for cli {adapter.name!r}",
        )
    if not model:
        # ollama has no default model; omitting it yields a 400 whose body
        # explains nothing useful. Fail before the call with the fix.
        return _failure(
            argv,
            time.monotonic() - started,
            f"cli {adapter.name!r} requires an explicit model "
            f"(no default exists); pass one in the roster",
        )

    prompt = prompt_file.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            # A streamed reply arrives as one JSON object per token, which
            # would have to be reassembled before anything could parse it.
            "stream": False,
        }
    ).encode("utf-8")

    # Scheme checked against _ALLOWED_SCHEMES above.
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = response.status
            # One byte past the ceiling is enough to know it was exceeded,
            # and never reads the rest of an arbitrarily large body.
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                return _failure(
                    argv,
                    time.monotonic() - started,
                    f"response exceeded {max_response_bytes} bytes",
                    status,
                )
            body = raw.decode("utf-8", errors="replace")
    except TimeoutError:
        return _failure(argv, time.monotonic() - started, "timeout")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        return _failure(
            argv, time.monotonic() - started, f"http {exc.code}: {body.strip()}", status=exc.code
        )
    except (urllib.error.URLError, OSError) as exc:
        return _failure(
            argv,
            time.monotonic() - started,
            f"endpoint unreachable: {endpoint} ({exc})",
        )

    duration = time.monotonic() - started

    # Pull the model's text out of ollama's own wrapper. A reply that is not
    # the shape this transport asked for falls through to normalizing the
    # whole body, for the same reason normalize() retries a raw scan when an
    # envelope yields nothing: a body that happens to contain a valid
    # findings object must not be discarded because its wrapper was
    # unexpected.
    text = body
    try:
        parsed_body = json.loads(body)
        if isinstance(parsed_body, dict) and isinstance(parsed_body.get(_OLLAMA_RESPONSE_KEY), str):
            text = parsed_body[_OLLAMA_RESPONSE_KEY]
    except json.JSONDecodeError:
        pass

    result = normalize(
        text,
        envelope=adapter.envelope,
        structured_output=adapter.structured_output,
        contract=contract,
    )
    return SpawnResult(
        argv=argv,
        exit_code=status,
        stdout=body,
        stderr="",
        duration_s=duration,
        timed_out=False,
        result=result,
        failure_reason=None if result.succeeded else "unusable output",
        orphans_suspected=False,
    )

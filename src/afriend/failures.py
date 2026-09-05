"""Classifying why a friend failed, and noticing when it will keep failing.

Spec §14's table gives auth its own row because auth is **deterministic**: a
friend that cannot authenticate now cannot authenticate on the next round
either. §7.2 therefore aborts the run rather than "burning the remaining
rounds and iterations" -- with `--mode loop` at its defaults that is up to
five iterations of three rounds each, every one of them a dispatch that
cannot possibly succeed.

Two mechanisms here, and the difference between them matters:

**Classification (§14) is by adapter-declared marker, never by inference.**
The spec's reason is concrete: gemini emits extension-loader errors, a
true-color warning and a ripgrep notice to stderr on *every* invocation, so
matching "auth" or "unauthorized" against stderr has a real false-positive
rate. A false auth classification is expensive -- it aborts the whole run.

The spec went one step further and forbade stderr entirely, requiring the
marker in the CLI's structured output. That was relaxed (recorded in the
spec's divergences) when the first real capture arrived and lived nowhere
else: agy on a lapsed login exits 1 -- a status it shares with unrelated
errors -- prints nothing to stdout, and says on stderr exactly

    Error: authentication required. Run 'agy' to log in, then retry.

So a stderr marker is allowed, on the condition that keeps the spec's
reasoning intact: it must be that CLI's own sentence, captured verbatim
from a real failure, not a word the CLI might plausibly use. Anything
unrecognised is still `UNKNOWN` rather than guessed at.

**Repeat detection needs no markers at all.** A friend that failed the same
way on two consecutive rounds is not going to succeed on the third, whatever
the cause -- a missing binary, a revoked token, a model name the provider
retired. That rule covers auth without needing to recognise it, and covers
failures nobody has captured a marker for. It disables the friend for the
rest of the run instead of aborting it, because unlike auth it is inferred
rather than declared, and inference should cost less when it is wrong.

agy is the only adapter with a populated marker, captured from a real
failure during a crossexam whose other two friends broke for unrelated
reasons. The others stay empty until someone captures theirs; inventing one
would be exactly the guessing the spec rejects.

One trap, found while probing whether the CLIs honor HTTPS_PROXY: with the
network unreachable, agy prints

    Error: authentication timed out.
    Error: authentication failed or timed out

That reads like the missing marker and must NOT be adopted as one. It is
what agy says when it cannot REACH the auth endpoint, so a marker matching
it would classify every network-denied run as an auth failure -- aborting
the whole run, since auth is treated as declared rather than inferred. A
usable marker has to come from a run with working network and bad
credentials, which is the capture nobody has made yet.
"""

from .adapters import Adapter, AuthMarkers
from .spawn import SpawnResult

AUTH = "auth"
UNKNOWN = "unknown"

# How many consecutive identical failures before a friend is considered
# deterministically broken. Two, not three: the second identical failure is
# already evidence, and a third costs another full fan-out to learn nothing.
REPEAT_LIMIT = 2


def _dotted(payload: object, path: str) -> object:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def classify(outcome: SpawnResult, adapter: Adapter | None) -> str:
    """`AUTH` or `UNKNOWN` -- never a guess.

    A timeout is deliberately never classified as auth even if a marker
    somehow matched: §14 gives timeout precedence over output inspection,
    because a killed friend's truncated output must not enter any
    interpretation path.
    """
    if outcome.failure_reason is None or outcome.timed_out:
        return UNKNOWN
    markers = adapter.auth if adapter else AuthMarkers()
    if not markers.declared():
        return UNKNOWN
    if outcome.exit_code is not None and outcome.exit_code in markers.exit_codes:
        return AUTH
    payload = outcome.result.payload
    for path, expected in markers.paths:
        if _dotted(payload, path) == expected:
            return AUTH
    for needle in markers.stderr:
        if needle in outcome.stderr:
            return AUTH
    return UNKNOWN


def failure_signature(outcome: SpawnResult) -> str | None:
    """A comparable shape for "this friend failed the same way again".

    Deliberately the reason and exit status, not stderr: a CLI that prints a
    timestamp or a request id would otherwise look like a different failure
    every round and never trip the repeat rule.
    """
    if outcome.failure_reason is None:
        return None
    return f"{outcome.exit_code}:{outcome.failure_reason}"


class RepeatTracker:
    """Remembers how a friend has been failing, across rounds.

    A friend is disabled after REPEAT_LIMIT identical consecutive failures.
    A success, or a different failure, resets it -- a friend that failed
    once transiently and then worked has told us the failure was transient,
    which is exactly the case that must not be disabled.
    """

    def __init__(self, limit: int = REPEAT_LIMIT) -> None:
        self.limit = limit
        self._last: dict[str, str] = {}
        self._count: dict[str, int] = {}
        self.disabled: dict[str, str] = {}

    def snapshot(self) -> dict[str, dict[str, str] | dict[str, int]]:
        """This tracker's state, JSON-safe, for `run.json`.

        A tracker lives only in the process that built it, and `--resume`
        is a new process: without persisting this, a friend disabled for
        repeated failure in iteration 1 was silently un-disabled the
        instant that process exited for an orchestrator halt, and iteration
        2's resume re-dispatched and could re-announce it as disabled --
        wasting a call and, worse, letting a broken friend's noise back
        into quorum and `--require-friends` counts that assumed it stayed
        excluded.
        """
        return {
            "last": dict(self._last),
            "count": dict(self._count),
            "disabled": dict(self.disabled),
        }

    @classmethod
    def restore(cls, data: dict[str, object], limit: int = REPEAT_LIMIT) -> "RepeatTracker":
        """The inverse of `snapshot`. `data` is typically absent (a fresh
        run, or a halt written by a version that predates this) -- an empty
        dict restores a tracker identical to a fresh `RepeatTracker()`."""
        tracker = cls(limit=limit)
        last = data.get("last")
        count = data.get("count")
        disabled = data.get("disabled")
        if isinstance(last, dict):
            tracker._last = {str(k): str(v) for k, v in last.items()}
        if isinstance(count, dict):
            tracker._count = {str(k): int(v) for k, v in count.items()}
        if isinstance(disabled, dict):
            tracker.disabled = {str(k): str(v) for k, v in disabled.items()}
        return tracker

    def record(self, friend: str, outcome: SpawnResult) -> None:
        signature = failure_signature(outcome)
        if signature is None or signature != self._last.get(friend):
            self._count[friend] = 1 if signature else 0
            self._last[friend] = signature or ""
            if signature is None:
                self.disabled.pop(friend, None)
            return
        self._count[friend] = self._count.get(friend, 0) + 1
        if self._count[friend] >= self.limit:
            self.disabled[friend] = outcome.failure_reason or "repeated failure"

    def is_disabled(self, friend: str) -> bool:
        return friend in self.disabled

    def note(self, friend: str) -> str:
        return (
            f"{friend} failed identically {self._count.get(friend, 0)} rounds "
            f"running ({self.disabled[friend]}); it will not be dispatched again "
            "this run. A friend that fails the same way twice is broken, not "
            "unlucky, and re-running it costs a full dispatch to learn nothing."
        )


def auth_abort_message(friend: str, adapter: Adapter | None) -> str:
    """§14: remediation is a message, not a command.

    gemini's is a product migration behind a URL, not `gemini login` -- so
    the field carries whatever prose that CLI's adapter declared, and says
    nothing at all when it declared none.
    """
    remediation = adapter.auth.remediation if adapter else ""
    base = (
        f"{friend} could not authenticate. This is a deterministic failure: "
        "every remaining round and iteration would fail the same way, so the "
        "run is stopping now rather than spending them."
    )
    return f"{base} {remediation}".strip()

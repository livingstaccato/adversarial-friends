"""Live progress for a run, written to stderr.

No spec section asks for this. It exists because a crossexam is silent for
tens of minutes and a silent run is indistinguishable from a hung one.
Measured on this project's own source: one codex friend took 357 seconds in
a single round, against a default `--timeout` of 900 -- and three friends
across three rounds is the normal case, not the tail. The only thing the
runner printed in all that time was the run directory, after it was over.

**stderr, never stdout.** `afriend run` prints the run directory on stdout
and nothing else, so a caller can do `cd "$(afriend run spec.md)"`. Progress
sharing that stream would corrupt the one machine-readable thing the command
produces. Every write here goes to the stream this reporter was handed,
which defaults to stderr and is a parameter only so tests can read it back.

**The heartbeat is the part that matters.** A friend that has said nothing
for six minutes looks exactly like a friend that died, and the lifecycle
lines alone cannot tell them apart: they are emitted when something happens,
and the whole problem is that nothing is happening. So a background thread
names what is still outstanding and how long it has been outstanding, which
is the difference between "slow" and "stuck".
"""

from dataclasses import dataclass, field
import sys
import threading
import time
from typing import TextIO

from .events import EventRecord, EventWriter

# How often the heartbeat names what is still in flight. Thirty seconds is
# chosen against the thing being waited on: friends take minutes, so a
# shorter interval is noise that scrolls the lifecycle lines away, and a
# longer one leaves a stall unreported for longer than a person will wait
# before reaching for Ctrl-C.
DEFAULT_HEARTBEAT_S = 30.0

# How often the heartbeat thread wakes to check whether it should stop. It
# is deliberately much shorter than the interval: a round that ends two
# seconds after a heartbeat must not keep a thread alive for the remaining
# twenty-eight, or the process lingers after its work is done.
_TICK_S = 0.5
# A roster lens is intentionally free text. Lifecycle events are not a
# second place to persist it: an arbitrary lens could contain a credential
# or other user content, so every dispatched friend receives this fixed,
# descriptive label instead.
_LIFECYCLE_LENS = "configured"


def format_duration(seconds: float) -> str:
    """`357.1` -> `5m57s`. Minutes because that is the scale friends run at;
    reporting 357s makes a reader do the division that matters."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


@dataclass
class _InFlight:
    started: float
    timeout_s: int
    provider: str
    lens: str
    round_no: int


@dataclass
class Progress:
    """Threadsafe stderr reporter for one run.

    Every method is a no-op when `enabled` is False, so callers never guard
    a call site with an `if`. That is not only tidier -- a guarded reporter
    grows conditionals in the dispatch path, which is the one place in this
    runner where a stray exception costs a whole round's work.
    """

    stream: TextIO = field(default_factory=lambda: sys.stderr)
    enabled: bool = True
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _in_flight: dict[str, _InFlight] = field(default_factory=dict, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _beat: threading.Thread | None = field(default=None, repr=False)
    event_writer: EventWriter | None = field(default=None, repr=False)
    _terminal_event_written: bool = field(default=False, repr=False)

    # --- writing ----------------------------------------------------------

    def _emit(self, line: str) -> None:
        """One line, flushed.

        Flushed because stderr is block-buffered when it is a pipe rather
        than a terminal -- which is precisely the case that motivated this
        module, an agent reading the runner's output through a pipe. Without
        the flush the whole point is defeated exactly where it is needed.

        Never raises. A closed or broken stream is a reason to stop
        reporting progress, not a reason to lose a round of review that has
        already cost several minutes of somebody's CLI quota.
        """
        if not self.enabled:
            return
        try:
            with self._lock:
                self.stream.write(line + "\n")
                self.stream.flush()
        except (OSError, ValueError):
            self.enabled = False

    def note(self, text: str) -> None:
        self._emit(f"afriend: {text}")

    def _event(self, event_type: str, payload: dict[str, object]) -> None:
        """Best-effort telemetry that cannot change review execution."""
        writer = self.event_writer
        if writer is None:
            return
        try:
            writer.append(EventRecord.create(event_type, payload, run_id=writer.run_id))
        except Exception:
            # Events are observational. A full disk, a malformed runtime
            # projection, or a host-provided writer failure must not turn a
            # successful review into a failed one. Disable this writer after
            # its first failure rather than repeatedly perturbing dispatch.
            with self._lock:
                if self.event_writer is writer:
                    self.event_writer = None

    def run_started(self, mode: str, profile: str) -> None:
        self._event("run_started", {"mode": mode, "profile": profile, "status": "started"})

    def run_finished(self, status: str, next_action: str, *, duration_s: float) -> None:
        """Persist the one terminal record for this invocation."""
        with self._lock:
            if self._terminal_event_written:
                return
            self._terminal_event_written = True
        self._event(
            "run_finished",
            {"status": status, "next_action": next_action, "duration_s": duration_s},
        )
        self._emit(f"afriend: run {status} -- next action: {next_action}")

    # --- round lifecycle --------------------------------------------------

    def round_started(self, round_no: int, kind: str, names: list[str]) -> None:
        friends = ", ".join(names)
        self._emit(f"afriend: round {round_no} ({kind}): {len(names)} friends -- {friends}")
        self._start_heartbeat()

    def round_finished(self, round_no: int, summary: str, *, status: str = "completed") -> None:
        self._stop_heartbeat()
        self._emit(f"afriend: round {round_no} done -- {summary}")
        self._event("round_finished", {"round": round_no, "status": status})

    # --- friend lifecycle -------------------------------------------------

    def friend_dispatched(
        self,
        name: str,
        timeout_s: int,
        *,
        provider: str = "unknown",
        lens: str = "unknown",
        round_no: int = 1,
    ) -> None:
        del lens
        with self._lock:
            self._in_flight[name] = _InFlight(
                started=time.monotonic(),
                timeout_s=timeout_s,
                provider=provider,
                lens=_LIFECYCLE_LENS,
                round_no=round_no,
            )

    def friend_finished(self, name: str, outcome: str, *, succeeded: bool = True) -> None:
        with self._lock:
            record = self._in_flight.pop(name, None)
        elapsed_s = time.monotonic() - record.started if record else 0.0
        elapsed = format_duration(elapsed_s) if record else "?"
        self._emit(f"afriend:   {name} {outcome} in {elapsed}")
        self._event(
            "friend_finished" if succeeded else "friend_failed",
            {
                "friend": name,
                "provider": record.provider if record else "unknown",
                "lens": record.lens if record else "unknown",
                "round": record.round_no if record else 1,
                "duration_s": elapsed_s,
                "status": "succeeded" if succeeded else "failed",
            },
        )

    def friend_forgotten(self, name: str) -> None:
        """Drop a friend without reporting an outcome.

        For the case where the round is being torn down around it -- a
        deliberate AfError stop. Leaving it in the in-flight set would have
        the heartbeat keep naming a friend nobody is waiting for any more,
        which is worse than saying nothing: it describes a stall that is not
        happening while the real error scrolls past.
        """
        with self._lock:
            self._in_flight.pop(name, None)

    # --- heartbeat --------------------------------------------------------

    def _start_heartbeat(self) -> None:
        if not self.enabled or self._beat is not None:
            return
        self._stop.clear()
        # A daemon thread because it must never be the reason the process
        # stays alive. Its only job is describing work someone else is
        # doing; if that work is gone, so is the reason to report on it.
        self._beat = threading.Thread(target=self._heartbeat_loop, daemon=True, name="af-progress")
        self._beat.start()

    def _stop_heartbeat(self) -> None:
        self._stop.set()
        beat, self._beat = self._beat, None
        if beat is not None:
            beat.join(timeout=_TICK_S * 4)

    def _heartbeat_loop(self) -> None:
        # The tick is a responsiveness floor, not a second interval: it
        # bounds how long the thread lingers after a round ends. Capped at
        # the interval so a reporter asked for a shorter one actually gets
        # it -- otherwise any interval below the tick silently becomes the
        # tick, which is the kind of quietly-ignored setting that only
        # surfaces as a test that mysteriously has to sleep.
        tick = min(_TICK_S, self.heartbeat_s)
        last = time.monotonic()
        while not self._stop.wait(tick):
            now = time.monotonic()
            if now - last < self.heartbeat_s:
                continue
            last = now
            for line in self._waiting_lines(now):
                self._emit(line)

    def _waiting_lines(self, now: float) -> list[str]:
        """One line per outstanding friend, longest wait first.

        Named individually rather than counted. "2 friends still running"
        does not tell you which one to look at, and with a round-robin lens
        assignment the name is also the lens -- so the line says what is
        slow and what it was asked to do at the same time.
        """
        with self._lock:
            outstanding = sorted(self._in_flight.items(), key=lambda kv: kv[1].started)
        return [
            f"afriend:   still waiting: {name} "
            f"({format_duration(now - rec.started)} elapsed, "
            f"{format_duration(rec.timeout_s)} timeout)"
            for name, rec in outstanding
        ]

    # --- teardown ---------------------------------------------------------

    def close(self) -> None:
        """Stop the heartbeat. Safe to call more than once, and safe to call
        on a reporter whose round never started."""
        self._stop_heartbeat()


def disabled() -> Progress:
    """A reporter that says nothing.

    Returned rather than `None` so every call site stays unconditional --
    see the class docstring.
    """
    return Progress(enabled=False)

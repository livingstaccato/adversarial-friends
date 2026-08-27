"""Run in a subprocess by test_abort_reentry: the interleaving that hung a
real run for two days.

A second signal that is pending while the abort handler's first invocation
holds `abort_event`'s lock runs the handler again, nested, at the next
eval-breaker point. `Event.set` guards a plain `threading.Lock`, so the
nested `set()` blocks on a lock its own caller holds -- forever, since the
caller is further down the same thread's stack. Reproduced against the real
handler with three back-to-back SIGTERMs (which is fewer than coreutils
`timeout` sends). This probe makes the interleaving deterministic by raising
the second signal from inside `set()` itself, while the lock is held.
"""

import concurrent.futures
import os
import signal
import threading

from adversarial_friends.commands.environment import install_abort_handlers


class _SignalWhileLocked(threading.Event):
    """`Event.set` verbatim, with the second SIGTERM raised while the
    condition's lock is held. `notify_all` is a Python call, so the handler
    runs there, nested, before this frame releases anything."""

    def set(self) -> None:
        with self._cond:
            self._flag = True
            os.kill(os.getpid(), signal.SIGTERM)
            self._cond.notify_all()


abort_event = _SignalWhileLocked()
abort_signum: dict[str, int | None] = {"value": None}
active_pool: list[concurrent.futures.ThreadPoolExecutor | None] = [None]
install_abort_handlers(abort_event, abort_signum, active_pool)
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    active_pool[0] = pool
    os.kill(os.getpid(), signal.SIGTERM)
    for _ in range(1000):
        pass  # eval-breaker points for the first invocation to run at
print("handled", abort_signum["value"], abort_event.is_set(), flush=True)

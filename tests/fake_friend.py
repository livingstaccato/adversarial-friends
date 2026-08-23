"""A scripted stand-in for a real agent CLI. Never makes a model call."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

MODES = {
    "good": lambda: print(
        json.dumps(
            {
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
        )
    ),
    "empty": lambda: print(json.dumps({"findings": []})),
    "no_findings": lambda: print(json.dumps({"no_findings": True})),
    "offtopic": lambda: print("It looks like you just typed `--mode`."),
    "prose_wrapped": lambda: print(
        "Sure! " + json.dumps({"no_findings": True}) + " Hope that helps!"
    ),
    # Not a scripted verdict -- reports this process's own cwd as the
    # "evidence" field, so a caller can directly confirm what directory it
    # was actually run in (e.g. a private isolation worktree/doc dir, not
    # the af process's own working directory). Added for Task 12's
    # end-to-end isolation-wiring tests.
    "cwd_probe": lambda: print(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "low",
                        "claim": "cwd probe",
                        "location": None,
                        "evidence": str(Path.cwd()),
                        "failure_scenario": "n/a",
                        "suggested_fix": "n/a",
                    }
                ]
            }
        )
    ),
}


def _await_pidfile(pidfile: str, timeout: float = 30.0) -> None:
    """Block until a spawned descendant has recorded its pid, or `timeout`.

    Every attack mode below spawns a descendant that must still be alive,
    and still findable by pid, once this process is done. The descendant
    records its pid by writing `pidfile` -- but that costs a full Python
    interpreter startup, which under a loaded test suite can easily take
    longer than this process needs to print its payload and exit.

    Without this wait the modes race: the runner can correctly reap the
    whole process group before the descendant ever writes the file, so the
    pidfile never appears and the test fails with FileNotFoundError while
    reporting nothing about the behaviour it meant to check. Observed at
    roughly a 40% failure rate for `exit0_leaves_descendant` under full-suite
    load, and never in isolation.

    Waiting here does not weaken any of those tests: this process still
    never `wait()`s on the descendant, and the descendant is still running,
    still inside this process group, at the moment this process exits --
    which is the condition actually under test.
    """
    deadline = time.monotonic() + timeout
    target = Path(pidfile)
    while time.monotonic() < deadline:
        if target.exists():
            return
        time.sleep(0.01)


def _descendant(argv: list) -> None:
    """Internal helper used by the multi-level attack modes below.

    Writes our own pid to argv[0], and — if a second argument is present —
    spawns one more level of ourselves (recursing through this same
    function) before hanging. This lets `grandchild` mode build a small
    process tree (friend -> child -> grandchild) without any extra helper
    files: each level is just this script invoked with mode `_descendant`.
    """
    pidfile = argv[0]
    Path(pidfile).write_text(str(os.getpid()))
    rest = argv[1:]
    if rest:
        subprocess.Popen([sys.executable, __file__, "_descendant", *rest])
    time.sleep(600)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "good"

    if mode == "hang":
        # Spawn a child, then hang: the runner must reap the whole group.
        # If a pidfile path was passed (argv[2]), write the child's pid
        # there instead of relying on stdout captured after a group kill,
        # which is not guaranteed to be readable/flushed by that point.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        if len(sys.argv) > 2:
            Path(sys.argv[2]).write_text(str(child.pid))
        else:
            print(f"child_pid={child.pid}", flush=True)
        time.sleep(600)
        return 0

    if mode == "crash":
        print("boom", file=sys.stderr)
        return 1

    if mode == "hostile_stderr":
        # Whole-branch re-review, Regression 3: a friend's own stderr is
        # untrusted text that now reaches report.md's friend table (see
        # cli._stderr_tail). This mode's stderr carries inline
        # Markdown/HTML constructs an unauthenticated-CLI's real error
        # message could plausibly contain (a bracketed value, an angle-
        # bracketed placeholder, asterisks) to prove they render as inert
        # text, not real emphasis/links/raw HTML.
        print(
            "auth failed: **please** [login](http://evil.example) "
            "`token` <script>alert(1)</script>",
            file=sys.stderr,
        )
        return 1

    if mode == "_descendant":
        _descendant(sys.argv[2:])
        return 0

    if mode == "grandchild":
        # Attack: a friend whose child spawns its own child (a grandchild
        # relative to the runner). None of these levels call setsid, so
        # they all remain in the same process group as the friend itself
        # and must all be reachable by killpg.
        pidfile_child, pidfile_grandchild = sys.argv[2], sys.argv[3]
        subprocess.Popen(
            [sys.executable, __file__, "_descendant", pidfile_child, pidfile_grandchild]
        )
        # Two interpreter startups deep before the grandchild records its
        # pid -- the widest window of any mode here. See _await_pidfile.
        _await_pidfile(pidfile_child)
        _await_pidfile(pidfile_grandchild)
        time.sleep(600)
        return 0

    if mode == "escape":
        # Attack: a descendant that double-forks itself out of the process
        # group by calling os.setsid() before hanging. This creates a new
        # session and process group, so killpg on the friend's original
        # group can never reach it -- a genuine, irreducible escape without
        # OS-level containment (cgroups / job objects / pid namespaces).
        pidfile = sys.argv[2]
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, sys, time\n"
                "os.setsid()\n"
                "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "time.sleep(600)\n",
                pidfile,
            ]
        )
        # Same race as exit0_leaves_descendant, with more slack (this mode
        # hangs rather than exiting) but not immunity -- see _await_pidfile.
        _await_pidfile(pidfile)
        time.sleep(600)
        return 0

    if mode == "leaky_escape":
        # Attack (Task 12 review, Finding 4): like "escape", a descendant
        # calls os.setsid() before hanging -- but THIS process does not
        # hang itself; it prints a valid payload and exits immediately,
        # exactly like "exit0_leaves_descendant" above. Needs no pidfile
        # argument (cli.py's `fake:<mode>` dispatch has no room to pass
        # one -- see cli._specs_from_flags): the escapee is found instead
        # by grepping `ps` output for the literal marker comment below,
        # which -- like every `python -c "..."` invocation in this file --
        # appears verbatim in its command line.
        #
        # Reaping this process's OWN process group cannot reach the
        # escapee (same irreducible limitation as "escape" -- see
        # test_setsid_escapee_is_not_reaped in test_spawn.py), but this
        # process's inherited stdout/stderr pipes stay open as long as the
        # escapee (which never redirects them) is still running -- that is
        # spawn.run_process's SECOND, independent orphans_suspected
        # signal (see its module docstring): the output-pump threads are
        # still alive, well past the drain window, even though this
        # process itself already exited cleanly and fast.
        #
        # The escapee signals *after* os.setsid() returns, and this process
        # waits for that signal before exiting. Without the wait the mode
        # races: this process exits, the runner sweeps its group, and the
        # SIGTERM lands while the escapee is still starting up and still a
        # member of that group -- so it gets killed instead of escaping,
        # the inherited pipes close, and orphans_suspected comes back False.
        # Wide enough on a fast machine to almost never lose; observed
        # losing consistently under an emulated Linux container, where
        # interpreter startup is several seconds.
        escaped_marker = Path(tempfile.gettempdir()) / f"af-leaky-escaped-{os.getpid()}"
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, sys, time\n"
                "os.setsid()\n"
                "open(sys.argv[1], 'w').write('escaped')\n"
                "# af-leaky-escape-marker\n"
                "time.sleep(600)\n",
                str(escaped_marker),
            ]
        )
        _await_pidfile(str(escaped_marker))
        # Purely a handshake; the escapee is found by its `ps` marker, not
        # by this file. Removing it keeps the mode from littering /tmp.
        escaped_marker.unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "findings": [
                        {
                            "severity": "low",
                            "claim": "leaky escape probe",
                            "location": None,
                            "evidence": "spawned a setsid escapee, then exited",
                            "failure_scenario": "n/a",
                            "suggested_fix": "n/a",
                        }
                    ]
                }
            )
        )
        return 0

    if mode == "ignore_sigterm":
        # Attack: the friend itself ignores SIGTERM. SIGKILL cannot be
        # blocked or ignored, so escalation must still finish it off.
        pidfile = sys.argv[2]
        Path(pidfile).write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(600)
        return 0

    if mode == "close_stdout_then_hang":
        # Attack: the friend closes stdout early, then keeps running. The
        # runner must still detect the timeout (it waits for the process to
        # exit, not merely for EOF on the pipes) and must not hang itself
        # trying to drain output.
        sys.stdout.flush()
        sys.stdout.close()
        time.sleep(600)
        return 0

    if mode == "exit0_leaves_descendant":
        # Attack: the friend prints a valid, successful payload and exits 0
        # immediately, without waiting on a child it spawned. The child
        # stays alive in the same process group after the round is already
        # marked complete.
        pidfile = sys.argv[2]
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, sys, time\n"
                "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "time.sleep(600)\n",
                pidfile,
            ]
        )
        # See _await_pidfile: exiting before the descendant records its pid
        # makes this mode race the runner's own (correct) group sweep.
        _await_pidfile(pidfile)
        print(json.dumps({"no_findings": True}))
        return 0

    MODES.get(mode, MODES["good"])()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())

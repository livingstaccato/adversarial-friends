"""A scripted stand-in for a real agent CLI. Never makes a model call."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MODES = {
    "good": lambda: print(json.dumps({"findings": [{
        "severity": "high", "claim": "the guard is missing",
        "location": "src/auth.py:42", "evidence": "src/auth.py:38",
        "failure_scenario": "expired token reaches the handler",
        "suggested_fix": "check exp before dispatch"}]})),
    "empty": lambda: print(json.dumps({"findings": []})),
    "no_findings": lambda: print(json.dumps({"no_findings": True})),
    "offtopic": lambda: print("It looks like you just typed `--mode`."),
    "prose_wrapped": lambda: print(
        "Sure! " + json.dumps({"no_findings": True}) + " Hope that helps!"),
    # Not a scripted verdict -- reports this process's own cwd as the
    # "evidence" field, so a caller can directly confirm what directory it
    # was actually run in (e.g. a private isolation worktree/doc dir, not
    # the af process's own working directory). Added for Task 12's
    # end-to-end isolation-wiring tests.
    "cwd_probe": lambda: print(json.dumps({"findings": [{
        "severity": "low", "claim": "cwd probe",
        "location": None, "evidence": str(Path.cwd()),
        "failure_scenario": "n/a", "suggested_fix": "n/a"}]})),
}


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
        time.sleep(600)
        return 0

    if mode == "escape":
        # Attack: a descendant that double-forks itself out of the process
        # group by calling os.setsid() before hanging. This creates a new
        # session and process group, so killpg on the friend's original
        # group can never reach it -- a genuine, irreducible escape without
        # OS-level containment (cgroups / job objects / pid namespaces).
        pidfile = sys.argv[2]
        subprocess.Popen([
            sys.executable, "-c",
            "import os, sys, time\n"
            "os.setsid()\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n",
            pidfile,
        ])
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
        subprocess.Popen([
            sys.executable, "-c",
            "import os, time\n"
            "os.setsid()\n"
            "# af-leaky-escape-marker\n"
            "time.sleep(600)\n",
        ])
        print(json.dumps({"findings": [{
            "severity": "low", "claim": "leaky escape probe",
            "location": None, "evidence": "spawned a setsid escapee, then exited",
            "failure_scenario": "n/a", "suggested_fix": "n/a"}]}))
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
        subprocess.Popen([
            sys.executable, "-c",
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n",
            pidfile,
        ])
        print(json.dumps({"no_findings": True}))
        return 0

    MODES.get(mode, MODES["good"])()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())

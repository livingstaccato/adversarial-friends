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


# The runner passes a fake friend its prompt file as `--prompt=<path>`
# (see dispatch._dispatch). Every OTHER argument stays positional and keeps
# the meaning it already had: several modes take pidfile paths when a test
# invokes this script directly rather than through `--friend fake:<mode>`.
# Splitting the two here means neither can be mistaken for the other.
POSITIONALS = [a for a in sys.argv[1:] if not a.startswith("--prompt=")]
PROMPT_FILE = next((a.partition("=")[2] for a in sys.argv[1:] if a.startswith("--prompt=")), None)


def _claims_in_prompt() -> list:
    """The blind slice the runner put in this judge's prompt.

    A judging round's fake has to answer the real claim ids the runner
    generated, which it cannot know in advance -- so it reads them back out
    of its own prompt file, exactly as a real friend would have to. The
    slice is the JSON array following judgeprompt.SLICE_PREAMBLE.
    """
    if PROMPT_FILE is None:
        return []
    text = Path(PROMPT_FILE).read_text(encoding="utf-8")
    _head, sep, tail = text.partition("--- CLAIMS UNDER REVIEW ---")
    if not sep:
        return []
    start = tail.find("[")
    if start < 0:
        return []
    return json.loads(tail[start:].strip())


def _identity() -> str:
    """This friend's own name, taken from its prompt file (`<name>.prompt`).

    Two friends running the SAME mode would otherwise emit byte-identical
    claims, which exact_merge collapses into one claim carrying both
    origins -- leaving it with no independent judge at all (§7.1) and making
    a crossexam test look broken for a reason that has nothing to do with
    crossexam. Real friends differ because the friends differ; deriving the
    identity from the prompt path reproduces that.
    """
    if PROMPT_FILE is None:
        return "anonymous"
    return Path(PROMPT_FILE).stem


def _own_finding() -> None:
    """A round-1 critique unique to this friend -- see _identity."""
    who = _identity()
    print(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "high",
                        "claim": f"finding raised by {who}",
                        "location": f"src/{who}.py:1",
                        "evidence": f"src/{who}.py:2",
                        "failure_scenario": "scripted",
                        "suggested_fix": "scripted",
                    }
                ]
            }
        )
    )


def _judge(verdict: str, assessment: str = "confirmed", **extra) -> None:
    out = []
    for claim in _claims_in_prompt():
        entry = {
            "claim_id": claim["id"],
            "verdict": verdict,
            "confidence": "high",
            "evidence_assessment": assessment,
            "reasoning": f"scripted {verdict} for {claim['id']}",
            "counter_evidence": None,
            "amended_claim": None,
        }
        entry.update(extra)
        out.append(entry)
    print(json.dumps({"verdicts": out}))


# What each judging mode returns once it is handed someone else's claims.
# Every one of these also has to survive round 1, where it is asked for a
# critique instead -- see main().
_JUDGEMENTS = {
    "judge_uphold": lambda: _judge("upheld"),
    "judge_refute": lambda: _judge(
        "refuted", "disputed", counter_evidence="src/auth.py:38 already guards this"
    ),
    # Dispositive on its face, but the judge could not check the evidence --
    # §6.5 must downgrade this to `unproven` before anything counts it.
    "judge_unverifiable": lambda: _judge("refuted", "unverifiable"),
    "judge_amend": lambda: _judge("amended", amended_claim="the guard is weak, not missing"),
    # A well-formed but empty verdict set. Unlike a critique round there is
    # no honest empty result here, so this must be read as a failure.
    "judge_nothing": lambda: print(json.dumps({"verdicts": []})),
    # Answers only the FIRST claim it was shown. Passes validation -- the
    # schema requires at least one verdict, not one per claim -- so the
    # claims it skipped would look merely unproven, and the discard rule
    # would then close them as terminal without anyone having judged them.
    "judge_partial": lambda: _judge_first_only(),
    # Critiques normally in round 1, then fails the same way in every
    # judging round -- what a real friend did when its login lapsed
    # mid-run. Two identical failures get it disabled by the repeat tracker;
    # the claims it was supposed to judge must then read `incomplete`, not
    # `discarded` as though it had looked twice and failed.
    "judge_fail": lambda: _fail_judging(),
    # Fails in round 2 only, then judges normally: one unrelated friend's
    # failure must mark ITS slice incomplete, not every below-quorum claim
    # in the run. (No shared prefix with judge_fail: modes match by prefix.)
    "judge_absent_once": lambda: _fail_judging() if _round() == 2 else _judge("upheld"),
    # Amends, but could not check the evidence -- the evidence rule turns
    # this into `unproven`, which must not be reported as a final-round
    # amendment. (No shared prefix with judge_amend or judge_unverifiable.)
    "judge_shaky_amend": lambda: _judge(
        "amended", "unverifiable", amended_claim="the guard is weak, not missing"
    ),
}


def _round() -> int:
    """This round's number, from the prompt file's `round-N` directory."""
    assert PROMPT_FILE is not None
    return int(Path(PROMPT_FILE).parent.name.removeprefix("round-"))


def _fail_judging() -> None:
    print("Error: scripted judging failure", file=sys.stderr)
    raise SystemExit(1)


def _judge_first_only() -> None:
    claims = _claims_in_prompt()
    out = []
    for claim in claims[:1]:
        out.append(
            {
                "claim_id": claim["id"],
                "verdict": "upheld",
                "confidence": "high",
                "evidence_assessment": "confirmed",
                "reasoning": "only answered the first claim on purpose",
                "counter_evidence": None,
                "amended_claim": None,
            }
        )
    print(json.dumps({"verdicts": out}))


def _judgement_for(mode: str):
    """The judging behaviour for `mode`, matched by prefix.

    A fake friend's mode travels in the LENS slot of `--friend fake:<mode>`,
    and a claim's ledger identity is `cli/lens` -- so two friends with the
    same mode would be one identity, which the runner refuses before any
    run that judges (§8.1: a roster entry must be distinct). Matching by prefix means `judge_uphold_a` and `judge_uphold_b` behave
    identically while remaining two distinct friends.
    """
    for key, behaviour in _JUDGEMENTS.items():
        if mode.startswith(key):
            return behaviour
    return None


def main() -> int:
    mode = POSITIONALS[0] if POSITIONALS else "good"

    judgement = _judgement_for(mode)
    if judgement is not None:
        # A friend's mode is fixed for the whole run, so the same mode has
        # to answer two different questions. Which one it was asked is
        # visible in the prompt: a judging round carries a claim slice, a
        # critique round does not.
        if _claims_in_prompt():
            judgement()
        else:
            _own_finding()
        return 0

    if mode == "hang":
        # Spawn a child, then hang: the runner must reap the whole group.
        # If a pidfile path was passed positionally, write the child's pid
        # there instead of relying on stdout captured after a group kill,
        # which is not guaranteed to be readable/flushed by that point.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        if len(POSITIONALS) > 1:
            Path(POSITIONALS[1]).write_text(str(child.pid))
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
        _descendant(POSITIONALS[1:])
        return 0

    if mode == "grandchild":
        # Attack: a friend whose child spawns its own child (a grandchild
        # relative to the runner). None of these levels call setsid, so
        # they all remain in the same process group as the friend itself
        # and must all be reachable by killpg.
        pidfile_child, pidfile_grandchild = POSITIONALS[1], POSITIONALS[2]
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
        pidfile = POSITIONALS[1]
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
        pidfile = POSITIONALS[1]
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
        pidfile = POSITIONALS[1]
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

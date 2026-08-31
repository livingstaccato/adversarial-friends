"""End-to-end tests for `afriend run --mode report`: signal-driven teardown
(Task 12 review, Finding 1), orphaned-descendant surfacing, and `afriend
doctor`.

Proven here by actually sending SIGTERM/SIGINT to a real `afriend run`
process and inspecting real OS/git state afterward -- mutation testing
(used throughout the original submission) cannot reach this bug at all,
since it is specifically about what happens when Python's normal
exception-unwinding machinery never gets a chance to run. Reproduced
manually first (exact commands and output are in the task report) before
being written as these automated tests.

See tests/e2e_helpers.py for the safe-PATH subprocess harness this file (and
its siblings test_run_end_to_end_basics.py and
test_run_end_to_end_isolation.py) share.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time

from e2e_helpers import AF, _env, _git_commit, _git_repo, run_af


def _ps_all() -> list[tuple[int, int, str]]:
    """Return (pid, ppid, command) for every process, via the real `ps`
    (not the safe-PATH restricted one -- this reads the test's own host
    process table, nothing to do with what `afriend` itself can execute).

    `-ww` is load-bearing, not cosmetic. Without it `ps` truncates the
    command column to the terminal width, defaulting to 80 when there is no
    tty -- and the `pid`/`ppid` columns eat 16 of those, leaving 64. Every
    caller below identifies processes by substring-matching the command
    (`"fake_friend.py" in cmd`, `"hang" in cmd`, the escapee's marker
    comment), and all of those substrings sit *after* a long absolute path,
    so they were being cut off entirely. That made the signal-teardown
    tests fail on a CI runner whose checkout path was long enough to blow
    the 64-character budget, while passing locally and in a container where
    it was not, reported as "friend process never started" when the friend
    had in fact started and was sitting right there in the process table.
    """
    result = subprocess.run(
        ["ps", "-eo", "pid,ppid,command", "-ww"], capture_output=True, text=True, check=True
    )
    rows = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            pid, ppid, command = parts
            rows.append((int(pid), int(ppid), command))
    return rows


def _wait_until(predicate, timeout=10.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_signal_tears_everything_down(tmp_path, sig: int):
    # The run under test gets its OWN temp directory, and the leftover check
    # below looks only in there.
    #
    # It used to glob the shared system temp directory, which made the
    # assertion "no afriend anywhere on this machine has an isolation
    # directory right now". Any concurrent run failed it -- and this project
    # is routinely pointed at its own source while its tests run, so that is
    # not a hypothetical. Observed: a crossexam in another session left
    # `af-isolation-r2-*` in $TMPDIR and both signal tests failed, reporting
    # a teardown bug in code that had torn down correctly.
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir()
    env = _env({"TMPDIR": str(private_tmp)})

    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# spec\n")

    proc = subprocess.Popen(
        [
            sys.executable,
            str(AF),
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:hang:repo",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the friend (fake_friend.py in "hang" mode) to actually
        # start and for it to have spawned its own child, so the signal is
        # sent to a genuinely live process tree, not a not-yet-started one.
        # 30s, not the 10s default: repo scope does a snapshot commit and a
        # `git worktree add` *before* the friend is ever spawned, and on a
        # cold CI runner those git operations are far slower than locally.
        # This window only bounds "has it started yet", so being generous
        # costs nothing on a fast machine and removes a false failure on a
        # slow one.
        friend_pid = _wait_until(
            lambda: next(
                (
                    pid
                    for pid, ppid, cmd in _ps_all()
                    if ppid == proc.pid and "fake_friend.py" in cmd and "hang" in cmd
                ),
                None,
            ),
            timeout=30.0,
        )
        if not friend_pid:
            # Asserting bare here reports "never started" and nothing else,
            # which is unactionable when it happens on CI and not locally --
            # the interesting case is afriend having already died before it
            # ever dispatched, taking its reason with it.
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=10)
                raise AssertionError(
                    f"friend never started: afriend already exited "
                    f"{proc.returncode} before dispatching.\n"
                    f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
                )
            visible = "\n".join(
                f"  pid={pid} ppid={ppid} {cmd[:120]}"
                for pid, ppid, cmd in _ps_all()
                if "fake_friend" in cmd or "afriend" in cmd or ppid == proc.pid
            )
            raise AssertionError(
                f"friend never started within 30s, but afriend (pid {proc.pid}) "
                f"is still running. Related processes:\n{visible or '  (none)'}"
            )
        child_pid = _wait_until(
            lambda: next(
                (
                    pid
                    for pid, ppid, cmd in _ps_all()
                    if ppid == friend_pid and "time.sleep(600)" in cmd
                ),
                None,
            )
        )
        assert child_pid, "friend's own child never started within the wait window"

        worktrees_during = subprocess.run(
            ["git", "worktree", "list"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert len(worktrees_during.stdout.strip().splitlines()) == 2, (
            "expected the friend's private worktree to be live before signalling"
        )

        started = time.monotonic()
        proc.send_signal(sig)
        proc.wait(timeout=15)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr = proc.stderr.read()
    assert elapsed < 15, (
        f"afriend took {elapsed:.1f}s to exit after signal {sig} -- teardown blocked"
    )
    assert proc.returncode == 128 + sig, (proc.returncode, stderr)
    assert f"aborted by signal {sig}" in stderr, stderr

    worktrees_after = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert len(worktrees_after.stdout.strip().splitlines()) == 1, (
        f"leftover worktree registration after signal {sig}:\n{worktrees_after.stdout}"
    )

    leftover_iso_dirs = list(private_tmp.glob("af-isolation-*"))
    assert leftover_iso_dirs == [], f"leftover isolation temp dirs: {leftover_iso_dirs}"

    assert not _pid_alive(friend_pid), "friend process survived the signal"
    assert not _pid_alive(child_pid), "friend's child survived the signal"


def test_sigterm_tears_down_isolation_and_kills_the_friend_and_its_child(tmp_path):
    """Without an installed handler, SIGTERM's default disposition kills
    `afriend` immediately -- no Python-level unwinding, no `finally`
    blocks, no teardown at all. Confirms the installed handler changes
    that: prompt exit (128+SIGTERM), no leftover git worktree
    registration, no leftover af-isolation-* temp dir, and both the
    friend and its own child process are gone."""
    _assert_signal_tears_everything_down(tmp_path, signal.SIGTERM)


def test_sigint_tears_down_isolation_and_kills_the_friend_and_its_child(tmp_path):
    """SIGINT's default handler does raise KeyboardInterrupt, but (before
    this fix) that exception immediately re-blocks inside
    ThreadPoolExecutor.__exit__'s own shutdown(wait=True), which waits on
    the same still-hung worker -- so cleanup still never ran in practice.
    Same assertions as the SIGTERM case, confirming the installed handler
    and explicit shutdown(wait=False, cancel_futures=True) fix this path
    too, not only the "no handler at all" SIGTERM path."""
    _assert_signal_tears_everything_down(tmp_path, signal.SIGINT)


def test_orphans_suspected_is_surfaced_end_to_end(tmp_path):
    """Task 12 review, Finding 4: spawn.SpawnResult.orphans_suspected was
    plumbed into cmd_run's .meta file and status string, but never actually
    exercised end to end (the only tests that could produce a genuine
    orphan needed a pidfile argument fake:<mode> dispatch has no room to
    pass -- see fake_friend.py's "leaky_escape" mode, added for exactly
    this test). "leaky_escape" spawns a setsid-escaping descendant that
    keeps this friend's inherited stdout/stderr pipes open, then exits 0
    immediately with a valid payload -- the run succeeds (exit 0, a real
    finding), and orphans_suspected must still be True and visible in both
    the human-facing report and the machine-facing run.json, not just the
    per-friend .meta file."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    try:
        result = run_af(tmp_path, artifact, "--friend", "fake:leaky_escape")
        assert result.returncode == 0, result.stderr
        runs = sorted((tmp_path / "runs").iterdir())

        report = (runs[0] / "report.md").read_text()
        assert "orphans suspected" in report.lower()
        assert "leaky escape probe" in report  # the real finding still came through

        meta = json.loads((runs[0] / "run.json").read_text())
        assert "orphans suspected" in meta["friends"][0]["status"].lower()

        friend_meta_txt = (runs[0] / "round-1" / "fake-leaky_escape-0.meta").read_text()
        assert "orphans_suspected=True" in friend_meta_txt
    finally:
        # Same irreducible limitation as test_setsid_escapee_is_not_reaped
        # in test_spawn.py: nothing this process (or afriend, or spawn.py)
        # does can reach a setsid escapee through the process group. Clean
        # it up directly so it doesn't leak past this test.
        escapee_pid = next(
            (pid for pid, ppid, cmd in _ps_all() if "af-leaky-escape-marker" in cmd), None
        )
        if escapee_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(escapee_pid, signal.SIGKILL)


def test_doctor_reports_missing_clis_and_exits_3(tmp_path):
    """With the safe (agent-CLI-free) PATH, every real adapter must be
    reported missing and doctor must exit 3 (mirrors NoFriendsError's exit
    code, since discover_clis finds nothing usable)."""
    result = subprocess.run(
        [sys.executable, str(AF), "doctor"],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert result.returncode == 3, result.stderr
    assert "codex" in result.stdout
    assert "missing" in result.stdout


def test_doctor_reports_ollama_opt_out_not_a_stub_message(tmp_path):
    """doctor used to print "unimplemented" for ollama because no HTTP
    transport existed. It ships now, and the safe test environment's HTTP
    discovery opt-out is itself a canonical readiness state. Doctor reports
    that state without probing the endpoint, never a build-status message."""
    result = subprocess.run(
        [sys.executable, str(AF), "doctor"],
        capture_output=True,
        text=True,
        env=_env(),
    )
    ollama_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("ollama"))
    assert "unimplemented" not in ollama_line.lower()
    assert "state=disabled" in ollama_line
    assert "AF_NO_HTTP_DISCOVERY" in ollama_line
    assert "readonly=False" in ollama_line  # never claims an enforcement it has not made

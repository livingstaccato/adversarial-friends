"""End-to-end tests for `af run --mode report`.

Every subprocess launched here runs under a *constructed* PATH containing
only a symlink to the real `git` binary -- never the real, inherited PATH.
This machine may have codex/claude/agy/opencode installed for interactive
use; inheriting the real PATH would let friend discovery find them and shell
out to real, metered CLIs on every test run. `git` alone is let through
because isolation.py shells out to it for worktree snapshots, and several
tests below exercise the repo-scope isolation path on purpose.

AF_FAKE_FRIEND is the injection point that keeps `--friend fake:<mode>`
entirely off real CLIs: `cmd_run` treats cli == "fake" as a dedicated branch
that runs `$AF_FAKE_FRIEND <mode>` directly, bypassing adapter/build_argv
lookup, capability derivation, and roster resolution entirely (no adapter
named "fake" ever exists in the registry). This is a narrower and more
explicit injection point than routing a fake adapter through build_argv:
build_argv's prompt-mode contract expects to receive the untrusted document
as its prompt, but fake_friend.py's contract is a scripted MODE name as
argv[1] -- forcing that through build_argv would mean either lying to
build_argv about the prompt file's contents or inventing a new prompt_mode
in adapters.py (out of Task 12's scope, and shared by every real adapter's
tests). Keeping "fake" a small, self-contained branch in cmd_run confines
the test-only mechanism to the one file that is allowed to know about it.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from adversarial_friends import adapters, cli

REPO = Path(__file__).resolve().parents[1]
AF = REPO / "skills" / "adversarial-friends" / "scripts" / "af"
FAKE = REPO / "tests" / "fake_friend.py"


def _safe_path_dir() -> Path:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("git not available on this machine")
    d = Path(tempfile.mkdtemp(prefix="af-safe-path-"))
    (d / "git").symlink_to(real_git)
    return d


def _env(extra=None):
    env = {
        "PATH": str(_safe_path_dir()),
        "AF_FAKE_FRIEND": f"{sys.executable} {FAKE}",
    }
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    if extra:
        env.update(extra)
    return env


def run_af(tmp_path, artifact, *extra, env_extra=None):
    return subprocess.run(
        [sys.executable, str(AF), "run", str(artifact), "--mode", "report",
         "--out", str(tmp_path / "runs"), *extra],
        capture_output=True, text=True,
        env=_env(env_extra),
    )


def _git_commit(root: Path, message: str) -> None:
    # -c commit.gpgsign=false: this repo is disposable test scaffolding
    # under tmp_path, not the project's real history -- signing it would
    # only fail because _env() deliberately strips SSH_AUTH_SOCK/GPG_TTY
    # (see the safe-PATH rationale above), on any machine where
    # commit.gpgsign is enabled globally.
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
                   cwd=root, check=True, capture_output=True, env=_env())


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True,
                                    env=_env())
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    return root


def test_report_run_produces_ledger_and_report(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    assert ledger, "ledger should not be empty"
    assert json.loads(ledger[0])["type"] == "claim"
    assert "# Adversarial review" in (runs[0] / "report.md").read_text()


def test_failed_friend_is_reported_not_silently_dropped(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:offtopic")
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()


def test_zero_friends_exits_3(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact)
    assert result.returncode == 3


def test_missing_artifact_exits_2(tmp_path):
    result = run_af(tmp_path, tmp_path / "nope.md", "--friend", "fake:good")
    assert result.returncode == 2


# --- Adversarial break-it attempts beyond the brief's four required tests -


def test_mode_other_than_report_exits_2(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = subprocess.run(
        [sys.executable, str(AF), "run", str(artifact), "--mode", "gate",
         "--out", str(tmp_path / "runs"), "--friend", "fake:good"],
        capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 2
    assert "gate" in result.stderr.lower() or "not implemented" in result.stderr.lower()


def test_unknown_cli_in_friend_flag_exits_2_not_3(tmp_path):
    """Landmine #2 (inherited from Task 10): a config typo naming an
    unknown cli must be a usage error (exit 2), not 'no usable friends'
    (exit 3). This test exercises the --friend flag path in cli.py, which
    is Task 12's own code -- it never calls roster.resolve's overrides
    parameter (see module docstring), so it cannot inherit Task 10's
    'overrides=[] silently falls through to auto-discovery' bug either."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "no-such-cli:ops")
    assert result.returncode == 2, result.stderr
    assert "no-such-cli" in result.stderr


def test_artifact_outside_git_repo_downgrades_every_friend_to_doc_scope(tmp_path):
    """The artifact lives directly under tmp_path, which is never a git
    repository in these tests -- this is already exercised implicitly by
    every test above, but this test asserts the required, user-visible
    consequence: the report header says so."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "not inside a git repository" in report.lower()
    assert "doc scope" in report.lower()


def test_artifact_in_a_nested_subdirectory_of_a_repo_resolves_the_real_root(tmp_path):
    """isolation.snapshot_commit requires a repository ROOT and raises for
    a nested subdirectory. cmd_run must resolve the real root itself
    (via the artifact's enclosing git repo) rather than handing
    snapshot_commit the artifact's own (nested) directory."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    nested = repo / "docs" / "specs"
    nested.mkdir(parents=True)
    artifact = nested / "spec.md"
    artifact.write_text("# spec nested three levels deep\n")

    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "not inside a git repository" not in report.lower()


def test_a_slow_friend_timing_out_does_not_prevent_others_from_being_reported(tmp_path):
    """One friend hangs past the timeout; a second succeeds. The run must
    still exit 0 (at least one friend produced a usable result) and the
    report must show both outcomes -- the timeout must not silently drop
    either friend's row."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--timeout", "2",
                    "--friend", "fake:good", "--friend", "fake:hang")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "timeout" in report.lower() or "failed" in report.lower()
    assert "# c-0001" in report or "c-0001" in report


def test_all_friends_failing_exits_1_and_says_so(tmp_path):
    """Every dispatched friend fails (offtopic output both times): the run
    mechanism itself still completes and writes a report, but nothing
    usable came back. Distinct from test_zero_friends_exits_3 (which never
    even resolves any friends to run) -- here two friends actually run.
    Exit 1 ('gate blocked or incomplete') is used to distinguish this from
    exit 0's implicit claim that at least one friend's verdict is
    trustworthy."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:offtopic",
                    "--friend", "fake:offtopic")
    assert result.returncode == 1, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()


def test_a_run_directory_that_already_exists_fails_cleanly(tmp_path):
    """Simulates a run-id collision by pre-creating the directory the CLI
    would otherwise pick; since af generates its own run id internally the
    only reachable way to force this from outside is --out pointing at a
    path that is itself already occupied, so this drives
    runstore.RunStore directly instead -- see
    test_runstore.test_reusing_a_run_id_fails_cleanly_instead_of_mixing_ledgers
    for the unit-level check. This test instead confirms --out pointing at
    an existing plain FILE (not a directory) fails cleanly (a handled
    UsageError, exit 2) rather than crashing with a raw, unhandled
    NotADirectoryError traceback -- reproduced directly against a first
    version of RunStore.__init__ that called mkdir(parents=True) with no
    try/except around it."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    out_path = tmp_path / "runs"
    out_path.write_text("not a directory")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 2, result.stderr
    assert "af:" in result.stderr
    assert "Traceback" not in result.stderr


def test_two_friends_with_identical_cli_and_lens_get_distinct_names(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    names = [f["name"] for f in meta["friends"]]
    assert len(names) == len(set(names)) == 2


def test_symlinked_artifact_is_reviewed_via_its_real_content(tmp_path):
    real = tmp_path / "real_spec.md"
    real.write_text("# spec\nreal content behind a symlink\n")
    link = tmp_path / "link_spec.md"
    link.symlink_to(real)

    result = run_af(tmp_path, link, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert meta["artifact"] == "link_spec.md"
    copied = list((runs[0] / "artifact").iterdir())[0]
    assert copied.read_text() == "# spec\nreal content behind a symlink\n"


def test_doc_scope_friend_actually_runs_inside_its_own_private_directory(tmp_path):
    """Direct, unambiguous proof that dispatch's `cwd` is the friend's own
    isolation directory -- not, say, Path.cwd() of the `af` process itself
    (the brief's own reference `cmd_run` passed exactly that, unconditionally,
    for every friend; wiring isolation in at all is the corrected brief's
    requirement #4). fake:cwd_probe reports its own process cwd back as a
    finding's evidence field, so this asserts on it directly rather than
    inferring wiring indirectly from cleanup side effects."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:cwd_probe")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [json.loads(line) for line in
             (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    reported_cwd = Path(ledger[0]["evidence"])
    assert reported_cwd.name == "fake-cwd_probe-0"  # == cwd_for[spec.name]'s basename
    assert reported_cwd != Path.cwd()
    assert not reported_cwd.exists()  # torn down once af run returned


def test_repo_scope_friend_gets_a_real_private_worktree(tmp_path):
    """The "fake" cli defaults to doc-scope, so none of the tests above (or
    fake:cwd_probe just above) exercise isolation.snapshot_commit/
    add_worktree through `af run` at all -- the exact gap the corrected
    brief warns about ("the brief's cmd_run never calls isolation, which
    would ship Task 9 as dead code"). fake:cwd_probe:repo (see
    _specs_from_flags) forces scope="repo" for the test-only fake cli, so
    this drives a REAL `git worktree add` off a REAL snapshot commit,
    without needing any actual agent CLI to be present. The reported cwd
    must both be a real git worktree (checked out from the snapshot, with
    the repo's tracked file visible) and be torn down by the time `af run`
    returns."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# spec\n")

    result = run_af(tmp_path, artifact, "--friend", "fake:cwd_probe:repo")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [json.loads(line) for line in
             (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    reported_cwd = Path(ledger[0]["evidence"])
    assert reported_cwd.name == "fake-cwd_probe-0"
    assert reported_cwd != repo  # its OWN worktree, not the source repo directly
    assert not reported_cwd.exists()  # torn down by the time af run returned

    # The "fake" cli always carries a synthetic, hardcoded capability
    # (readonly=False -- see cli._FAKE_CAPABILITY) regardless of the
    # scope requested via the fake:<mode>:repo suffix. run.json's
    # friends[].readonly must reflect THAT, not `spec.scope == "repo"`
    # (which would say True here) -- the one reachable end-to-end case
    # where a re-derivation and the real capability actually diverge, so
    # it is the only place this can be caught without an in-process call.
    meta = json.loads((runs[0] / "run.json").read_text())
    assert meta["friends"][0]["readonly"] is False

    worktrees = subprocess.run(["git", "worktree", "list"], cwd=repo, check=True,
                               capture_output=True, text=True, env=_env())
    # Only the main working tree remains registered: the friend's private
    # worktree (proven above to have existed while the friend ran, holding
    # the checked-out snapshot) was cleanly removed afterward.
    assert len(worktrees.stdout.strip().splitlines()) == 1


def test_dispatch_never_rederives_capability_from_requested_scope():
    """Requirement: "Use the capability build_argv returns; never
    re-derive it." An adapter with NO readonly_argv at all never gets a
    readonly flag emitted by build_argv -- capability.readonly is False --
    even when scope="repo" is explicitly requested. Neither
    _specs_from_flags nor roster.resolve's auto-discovery path (the only
    two spec sources cmd_run actually uses) can ever produce this
    combination on their own: both always derive scope FROM
    adapter.readonly_argv, so spec.scope and the true capability never
    diverge through any input reachable via the real --friend/discovery
    CLI surface (opencode -- the one shipped adapter with empty
    readonly_argv -- always resolves to scope="doc" through both paths).
    A subprocess e2e test therefore cannot exercise this rule; this calls
    cli._dispatch directly, in-process, with a hand-built Adapter (never
    routed through load_adapters, and with a deliberately nonexistent
    binary name -- this must NEVER risk resolving to any real,
    PATH-installed CLI, in-process calls are not covered by this file's
    safe-PATH subprocess sandboxing at all) to prove the naive
    re-derivation `readonly = spec.scope == "repo"` is NOT what cli.py
    actually reports."""
    no_readonly_mode = adapters.Adapter(
        name="norepro", binary="af-test-nonexistent-binary-xyz",
        base_argv=[], prompt_mode="stdin", prompt_flag="", readonly_argv=[],
        schema_flag="", model_flag="", internal_timeout_flag="", effort_kind="none",
    )
    registry = {"norepro": no_readonly_mode}
    spec = adapters.FriendSpec(name="norepro-x", cli="norepro", lens="x", model=None,
                               effort=None, scope="repo", timeout=5)
    prompt_file = REPO / "tests" / "fake_friend.py"  # any existing text file
    schema_file = prompt_file  # build_argv never reads its contents
    _, capability, outcome = cli._dispatch(spec, REPO, registry, None,
                                           prompt_file, schema_file)
    assert capability.readonly is False, (
        "spec.scope == 'repo' but this adapter has no readonly_argv at all -- "
        "re-deriving readonly from spec.scope instead of using build_argv's "
        "own Capability would get this wrong"
    )
    # The binary name is fabricated and cannot exist on any machine's PATH:
    # confirms nothing was actually spawned, so the capability assertion
    # above is not incidentally masked by a real process having run.
    assert outcome.failure_reason == "binary not found: af-test-nonexistent-binary-xyz"


class _StopAfterResolve(Exception):
    """Raised by the resolve() spy below, purely to abort cmd_run right
    after the call this test cares about -- nothing past that point (real
    isolation setup, real dispatch) needs to run for this test's purpose."""


def test_cli_run_passes_timeout_through_to_roster_resolve(monkeypatch, tmp_path):
    """Task 12 review, Finding 2: confirms cmd_run's own plumbing, not just
    roster.resolve's new parameter (see test_roster.py for that). --friend
    is deliberately omitted so cmd_run takes the auto-discovery branch and
    actually calls roster.resolve."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    captured: dict = {}

    def _spy_resolve(*args, **kwargs):
        captured.update(kwargs)
        raise _StopAfterResolve

    monkeypatch.setattr(cli, "resolve", _spy_resolve)
    parser = cli.build_parser()
    parsed = parser.parse_args(
        ["run", str(artifact), "--mode", "report", "--timeout", "37"]
    )
    with pytest.raises(_StopAfterResolve):
        cli.cmd_run(parsed)
    assert captured.get("timeout") == 37


# --- Signal teardown (Task 12 review, Finding 1) -------------------------
#
# Proven here by actually sending SIGTERM/SIGINT to a real `af run` process
# and inspecting real OS/git state afterward -- mutation testing (used
# throughout the rest of this file and the original submission) cannot
# reach this bug at all, since it is specifically about what happens when
# Python's normal exception-unwinding machinery never gets a chance to run.
# Reproduced manually first (exact commands and output are in the task
# report) before being written as this automated test.


def _ps_all() -> list[tuple[int, int, str]]:
    """Return (pid, ppid, command) for every process, via the real `ps`
    (not the safe-PATH restricted one -- this reads the test's own host
    process table, nothing to do with what `af` itself can execute)."""
    result = subprocess.run(["ps", "-eo", "pid,ppid,command"],
                            capture_output=True, text=True, check=True)
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
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# spec\n")

    proc = subprocess.Popen(
        [sys.executable, str(AF), "run", str(artifact), "--mode", "report",
         "--out", str(tmp_path / "runs"), "--friend", "fake:hang:repo"],
        env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Wait for the friend (fake_friend.py in "hang" mode) to actually
        # start and for it to have spawned its own child, so the signal is
        # sent to a genuinely live process tree, not a not-yet-started one.
        friend_pid = _wait_until(lambda: next(
            (pid for pid, ppid, cmd in _ps_all()
             if ppid == proc.pid and "fake_friend.py" in cmd and "hang" in cmd), None))
        assert friend_pid, "friend process never started within the wait window"
        child_pid = _wait_until(lambda: next(
            (pid for pid, ppid, cmd in _ps_all()
             if ppid == friend_pid and "time.sleep(600)" in cmd), None))
        assert child_pid, "friend's own child never started within the wait window"

        worktrees_during = subprocess.run(["git", "worktree", "list"], cwd=repo, check=True,
                                          capture_output=True, text=True, env=_env())
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
    assert elapsed < 15, f"af took {elapsed:.1f}s to exit after signal {sig} -- teardown blocked"
    assert proc.returncode == 128 + sig, (proc.returncode, stderr)
    assert f"aborted by signal {sig}" in stderr, stderr

    worktrees_after = subprocess.run(["git", "worktree", "list"], cwd=repo, check=True,
                                     capture_output=True, text=True, env=_env())
    assert len(worktrees_after.stdout.strip().splitlines()) == 1, (
        f"leftover worktree registration after signal {sig}:\n{worktrees_after.stdout}"
    )

    leftover_iso_dirs = list(Path(tempfile.gettempdir()).glob("af-isolation-*"))
    assert leftover_iso_dirs == [], f"leftover isolation temp dirs: {leftover_iso_dirs}"

    assert not _pid_alive(friend_pid), "friend process survived the signal"
    assert not _pid_alive(child_pid), "friend's child survived the signal"


def test_sigterm_tears_down_isolation_and_kills_the_friend_and_its_child(tmp_path):
    """Without an installed handler, SIGTERM's default disposition kills
    `af` immediately -- no Python-level unwinding, no `finally` blocks, no
    teardown at all. Confirms the installed handler changes that: prompt
    exit (128+SIGTERM), no leftover git worktree registration, no leftover
    af-isolation-* temp dir, and both the friend and its own child process
    are gone."""
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
    plumbed into cli.py's .meta file and status string, but never actually
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
        # in test_spawn.py: nothing this process (or af, or spawn.py) does
        # can reach a setsid escapee through the process group. Clean it up
        # directly so it doesn't leak past this test.
        escapee_pid = next((pid for pid, ppid, cmd in _ps_all()
                            if "af-leaky-escape-marker" in cmd), None)
        if escapee_pid is not None:
            try:
                os.kill(escapee_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_doctor_reports_missing_clis_and_exits_3(tmp_path):
    """With the safe (agent-CLI-free) PATH, every real adapter must be
    reported missing and doctor must exit 3 (mirrors NoFriendsError's exit
    code, since discover_clis finds nothing usable)."""
    result = subprocess.run(
        [sys.executable, str(AF), "doctor"],
        capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 3, result.stderr
    assert "codex" in result.stdout
    assert "missing" in result.stdout

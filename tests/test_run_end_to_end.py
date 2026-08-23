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
import threading
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


def test_friend_ollama_is_rejected_as_unimplemented_not_a_silent_empty_binary(tmp_path):
    """I3 (whole-branch review): ollama.toml declares transport="http" with
    an empty `binary` (there is no HTTP transport in this build -- only
    exec/Popen dispatch). Before this fix, --friend ollama:x fell straight
    through to build_argv, which handed spawn.run_process argv[0] == "" and
    failed opaquely as "binary not found: " -- while README, SKILL.md's
    frontmatter, af doctor, and troubleshooting.md all implied ollama
    worked. Rejecting outright (exit 2, before dispatch) rather than
    implementing HTTP transport (a feature, not a fix)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "ollama:ops")
    assert result.returncode == 2, result.stderr
    assert "ollama" in result.stderr
    assert "not implemented" in result.stderr.lower()


def test_preset_other_than_inherit_is_rejected(tmp_path):
    """I5: --preset is accepted and printed in the report header, but
    nothing reads it -- no code path varies behavior by preset name.
    Rejected explicitly (same pattern as --mode) rather than silently
    accepted and doing nothing."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--preset", "thorough", "--friend", "fake:good")
    assert result.returncode == 2, result.stderr
    assert "thorough" in result.stderr
    assert "not implemented" in result.stderr.lower()


def test_preset_inherit_is_accepted(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--preset", "inherit", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr


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


def test_a_slow_friend_timing_out_does_not_prevent_others_from_being_reported(
    monkeypatch, tmp_path
):
    """One friend hangs past the timeout; a second succeeds. The run must
    still exit 0 (at least one friend produced a usable result) and the
    report must show both outcomes -- the timeout must not silently drop
    either friend's row.

    Run in-process (not via the `run_af` subprocess helper every other test
    in this file uses) specifically so cli.KILL_GRACE_S can be monkeypatched
    down: I4 (spec 11.3) makes the real kill deadline `--timeout +
    KILL_GRACE_S` (60s in production), so a subprocess run with `--timeout 2`
    would now take 62+ real seconds to observe the same timeout behavior
    this test only needs to confirm the MECHANISM for, not the exact
    production grace window (asserted separately, cheaply, in
    test_kill_grace_period_constant_is_sixty_seconds below)."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setattr(cli, "KILL_GRACE_S", 1)
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    parser = cli.build_parser()
    parsed = parser.parse_args([
        "run", str(artifact), "--mode", "report", "--timeout", "2",
        "--out", str(tmp_path / "runs"),
        "--friend", "fake:good", "--friend", "fake:hang",
    ])
    returncode = cli.cmd_run(parsed)
    assert returncode == 0
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "timeout" in report.lower() or "failed" in report.lower()
    assert "# c-0001" in report or "c-0001" in report


# --- I4: the runner's kill deadline must be strictly greater than --timeout
#
# The kill deadline previously equaled the CLI's internal timeout exactly
# (spec 11.3 requires strictly greater), so a friend with its own internal
# timeout (agy --print-timeout) could be killed by the runner at the exact
# instant it was trying to report its own timeout cleanly, mid-write.


def test_kill_grace_period_constant_is_sixty_seconds():
    assert cli.KILL_GRACE_S == 60


def test_kill_deadline_is_strictly_greater_than_the_configured_timeout(monkeypatch, tmp_path):
    """Direct proof of the arithmetic (not just "eventually times out"):
    with KILL_GRACE_S monkeypatched down to 1s (see the test above this
    section for why), a friend that hangs must survive strictly past
    --timeout alone (1s) and be killed only once timeout + KILL_GRACE_S
    (2s) has elapsed."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setattr(cli, "KILL_GRACE_S", 1)
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    parser = cli.build_parser()
    parsed = parser.parse_args([
        "run", str(artifact), "--mode", "report", "--timeout", "1",
        "--out", str(tmp_path / "runs"), "--friend", "fake:hang",
    ])
    started = time.monotonic()
    returncode = cli.cmd_run(parsed)
    elapsed = time.monotonic() - started
    assert returncode == 1  # the only dispatched friend timed out
    assert elapsed >= 2.0, (
        f"killed after {elapsed:.2f}s -- expected >= timeout(1) + KILL_GRACE_S(1) == 2s"
    )


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


# --- I2: corroboration must survive exact-merge, end to end ---------------
#
# report.render never touched claim.origin/claim.lens, and cli.py appended
# only KEPT claims and aliases to the ledger -- the duplicate claim record
# itself was never written. Four friends independently finding the same
# defect collapsed to one claim with origin of length 1 plus dangling alias
# references (an Alias.duplicate id with no matching `claim` record
# anywhere in claims.jsonl). fake_friend.py's mode dispatch falls back to
# "good" for any unrecognized mode name (see fake_friend.py's MODES.get
# fallback), so --friend fake:security and --friend fake:ops (both real
# lens files, so neither trips the "no lens file found" downgrade) produce
# BYTE-IDENTICAL findings under two DISTINCT (cli, lens) origins --
# "fake/security" and "fake/ops" -- exactly the exact-merge scenario this
# fix targets, without needing two different real agent CLIs.


def test_corroborating_friends_leave_no_dangling_alias_reference_in_the_ledger(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    records = [json.loads(line) for line in
              (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    claim_ids = {r["id"] for r in records if r["type"] == "claim"}
    aliases = [r for r in records if r["type"] == "alias"]
    assert aliases, "expected at least one alias from the identical-finding merge"
    for alias in aliases:
        assert alias["duplicate"] in claim_ids, (
            f"alias {alias} references a duplicate id with no claim record "
            f"in the ledger -- known ids: {sorted(claim_ids)}"
        )
        assert alias["canonical"] in claim_ids


def test_corroborating_friends_origins_are_reconstructible_from_the_ledger_alone(tmp_path):
    """The canonical claim's OWN ledger record keeps whatever origin it had
    when first written (the ledger is append-only -- nothing already
    written is rewritten in place); it is the alias chain plus the
    duplicate's OWN claim record (see the dangling-reference test above)
    that lets a reader reconstruct full corroboration from claims.jsonl by
    itself, without needing the in-memory state a live `af run` process
    held. This is the ledger-level guarantee; the merged, ready-to-read
    origin list lives in report.md (see
    test_report_shows_corroboration_for_a_claim_multiple_friends_raised)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    records = [json.loads(line) for line in
              (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    claims_by_id = {r["id"]: r for r in records if r["type"] == "claim"}
    aliases = [r for r in records if r["type"] == "alias"]
    assert len(aliases) == 1
    alias = aliases[0]

    reconstructed_origin: set[str] = set(claims_by_id[alias["canonical"]]["origin"])
    reconstructed_origin |= set(claims_by_id[alias["duplicate"]]["origin"])
    assert reconstructed_origin == {"fake/security", "fake/ops"}


def test_report_shows_corroboration_for_a_claim_multiple_friends_raised(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "corroborated by 2 friends" in report
    assert "fake/security" in report and "fake/ops" in report
    # A single-friend-run finding must NOT claim corroboration it doesn't have.
    assert report.count("### c-") == 1  # the duplicate was merged, not double-listed


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


def test_cmd_run_from_a_background_thread_completes_rather_than_raising(monkeypatch, tmp_path):
    """Task 12 re-review, round 2, Finding 1 (Important): signal.signal()
    only works from the main thread of the main interpreter and raises
    ValueError from anywhere else. Before this fix, cmd_run called
    signal.signal() unguarded, so invoking it from a caller's own
    threading.Thread raised ValueError before cmd_run's own try even
    began -- no exit code, no report, no teardown attempted. cmd_run's own
    comment frames it as "library-ish" (the justification for restoring
    handlers unconditionally), so a non-main-thread caller is an audience
    the code already contemplates. Threads swallow exceptions raised in
    their target silently (they do not propagate to the joining thread),
    so this test captures the outcome through a shared dict rather than
    wrapping thread.join() in a bare try/except."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    parser = cli.build_parser()
    parsed = parser.parse_args([
        "run", str(artifact), "--mode", "report",
        "--out", str(tmp_path / "runs"), "--friend", "fake:good",
    ])
    outcome: dict = {}

    def _target():
        try:
            outcome["returncode"] = cli.cmd_run(parsed)
        except BaseException as exc:  # capturing intentionally, any exception counts
            outcome["exception"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive(), "cmd_run did not complete within 15s from a background thread"
    assert "exception" not in outcome, f"cmd_run raised from a background thread: {outcome.get('exception')!r}"
    assert outcome.get("returncode") == 0, outcome

    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any("main thread" in note for note in meta["downgrades"]), (
        "run completed but never recorded that signal-based abort was unavailable"
    )


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


def test_doctor_marks_ollama_unimplemented_not_merely_unprobed(tmp_path):
    """I3: af doctor's http-transport branch previously printed a neutral
    'reachability not probed by doctor' line for ollama, which reads as
    'supported but unverified' rather than 'not implemented at all in this
    build' -- misleading given --friend ollama:* is rejected outright."""
    result = subprocess.run(
        [sys.executable, str(AF), "doctor"],
        capture_output=True, text=True, env=_env(),
    )
    ollama_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("ollama"))
    assert "unimplemented" in ollama_line.lower()


# --- Lens wiring (Task 13 coordinator finding) ----------------------------
#
# Before this fix, cmd_run built exactly one prompt.txt (PROMPT_HEADER +
# artifact) before the dispatch loop and handed the same Path to every
# friend regardless of --friend cli:lens. LENS_DIR/available_lenses() only
# ever harvested filename stems for round-robin assignment and bookkeeping
# (friend naming, claim origin/lens fields) -- no code path ever read a
# lens file's prose into a prompt. Every friend therefore received a
# byte-identical, lens-blind prompt: the only diversity in a run was model
# diversity, and the lens name on a claim was decorative. These tests prove
# the fix by reading the actual <friend>.prompt files a real run writes to
# disk, not by inspecting cli.py internals.


def test_two_friends_with_different_lenses_get_demonstrably_different_prompts(tmp_path):
    """The core fix. security and ops are both real, shipped lens files
    with genuinely different prose. Each friend's prompt must carry its own
    lens's body -- frontmatter stripped, not appended whole -- while both
    still carry the shared contract header and the artifact."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    round_dir = runs[0] / "round-1"

    security_prompt = (round_dir / "fake-security-0.prompt").read_text()
    ops_prompt = (round_dir / "fake-ops-1.prompt").read_text()

    assert security_prompt != ops_prompt

    # Each prompt carries its own lens's distinctive prose, and not the
    # other lens's...
    assert "Attack the design as written" in security_prompt
    assert "Attack the design as written" not in ops_prompt
    assert "Ask what happens at 3am" in ops_prompt
    assert "Ask what happens at 3am" not in security_prompt

    # ...with the YAML frontmatter stripped, not carried into the prompt...
    assert "requires_failure_scenario:" not in security_prompt
    assert "requires_failure_scenario:" not in ops_prompt

    # ...and both still carry the shared contract header and the artifact.
    assert "Return ONLY a JSON object" in security_prompt
    assert "Return ONLY a JSON object" in ops_prompt
    assert "A design with a missing guard" in security_prompt
    assert "A design with a missing guard" in ops_prompt


def test_friend_with_unknown_lens_falls_back_to_generic_prompt_and_records_a_downgrade(tmp_path):
    """fake:good's lens slot is "good", which has no lenses/good.md file --
    every other fake-friend test in this file already relies on exactly
    this fallback (fake:offtopic, fake:hang, fake:cwd_probe, ...) and must
    keep working unchanged. The fallback must be explicit and visible, not
    silent: no --- LENS --- section in the written prompt, and a downgrade
    naming the friend and the missing lens in run.json."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    prompt_text = (runs[0] / "round-1" / "fake-good-0.prompt").read_text()
    assert "--- LENS ---" not in prompt_text
    assert "Return ONLY a JSON object" in prompt_text

    meta = json.loads((runs[0] / "run.json").read_text())
    assert any("fake-good-0" in note and "good" in note and "lens" in note.lower()
               for note in meta["downgrades"]), meta["downgrades"]


def test_advisory_flag_is_set_from_the_lens_requires_failure_scenario(tmp_path):
    """lenses/scope.md is the one shipped lens with
    requires_failure_scenario: false. A claim produced under it must come
    back with advisory=True -- previously hardcoded False for every claim
    regardless of lens. report.py already renders an *(advisory)* marker;
    only the runner ever needed to set the field truthfully."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:scope")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [json.loads(line) for line in
             (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    claims = [r for r in ledger if r["type"] == "claim"]
    assert claims and all(c["advisory"] is True for c in claims)

    report = (runs[0] / "report.md").read_text()
    assert "(advisory)" in report


def test_advisory_flag_is_false_for_a_lens_that_requires_a_failure_scenario(tmp_path):
    """The converse of the above: security.md sets
    requires_failure_scenario: true (the default for every lens but
    scope), so its claims must come back non-advisory."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [json.loads(line) for line in
             (runs[0] / "claims.jsonl").read_text().strip().splitlines()]
    claims = [r for r in ledger if r["type"] == "claim"]
    assert claims and all(c["advisory"] is False for c in claims)


# --- Single-friend visibility (Task 13 coordinator review, round 2) -------
#
# --friend REPLACES the whole roster rather than augmenting default
# discovery (cli.py branches `if args.friend: _specs_from_flags(...) else
# resolve(...)` -- there is no path that layers a --friend override on top
# of discovery). A single --friend flag therefore produces a single-friend
# run, which cannot cross-examine anything (design doc §8.3's "degraded
# single-friend mode"). That reduced guarantee must be visible in run.json
# and report.md, the same rule already applied to every other downgrade
# this runner records (repo-scope, missing lens, degraded signal handling).


def test_single_friend_run_via_friend_flag_records_a_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any("one friend" in note.lower() and "cross-examin" in note.lower()
               for note in meta["downgrades"]), meta["downgrades"]
    report = (runs[0] / "report.md").read_text()
    assert "cross-examin" in report.lower()


def test_two_friend_run_does_not_record_the_single_friend_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert not any("cross-examin" in note.lower() for note in meta["downgrades"]), meta["downgrades"]


# --- C3: one friend's unexpected exception must not end the whole run -----
#
# spawn.run_process previously caught only FileNotFoundError/PermissionError
# from Popen(); any other OSError (E2BIG from an oversized prompt in one
# argv element, ENOEXEC from a broken shim -- see test_spawn.py for the
# unit-level proof of both) escaped the worker thread, was not an AfError,
# and killed the WHOLE run with a raw traceback -- losing every other
# friend's already-succeeded result along with it. Fixed at two layers:
# spawn.run_process now catches OSError broadly, and cli.py's own per-friend
# dispatch wrapper (_run_one, inside cmd_run) catches any OTHER unexpected
# exception too, so nothing short of a deliberate AfError can end a run.


def test_enoexec_friend_does_not_prevent_a_second_friend_from_being_reported(tmp_path):
    """End-to-end version of test_spawn.py's ENOEXEC unit test: a real
    adapter ('codex') resolves, via PATH, to a broken shim (executable bit
    set, but not a valid executable format at all) instead of the real
    codex CLI. This must fail as a clean per-friend result, not take down
    the dispatch of a second, working friend (fake:good)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")

    broken_dir = Path(tempfile.mkdtemp(prefix="af-broken-bin-"))
    broken = broken_dir / "codex"
    broken.write_bytes(b"")  # empty file: no shebang, no recognizable format
    broken.chmod(0o755)

    combined_path = f"{_safe_path_dir()}{os.pathsep}{broken_dir}"
    result = run_af(tmp_path, artifact, "--friend", "codex:ops", "--friend", "fake:good",
                    env_extra={"PATH": combined_path})
    assert result.returncode == 0, result.stderr

    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()
    assert "the guard is missing" in report  # fake:good's finding still came through

    meta = json.loads((runs[0] / "run.json").read_text())
    codex_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("codex"))
    assert "failed" in codex_status.lower()
    fake_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("fake"))
    assert fake_status == "ok"


def test_unexpected_exception_in_one_friends_dispatch_does_not_end_the_run(monkeypatch, tmp_path):
    """Simulates a bug unrelated to process-spawning entirely (something
    spawn.run_process's own OSError handling could never catch, since it
    never even reaches Popen()) by monkeypatching build_argv to raise for
    exactly one friend's cli ('codex'), while a second friend ('fake:good',
    which never calls build_argv at all -- see cli._dispatch) succeeds
    normally. cli.cmd_run is called in-process (not via subprocess) because
    the patch target is an internal cli.py name."""
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("PATH", str(_safe_path_dir()))
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")

    real_build_argv = adapters.build_argv

    def _boom(adapter, spec, prompt_file, schema_file):
        if spec.cli == "codex":
            raise RuntimeError("simulated unexpected bug in adapter wiring")
        return real_build_argv(adapter, spec, prompt_file, schema_file)

    monkeypatch.setattr(cli, "build_argv", _boom)
    parser = cli.build_parser()
    parsed = parser.parse_args([
        "run", str(artifact), "--mode", "report",
        "--out", str(tmp_path / "runs"),
        "--friend", "codex:ops", "--friend", "fake:good",
    ])
    returncode = cli.cmd_run(parsed)
    assert returncode == 0

    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "the guard is missing" in report  # fake:good's finding survived
    meta = json.loads((runs[0] / "run.json").read_text())
    codex_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("codex"))
    assert "unexpected error" in codex_status.lower()
    assert "simulated unexpected bug" in codex_status
    fake_status = next(f["status"] for f in meta["friends"] if f["name"].startswith("fake"))
    assert fake_status == "ok"


def test_oversized_prompt_for_a_non_stdin_adapter_records_an_e2big_downgrade(tmp_path):
    """claude places the whole prompt in one argv element (prompt_mode
    'trailing-arg'); Linux commonly caps a single argv element near 128KB
    (the limit varies by OS -- this test itself may run on macOS), so a
    large artifact can make the real dispatch fail with E2BIG. This is detected
    and recorded up front (see cmd_run's PROMPT_ARGV_WARN_BYTES check), not
    solved -- switching prompt modes is a design change. The downgrade must
    appear regardless of whether the friend actually resolves (claude is not
    on the safe-PATH used by this test, so it also fails for an unrelated
    reason -- 'binary not found' -- but the downgrade is recorded before
    dispatch even starts, independent of that outcome)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n" + ("x" * 150_000) + "\n")
    result = run_af(tmp_path, artifact, "--friend", "claude:ops", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any("E2BIG" in note or "Argument list too long" in note
               for note in meta["downgrades"]), meta["downgrades"]
    assert any("claude" in note for note in meta["downgrades"])


def test_small_prompt_does_not_record_an_e2big_downgrade(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nshort\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert not any("E2BIG" in note for note in meta["downgrades"]), meta["downgrades"]


# --- I1: friend stderr is captured and persisted, not thrown away ---------
#
# SpawnResult.stderr was populated by spawn.run_process but referenced
# nowhere in cli.py: an unauthenticated friend showed up as "failed: exit 1"
# with a 0-byte .raw and no diagnosis anywhere, while troubleshooting.md
# sent the operator to `af doctor`, which only calls shutil.which and never
# probes auth. fake:crash (fake_friend.py) prints "boom" to stderr and
# exits 1 -- a real, if minimal, stand-in for that unauthenticated-friend
# shape.


def test_failed_friends_stderr_is_written_to_its_own_err_file(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:crash", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    err_text = (runs[0] / "round-1" / "fake-crash-0.err").read_text()
    assert err_text.strip() == "boom"


def test_successful_friends_err_file_still_exists_and_is_empty(tmp_path):
    """A stable, always-present file beats one that only sometimes exists --
    an operator grepping round-1/*.err should never have to first check
    which friends failed before knowing which files are there to read."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    err_path = runs[0] / "round-1" / "fake-good-0.err"
    assert err_path.exists()
    assert err_path.read_text() == ""


def test_failed_friends_status_carries_a_short_stderr_tail_and_points_at_the_err_file(tmp_path):
    """The report/run.json status column must show enough to diagnose an
    unauthenticated/misconfigured friend without a second file open --
    'failed: exit 1' alone (the pre-fix behavior) gave no clue at all."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:crash", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    crash_status = next(f["status"] for f in meta["friends"] if f["name"] == "fake-crash-0")
    assert "boom" in crash_status
    assert "fake-crash-0.err" in crash_status
    report = (runs[0] / "report.md").read_text()
    assert "boom" in report
    assert "fake-crash-0.err" in report


def test_successful_friends_status_does_not_carry_a_stderr_tail(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert meta["friends"][0]["status"] == "ok"


# --- Regression 3 (whole-branch re-review): the stderr tail is untrusted
# text on a new path into the report. report._escape_cell alone neutralizes
# only `\`, `|`, and newlines (enough to keep the table structure intact),
# not inline Markdown/HTML (`**bold**`, `[text](url)`, `` `code` ``, a raw
# `<script>`/autolink) -- those still render as real emphasis, a real
# clickable link, or raw HTML once inside a cell. fake:hostile_stderr
# (fake_friend.py) prints exactly that shape to stderr and exits 1.


def test_stderr_tail_strips_inline_markdown_significant_characters():
    """Unit-level proof at the source: cli._stderr_tail must not merely cap
    length, it must strip the characters that make emphasis/links/code
    spans/raw HTML possible in the first place -- report._escape_cell,
    applied later to the whole status string, never touches these."""
    hostile = "auth failed: **please** [login](http://evil.example) `token` <script>alert(1)</script>"
    tail = cli._stderr_tail(hostile)
    for char in "`*_[]<>":
        assert char not in tail, f"{char!r} survived stripping: {tail!r}"
    assert "auth failed" in tail and "login" in tail  # content otherwise preserved


def test_hostile_stderr_does_not_render_as_markdown_in_the_report(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:hostile_stderr",
                    "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    # Isolate exactly the stderr-tail excerpt cli.py inserted (between
    # "(stderr: " and the following "; full text in ...") -- the rest of
    # that table row (the friend name, and the "full text in
    # round-1/<name>.err" reference cli.py itself generates) legitimately
    # contains "_" and other characters that have nothing to do with the
    # sanitizer being tested here.
    status_line = [ln for ln in report.splitlines()
                   if ln.startswith("| fake-hostile_stderr")][0]
    tail_excerpt = status_line.split("(stderr: ", 1)[1].split("; full text in", 1)[0]
    for char in "`*_[]<>":
        assert char not in tail_excerpt, f"{char!r} leaked into: {tail_excerpt!r}"
    assert "auth failed" in tail_excerpt  # the diagnostic content still came through
    assert "the guard is missing" in report  # the second friend's finding still came through


@pytest.mark.skipif(shutil.which("cmark") is None, reason="cmark not installed on this machine")
def test_hostile_stderr_produces_no_link_or_emphasis_under_cmark(tmp_path):
    """report.md legitimately uses **bold** labels ("**Claim:**", etc.) for
    every real finding, which correctly render as <strong> -- a blanket "no
    <strong> anywhere" assertion would be wrong. What must NOT appear is
    the hostile content specifically forming a real link, real emphasis, or
    raw HTML: the "evil.example" URL as a clickable <a href>, "please" (from
    "**please**") wrapped in <strong>, or the literal <script> tag surviving
    unescaped."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:hostile_stderr",
                    "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    report_text = (runs[0] / "report.md").read_text()
    html = subprocess.run(["cmark"], input=report_text, capture_output=True,
                          text=True, check=True).stdout
    assert "evil.example" in html  # the diagnostic text still made it through...
    assert "<a href" not in html  # ...but never as a real, clickable link
    assert "<strong>please" not in html
    assert "<script>alert" not in html

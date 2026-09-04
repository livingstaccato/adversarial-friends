"""End-to-end tests for `afriend run --mode report`: claim corroboration
across the exact-merge ledger (I2), per-friend isolation (doc/repo scope),
capability trust, and cmd_run's threading/signal-safety plumbing.

See tests/e2e_helpers.py for the safe-PATH subprocess harness this file (and
its siblings test_run_end_to_end_basics.py and
test_run_end_to_end_lenses.py) share.
"""

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading

from e2e_helpers import FAKE, REPO, _env, _git_commit, _git_repo, run_af
import pytest

from adversarial_friends import adapters, cli
from adversarial_friends.commands import friends as friends_module

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
    records = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
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
    itself, without needing the in-memory state a live `afriend run`
    process held. This is the ledger-level guarantee; the merged,
    ready-to-read origin list lives in report.md (see
    test_report_shows_corroboration_for_a_claim_multiple_friends_raised)."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:security", "--friend", "fake:ops")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    records = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
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
    copied = next(iter((runs[0] / "artifact").iterdir()))
    assert copied.read_text() == "# spec\nreal content behind a symlink\n"


def _in_repo_symlink_to_outside(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("repository bytes\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "initial")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside repository bytes\n")
    link = repo / "spec.md"
    link.symlink_to(outside)
    return repo, link, outside


@pytest.mark.parametrize("source_change", ["retarget", "remove"])
def test_snapshot_refuses_if_symlink_target_changes_during_capture(
    monkeypatch, tmp_path, source_change
):
    from adversarial_friends import isolation
    from adversarial_friends.errors import UsageError
    from adversarial_friends.snapshots import SnapshotIdentity

    repo = _git_repo(tmp_path / "repo")
    inside = repo / "inside.md"
    inside.write_bytes(b"# stable frozen bytes\n")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"# outside bytes\n")
    source = repo / "spec.md"
    source.symlink_to(inside)
    frozen = tmp_path / "frozen.md"
    frozen.write_bytes(inside.read_bytes())
    digest = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    real_snapshot = isolation.snapshot_commit

    def change_source_after_snapshot(root):
        commit = real_snapshot(root)
        source.unlink()
        if source_change == "retarget":
            source.symlink_to(outside)
        return commit

    monkeypatch.setattr(isolation, "snapshot_commit", change_source_after_snapshot)
    with pytest.raises(UsageError, match=r"repository artifact.*(changed|unavailable)"):
        SnapshotIdentity.create(repo, frozen, digest, source_artifact=source)


def test_in_repo_symlink_to_outside_downgrades_repo_scope_before_dispatch(tmp_path):
    _repo, link, _outside = _in_repo_symlink_to_outside(tmp_path)

    result = run_af(tmp_path, link, "--friend", "fake:cwd_probe:repo")

    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["roster"][0]["scope"] == "doc"
    assert meta["friends"][0]["declared_scope"] == "doc"
    assert any("no repository to snapshot or read" in note for note in meta["downgrades"])


def test_in_repo_symlink_to_outside_records_a_complete_no_repo_identity_for_doc_scope(
    tmp_path,
):
    _repo, link, outside = _in_repo_symlink_to_outside(tmp_path)

    result = run_af(tmp_path, link, "--friend", "fake:good")

    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["snapshot"]["repo_root"] is None
    assert meta["snapshot"]["commit"] is None
    assert meta["snapshot"]["tree"] is None
    assert meta["repo_root"] is None
    assert meta["snapshot_sha"] is None
    assert meta["artifact_hash"] == "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()
    assert meta["roster"][0]["scope"] == "doc"
    assert any("no repository to snapshot or read" in note for note in meta["downgrades"])


def test_in_repo_symlink_to_outside_reconciles_a_mixed_roster_consistently(tmp_path):
    _repo, link, _outside = _in_repo_symlink_to_outside(tmp_path)

    result = run_af(
        tmp_path,
        link,
        "--friend",
        "fake:cwd_probe:repo",
        "--friend",
        "fake:good",
    )

    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    assert {friend["declared_scope"] for friend in meta["friends"]} == {"doc"}
    assert {spec["scope"] for spec in meta["roster"]} == {"doc"}
    matching = [note for note in meta["downgrades"] if "no repository to snapshot or read" in note]
    assert len(matching) == 1


def test_loop_successor_reconciles_scope_when_symlink_retargets_outside(monkeypatch, tmp_path):
    from adversarial_friends.commands import run as run_module

    repo = _git_repo(tmp_path / "repo")
    inside = repo / "inside.md"
    inside.write_text("# inside repository bytes\n")
    link = repo / "spec.md"
    link.symlink_to(inside)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "initial")
    outside = tmp_path / "outside.md"
    outside.write_text("# successor outside bytes\n")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    real_freeze = run_module.freeze_revision
    retargeted = False

    def retarget_then_freeze(*args, **kwargs):
        nonlocal retargeted
        if not retargeted:
            link.unlink()
            link.symlink_to(outside)
            retargeted = True
        return real_freeze(*args, **kwargs)

    monkeypatch.setattr(run_module, "freeze_revision", retarget_then_freeze)
    parsed = cli.build_parser().parse_args(
        [
            "run",
            str(link),
            "--mode",
            "loop",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:judge_uphold_a:repo",
            "--friend",
            "fake:judge_uphold_b",
            "--max-loop-iterations",
            "1",
        ]
    )

    assert cli.cmd_run(parsed) == 11

    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    first, successor = meta["snapshot_history"]
    assert first["commit"] is not None
    assert successor["predecessor"] == first["commit"]
    assert successor["repo_root"] is None
    assert successor["commit"] is None
    assert successor["tree"] is None
    assert meta["snapshot"] == successor
    assert meta["repo_root"] is None
    assert meta["snapshot_sha"] is None
    assert {friend["declared_scope"] for friend in meta["friends"]} == {"doc"}
    assert {spec["scope"] for spec in meta["roster"]} == {"doc"}
    assert any("no repository to snapshot or read" in note for note in meta["downgrades"])


def test_loop_reports_doc_scope_warning_once_when_scope_drops_after_first_iteration(
    monkeypatch, tmp_path
):
    from adversarial_friends.commands import run as run_module

    repo = _git_repo(tmp_path / "repo")
    inside = repo / "inside.md"
    inside.write_text("# inside repository bytes\n")
    link = repo / "spec.md"
    link.symlink_to(inside)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "initial")
    outside = tmp_path / "outside.md"
    outside.write_text("# successor outside bytes\n")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    real_freeze = run_module.freeze_revision
    seen = 0

    def retarget_before_second_round(*args, **kwargs):
        nonlocal seen
        if seen == 1:
            link.unlink()
            link.symlink_to(outside)
        seen += 1
        return real_freeze(*args, **kwargs)

    monkeypatch.setattr(run_module, "freeze_revision", retarget_before_second_round)
    parsed = cli.build_parser().parse_args(
        [
            "run",
            str(link),
            "--mode",
            "loop",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:judge_uphold_a",
            "--friend",
            "fake:judge_uphold_b",
            "--max-loop-iterations",
            "2",
            "--no-progress",
        ]
    )

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert cli.cmd_run(parsed) == 11
    assert stderr.getvalue().count("warning: doc scope only") == 1
    assert "no repository was detected" in stderr.getvalue()


def test_doc_scope_friend_actually_runs_inside_its_own_private_directory(tmp_path):
    """Direct, unambiguous proof that dispatch's `cwd` is the friend's own
    isolation directory -- not, say, Path.cwd() of the `afriend` process
    itself (the brief's own reference `cmd_run` passed exactly that,
    unconditionally, for every friend; wiring isolation in at all is the
    corrected brief's requirement #4). fake:cwd_probe reports its own
    process cwd back as a finding's evidence field, so this asserts on it
    directly rather than inferring wiring indirectly from cleanup side
    effects."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:cwd_probe")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
    reported_cwd = Path(ledger[0]["evidence"])
    assert reported_cwd.name == "fake-cwd_probe-0"  # == cwd_for[spec.name]'s basename
    assert reported_cwd != Path.cwd()
    assert not reported_cwd.exists()  # torn down once afriend run returned


def test_fresh_dispatch_uses_the_frozen_copy_if_live_source_changes_after_freeze(
    monkeypatch, tmp_path
):
    from adversarial_friends import cli
    from adversarial_friends.commands import run as run_module

    artifact = tmp_path / "spec.md"
    original = "# immutable dispatch bytes\n"
    artifact.write_text(original)
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    real_freeze = run_module.freeze_revision

    def freeze_then_edit(*args, **kwargs):
        revision = real_freeze(*args, **kwargs)
        artifact.write_text("# changed after freeze\n")
        return revision

    monkeypatch.setattr(run_module, "freeze_revision", freeze_then_edit)
    parsed = cli.build_parser().parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:good",
            "--keep",
        ]
    )

    assert cli.cmd_run(parsed) == 0

    run_dir = next((tmp_path / "runs").iterdir())
    sandbox_copy = run_dir / "isolation" / "round-1" / "fake-good-0" / artifact.name
    assert sandbox_copy.read_text() == original


def test_repo_scope_friend_gets_a_real_private_worktree(tmp_path):
    """The "fake" cli defaults to doc-scope, so none of the tests above (or
    fake:cwd_probe just above) exercise isolation.snapshot_commit/
    add_worktree through `afriend run` at all -- the exact gap the
    corrected brief warns about ("the brief's cmd_run never calls
    isolation, which would ship Task 9 as dead code"). fake:cwd_probe:repo
    (see cliargs._specs_from_flags) forces scope="repo" for the test-only
    fake cli, so this drives a REAL `git worktree add` off a REAL snapshot
    commit, without needing any actual agent CLI to be present. The
    reported cwd must both be a real git worktree (checked out from the
    snapshot, with the repo's tracked file visible) and be torn down by
    the time `afriend run` returns."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "init")
    artifact = repo / "spec.md"
    artifact.write_text("# spec\n")

    result = run_af(tmp_path, artifact, "--friend", "fake:cwd_probe:repo")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = [
        json.loads(line) for line in (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    ]
    reported_cwd = Path(ledger[0]["evidence"])
    assert reported_cwd.name == "fake-cwd_probe-0"
    assert reported_cwd != repo  # its OWN worktree, not the source repo directly
    assert not reported_cwd.exists()  # torn down by the time afriend run returned

    # The "fake" cli always carries a synthetic, hardcoded capability
    # (readonly=False -- see dispatch._FAKE_CAPABILITY) regardless of the
    # scope requested via the fake:<mode>:repo suffix. run.json's
    # friends[].readonly must reflect THAT, not `spec.scope == "repo"`
    # (which would say True here) -- the one reachable end-to-end case
    # where a re-derivation and the real capability actually diverge, so
    # it is the only place this can be caught without an in-process call.
    meta = json.loads((runs[0] / "run.json").read_text())
    friend = meta["friends"][0]
    assert friend["readonly"] is False
    assert friend["write_protected"] is False
    assert friend["declared_scope"] == "repo"
    assert friend["transport"] == "fake"
    assert friend["os_confined"] is False

    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
    )
    # Only the main working tree remains registered: the friend's private
    # worktree (proven above to have existed while the friend ran, holding
    # the checked-out snapshot) was cleanly removed afterward.
    assert len(worktrees.stdout.strip().splitlines()) == 1


def test_explicit_repo_scope_uses_selected_code_for_an_ignored_artifact_in_another_repo(tmp_path):
    """The artifact's location does not decide repo scope when --repo names one.

    The artifact lives in a different repository and is deliberately ignored,
    so binding it to the selected snapshot would fail. The kept worktree proves
    the friend instead gets selected tracked code only; its private artifact copy
    remains separate.
    """
    repo = _git_repo(tmp_path / "reviewed-repo")
    (repo / ".gitignore").write_text("*.secret\n")
    (repo / "tracked.py").write_text("selected repository code\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "initial")
    (repo / "ignored.secret").write_text("not visible to friends\n")
    artifact_repo = _git_repo(tmp_path / "artifact-repo")
    (artifact_repo / ".gitignore").write_text("*.secret\n")
    artifact = artifact_repo / "spec.secret"
    artifact.write_text("# ignored artifact\n")

    result = run_af(
        tmp_path,
        artifact,
        "--repo",
        str(repo),
        "--friend",
        "fake:cwd_probe:repo",
        "--keep",
    )

    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["repository_scope_mode"] == "explicit"
    assert meta["snapshot"]["repo_root"] == str(repo.resolve())
    assert meta["snapshot"]["artifact_bound_to_snapshot"] is False
    assert meta["snapshot"]["source_path"] is None
    assert meta["repository_scope_audit"] == (
        "repository scope selected explicitly; frozen artifact independently "
        "bound (not Git-blob-bound)."
    )
    assert not any("repository scope selected explicitly" in note for note in meta["downgrades"])
    worktree = run_dir / "isolation" / "round-1" / "fake-cwd_probe-0"
    assert (worktree / "tracked.py").read_text() == "selected repository code\n"
    assert not (worktree / artifact.name).exists()
    assert not (worktree / "ignored.secret").exists()
    assert (run_dir / "artifact" / artifact.name).read_text() == "# ignored artifact\n"


def test_explicit_repo_scope_stays_unbound_when_a_loop_artifact_changes(monkeypatch, tmp_path):
    from adversarial_friends.commands import run as run_module

    repo = _git_repo(tmp_path / "reviewed-repo")
    (repo / ".gitignore").write_text("*.secret\n")
    (repo / "tracked.py").write_text("selected repository code\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=_env())
    _git_commit(repo, "initial")
    artifact = repo / "spec.secret"
    artifact.write_text("# first revision\n")
    monkeypatch.setenv("AF_FAKE_FRIEND", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("AF_NO_HTTP_DISCOVERY", "1")
    real_freeze = run_module.freeze_revision
    changed = False

    def revise_before_freeze(*args, **kwargs):
        nonlocal changed
        if not changed:
            artifact.write_text("# changed revision\n")
            changed = True
        return real_freeze(*args, **kwargs)

    monkeypatch.setattr(run_module, "freeze_revision", revise_before_freeze)
    parsed = cli.build_parser().parse_args(
        [
            "run",
            str(artifact),
            "--repo",
            str(repo),
            "--mode",
            "loop",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:judge_uphold_a:repo",
            "--friend",
            "fake:judge_uphold_b:repo",
            "--max-loop-iterations",
            "1",
        ]
    )

    assert cli.cmd_run(parsed) == 11
    meta = json.loads((next((tmp_path / "runs").iterdir()) / "run.json").read_text())
    assert len(meta["snapshot_history"]) == 2
    assert all(entry["repo_root"] == str(repo.resolve()) for entry in meta["snapshot_history"])
    assert all(entry["artifact_bound_to_snapshot"] is False for entry in meta["snapshot_history"])
    assert all(entry["source_path"] is None for entry in meta["snapshot_history"])


def test_dispatch_never_rederives_capability_from_requested_scope():
    """Requirement: "Use the capability build_argv returns; never
    re-derive it." An adapter with NO readonly_argv at all never gets a
    readonly flag emitted by build_argv -- capability.readonly is False --
    even when scope="repo" is explicitly requested. Neither
    cliargs._specs_from_flags nor roster.resolve's auto-discovery path (the
    only two spec sources cmd_run actually uses) can ever produce this
    combination on their own: both always derive scope FROM
    adapter.readonly_argv, so spec.scope and the true capability never
    diverge through any input reachable via the real --friend/discovery
    CLI surface (opencode -- the one shipped adapter with empty
    readonly_argv -- always resolves to scope="doc" through both paths).
    A subprocess e2e test therefore cannot exercise this rule; this calls
    dispatch._dispatch directly, in-process, with a hand-built Adapter
    (never routed through load_adapters, and with a deliberately
    nonexistent binary name -- this must NEVER risk resolving to any real,
    PATH-installed CLI, in-process calls are not covered by this file's
    safe-PATH subprocess sandboxing at all) to prove the naive
    re-derivation `readonly = spec.scope == "repo"` is NOT what the runner
    actually reports."""
    no_readonly_mode = adapters.Adapter(
        name="norepro",
        binary="af-test-nonexistent-binary-xyz",
        base_argv=[],
        prompt_mode="stdin",
        prompt_flag="",
        readonly_argv=[],
        schema_flag="",
        model_flag="",
        internal_timeout_flag="",
        effort_kind="none",
        external_tools="none",
        external_tool_sources=("test executable",),
    )
    registry = {"norepro": no_readonly_mode}
    spec = adapters.FriendSpec(
        name="norepro-x", cli="norepro", lens="x", model=None, effort=None, scope="repo", timeout=5
    )
    prompt_file = REPO / "tests" / "fake_friend.py"  # any existing text file
    schema_file = prompt_file  # build_argv never reads its contents
    _, capability, outcome, _policy = cli._dispatch(
        spec, REPO, registry, None, prompt_file, schema_file
    )
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

    # roster.resolve is called from commands.friends now, not commands.run:
    # resolving the roster became a separate decision from running it.
    monkeypatch.setattr(friends_module, "resolve", _spy_resolve)
    parser = cli.build_parser()
    parsed = parser.parse_args(["run", str(artifact), "--mode", "report", "--timeout", "37"])
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
    parsed = parser.parse_args(
        [
            "run",
            str(artifact),
            "--mode",
            "report",
            "--out",
            str(tmp_path / "runs"),
            "--friend",
            "fake:good",
        ]
    )
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
    assert "exception" not in outcome, (
        f"cmd_run raised from a background thread: {outcome.get('exception')!r}"
    )
    assert outcome.get("returncode") == 0, outcome

    runs = sorted((tmp_path / "runs").iterdir())
    meta = json.loads((runs[0] / "run.json").read_text())
    assert any("main thread" in note for note in meta["downgrades"]), (
        "run completed but never recorded that signal-based abort was unavailable"
    )

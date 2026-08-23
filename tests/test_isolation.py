import subprocess

import pytest

from adversarial_friends import isolation
from adversarial_friends.errors import AfError


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=root, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (root / "tracked.py").write_text("original\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "init")
    return root


def test_snapshot_includes_untracked_files(repo, tmp_path):
    """git stash create omits untracked files; the snapshot must not."""
    (repo / "brand_new.py").write_text("added but never committed\n")
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / "brand_new.py").read_text() == "added but never committed\n"


def test_snapshot_includes_uncommitted_modifications(repo, tmp_path):
    (repo / "tracked.py").write_text("modified\n")
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / "tracked.py").read_text() == "modified\n"


def test_snapshot_leaves_working_tree_untouched(repo, tmp_path):
    (repo / "tracked.py").write_text("modified\n")
    isolation.snapshot_commit(repo)
    assert (repo / "tracked.py").read_text() == "modified\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "tracked.py" in status  # still dirty; nothing was stashed away


def test_worktree_add_does_not_run_hooks(repo, tmp_path):
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "hook_ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    sha = isolation.snapshot_commit(repo)
    isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert not marker.exists()


def test_each_friend_gets_an_independent_worktree(repo, tmp_path):
    sha = isolation.snapshot_commit(repo)
    first = isolation.add_worktree(repo, sha, tmp_path / "wt-a")
    second = isolation.add_worktree(repo, sha, tmp_path / "wt-b")
    (first / "tracked.py").write_text("friend A scribbled here\n")
    assert (second / "tracked.py").read_text() == "original\n"


def test_doc_scope_dir_contains_only_the_artifact(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    dest = isolation.doc_scope_dir(tmp_path / "docdir", artifact)
    assert [p.name for p in dest.iterdir()] == ["spec.md"]
    assert not (dest / ".git").exists()


def test_doc_scope_dir_raises_instead_of_clobbering_a_pre_existing_file(tmp_path):
    """dest is explicitly allowed to pre-exist; a file already occupying
    dest/<artifact-name> must not be silently overwritten."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# new spec\n")
    docdir = tmp_path / "docdir"
    docdir.mkdir()
    (docdir / "spec.md").write_text("# pre-existing, different content\n")

    with pytest.raises(AfError):
        isolation.doc_scope_dir(docdir, artifact)
    # Untouched -- the pre-existing file's content survives the failed call.
    assert (docdir / "spec.md").read_text() == "# pre-existing, different content\n"


def test_doc_scope_dir_raises_instead_of_nesting_under_a_pre_existing_directory(tmp_path):
    """If dest/<artifact-name> already exists as a directory, shutil.copy2 would nest
    the artifact one level deeper (dest/spec.md/spec.md), silently breaking the 'bare
    directory holding only the artifact' contract. Must raise instead."""
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    docdir = tmp_path / "docdir"
    (docdir / "spec.md").mkdir(parents=True)  # spec.md is a directory, not a file

    with pytest.raises(AfError):
        isolation.doc_scope_dir(docdir, artifact)
    # Nothing was nested underneath it.
    assert list((docdir / "spec.md").iterdir()) == []


# --- Adversarial isolation-breaking attempts (beyond the brief's 6 required tests) ---
#
# The brief's own reference implementation (git rev-parse HEAD, unconditionally) raises
# a raw AfError with git's confusing "ambiguous argument 'HEAD'... Use '--' to separate
# paths from revisions" message for a repo with no commits at all -- correct to fail, but
# a confusing failure. snapshot_commit() was extended to detect an unborn HEAD via
# `git rev-parse --verify -q HEAD` (silent, clean exit 1, no stderr noise -- verified
# directly against this git before writing the fix) and build a parentless root commit in
# that case, rather than surface git's generic message. Genuinely not-a-repo is checked
# separately so it still gets its own clear error.
#
# Fix round: `_require_repo_root` replaced a bare `(repo / ".git").exists()` check.
# That check is true for a FILE, not just a directory -- a linked worktree's or a
# submodule's `.git` is a one-line file pointing elsewhere -- so it let both proceed
# into a temp-index path that no longer exists relative to `repo` (the fix round also
# moved the temp index out of `.git` into its own `tempfile.TemporaryDirectory`,
# closing that failure mode independently). `_require_repo_root` now checks identity
# against `git rev-parse --show-toplevel`, which correctly accepts a worktree root, a
# submodule root, and a plain repo root, and correctly rejects a subdirectory nested
# inside a larger repo -- verified directly against real git worktrees and submodules
# before writing the five tests below.


def test_snapshot_works_on_a_repo_with_no_commits(tmp_path):
    """No HEAD at all (freshly `git init`, nothing committed yet) -- brand new project,
    never reviewed before. This is also the 'HEAD points at an unborn branch' case: an
    unborn branch and 'no commits yet' are the same on-disk state (a symbolic HEAD
    pointing at a ref that does not exist yet)."""
    root = tmp_path / "unborn"
    root.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=root, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (root / "never_committed.py").write_text("brand new project\n")

    sha = isolation.snapshot_commit(root)
    dest = isolation.add_worktree(root, sha, tmp_path / "wt-unborn")
    assert (dest / "never_committed.py").read_text() == "brand new project\n"
    # The operator's repo is still commit-less; nothing was staged into the real index.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    assert "never_committed.py" in status


def test_snapshot_works_on_a_detached_head_repo(tmp_path):
    root = tmp_path / "detached"
    root.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=root, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (root / "f.txt").write_text("v1\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "c1")
    (root / "f.txt").write_text("v2\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "c2")
    run("git", "checkout", "-q", "HEAD~1")
    (root / "f.txt").write_text("v3-uncommitted-while-detached\n")

    sha = isolation.snapshot_commit(root)
    dest = isolation.add_worktree(root, sha, tmp_path / "wt-detached")
    assert (dest / "f.txt").read_text() == "v3-uncommitted-while-detached\n"


# The five inputs `_require_repo_root` must distinguish: repo root, worktree root, and
# submodule root must all succeed; a nested subdirectory and a plain non-repo directory
# must both raise AfError, with messages that say which problem it is.


def test_snapshot_commit_accepts_a_plain_repo_root(repo, tmp_path):
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / "tracked.py").read_text() == "original\n"


def test_snapshot_commit_accepts_a_worktree_root(repo, tmp_path):
    """A linked worktree's `.git` is a FILE (`gitdir: <path>`), not a directory --
    this is the case this very project runs in, and it must work."""
    wt_root = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(wt_root), "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert (wt_root / ".git").is_file()  # confirms this is exercising the FILE case

    sha = isolation.snapshot_commit(wt_root)
    dest = isolation.add_worktree(wt_root, sha, tmp_path / "wt-from-worktree")
    assert (dest / "tracked.py").read_text() == "original\n"


def test_snapshot_commit_accepts_a_submodule_root(repo, tmp_path):
    """A submodule's `.git` is also a FILE (`gitdir: ../.git/modules/<name>`), and its
    own `--show-toplevel` is its own directory, not the superproject's."""
    sub = tmp_path / "sub"
    sub.mkdir()

    def run_sub(*a):
        return subprocess.run(a, cwd=sub, check=True, capture_output=True)

    run_sub("git", "init", "-q")
    run_sub("git", "config", "user.email", "t@example.com")
    run_sub("git", "config", "user.name", "T")
    (sub / "s.txt").write_text("submodule content\n")
    run_sub("git", "add", "-A")
    run_sub("git", "commit", "-q", "-m", "sub c1")

    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "subdir"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add submodule"], cwd=repo, check=True, capture_output=True
    )
    sub_root = repo / "subdir"
    assert (sub_root / ".git").is_file()  # confirms this is exercising the FILE case

    sha = isolation.snapshot_commit(sub_root)
    dest = isolation.add_worktree(sub_root, sha, tmp_path / "wt-from-submodule")
    assert (dest / "s.txt").read_text() == "submodule content\n"


def test_snapshot_commit_rejects_a_non_git_directory(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    (plain / "f.txt").write_text("just files, no .git\n")
    with pytest.raises(AfError, match=r"^not a git repository:"):
        isolation.snapshot_commit(plain)


def test_snapshot_commit_rejects_a_subdir_nested_inside_a_larger_repo(repo):
    """A directory with no .git of its own, but nested inside a real repo, is a
    materially different -- and more plausible -- mistake than a directory with no
    git ancestry anywhere (e.g. passing a monorepo package path instead of the repo
    root). Without an explicit up-front check, git's own upward .git discovery finds
    the *outer* repo, `rev-parse HEAD` and `git add -A` both succeed against it, and
    the snapshot only fails later, deep inside read-tree/write-tree, with a message
    that reads like a filesystem/permissions problem, not 'wrong path'. Verified
    directly before writing this assertion. Message is required to be distinct from
    the plain non-repo case above, per review."""
    subdir = repo / "not_its_own_repo_root"
    subdir.mkdir()
    (subdir / "f.txt").write_text("lives inside a real repo, but isn't a repo root\n")
    with pytest.raises(AfError, match=r"^not the root of its git repository:"):
        isolation.snapshot_commit(subdir)


def test_gitignored_untracked_file_is_excluded_from_the_worktree(repo, tmp_path):
    """Deliberate, per the brief: git add -A honors .gitignore, so ignored files
    (.env and similar) never reach a friend's worktree."""
    (repo / ".gitignore").write_text("*.secret\n")
    (repo / "api.secret").write_text("TOP SECRET\n")
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert not (dest / "api.secret").exists()


def test_add_worktree_fails_cleanly_when_dest_already_has_a_worktree(repo, tmp_path):
    dest = tmp_path / "wt"
    sha = isolation.snapshot_commit(repo)
    isolation.add_worktree(repo, sha, dest)
    with pytest.raises(AfError):
        isolation.add_worktree(repo, sha, dest)


def test_remove_worktree_then_add_worktree_reuses_the_dest_cleanly(repo, tmp_path):
    """A stale worktree registration (dest deleted without going through
    remove_worktree) is a real, if not-code-under-test, footgun -- git refuses to
    re-add over it ('missing but already registered worktree'). Going through
    remove_worktree first is the supported path and must keep working."""
    dest = tmp_path / "wt"
    sha = isolation.snapshot_commit(repo)
    isolation.add_worktree(repo, sha, dest)
    isolation.remove_worktree(repo, dest)
    second = isolation.add_worktree(repo, sha, dest)
    assert (second / "tracked.py").read_text() == "original\n"


def test_symlink_inside_the_repo_pointing_outside_survives_the_snapshot_verbatim(repo, tmp_path):
    """Documents a real, unresolved gap rather than hiding it: git stores and checks
    out symlinks as symlink blobs, not resolved targets, so a symlink already
    committed into the source tree (pointing anywhere, including outside the repo)
    reaches every friend's worktree unchanged. isolation.py does not (and per its
    four-function interface, cannot on its own) strip or rewrite it -- containment
    against a friend that walks such a link is a concern for whatever enforces
    write/read boundaries around a friend's run, not for the snapshot mechanism
    itself. See the task report for the full write-up."""
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("outside the repo\n")
    (repo / "escape_link").symlink_to(outside)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add symlink"], cwd=repo, check=True, capture_output=True
    )

    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    link = dest / "escape_link"
    assert link.is_symlink()
    assert link.readlink() == outside


def test_unicode_and_newline_filenames_survive_the_snapshot(repo, tmp_path):
    unicode_name = "café-日本語-\U0001f600.txt"
    (repo / unicode_name).write_bytes(b"unicode, committed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "unicode filename"], cwd=repo, check=True, capture_output=True
    )
    newline_name = "weird\nname.txt"
    (repo / newline_name).write_bytes(b"newline in filename, never committed\n")

    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / unicode_name).read_bytes() == b"unicode, committed\n"
    assert (dest / newline_name).read_bytes() == b"newline in filename, never committed\n"


def test_snapshot_works_with_no_ambient_git_identity(repo, tmp_path, monkeypatch):
    """`git commit-tree` refuses to build a commit object without an author,
    so a snapshot that inherited the operator's identity failed outright
    ("Author identity unknown") anywhere none was configured -- a fresh
    container, a CI runner, or a machine where identity is set per-repo
    rather than globally. Caught by running the suite in a Linux container,
    where it took out repo-scope isolation entirely.

    Reproduced by pointing every git config source somewhere empty and
    stripping the identity environment variables, so nothing but
    snapshot_commit's own explicit identity remains.
    """
    empty_config = tmp_path / "empty-gitconfig"
    empty_config.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty_config))
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    # Drop the identity the `repo` fixture set locally, so no source has one.
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, capture_output=True)

    (repo / "untracked.py").write_text("no identity anywhere\n")
    sha = isolation.snapshot_commit(repo)

    dest = isolation.add_worktree(repo, sha, tmp_path / "wt-no-identity")
    assert (dest / "untracked.py").read_text() == "no identity anywhere\n"

    author = subprocess.run(
        ["git", "show", "-s", "--format=%an <%ae>", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == f"{isolation.SNAPSHOT_IDENTITY_NAME} <{isolation.SNAPSHOT_IDENTITY_EMAIL}>"

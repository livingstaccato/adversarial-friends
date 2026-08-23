import subprocess
from pathlib import Path

import pytest

from adversarial_friends import isolation


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)
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
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
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

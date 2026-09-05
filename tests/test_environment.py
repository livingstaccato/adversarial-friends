"""Focused repository-scope selection tests."""

from pathlib import Path
import subprocess

import pytest

from afriend.commands.environment import resolve_run_repo
from afriend.errors import UsageError


def _git_repo(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return root


def test_resolve_run_repo_uses_artifact_repository_without_an_explicit_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    artifact = repo / "docs" / "spec.md"
    artifact.parent.mkdir()
    artifact.write_text("# spec\n")

    root, explicit = resolve_run_repo(artifact, None)

    assert root == repo.resolve()
    assert explicit is False


@pytest.mark.parametrize("kind", ["missing", "file", "non_git", "nested", "bare"])
def test_resolve_run_repo_requires_an_explicit_git_worktree_root(tmp_path, kind):
    artifact = tmp_path / "outside.md"
    artifact.write_text("# spec\n")
    repo = _git_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    file = tmp_path / "not-a-directory"
    file.write_text("not a directory\n")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True)
    supplied = {
        "missing": tmp_path / "missing",
        "file": file,
        "non_git": plain,
        "nested": nested,
        "bare": bare,
    }[kind]

    with pytest.raises(UsageError, match="Git worktree root") as raised:
        resolve_run_repo(artifact, str(supplied))

    assert str(supplied) in str(raised.value)


def test_resolve_run_repo_preserves_the_explicit_root_and_selection_marker(tmp_path):
    artifact = tmp_path / "outside.md"
    artifact.write_text("# spec\n")
    repo = _git_repo(tmp_path / "repo")

    root, explicit = resolve_run_repo(artifact, str(repo))

    assert root == repo.resolve()
    assert explicit is True

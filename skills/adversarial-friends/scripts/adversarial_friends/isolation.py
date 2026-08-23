"""Snapshot the repository and hand each friend an isolated copy.

`git stash create` is deliberately not used: its synopsis is
`git stash create [<message>]` with no -u, so it omits untracked files. A
newly added file would then appear in the diff artifact but be missing from
the worktree, forcing every claim about it to 'unverifiable' and blaming the
judge for a broken snapshot. Instead, a temporary index is populated with
`git add -A` (tracked, staged, and untracked-but-not-ignored files alike) and
turned into a commit object with `commit-tree`, without ever touching the
operator's real index or working tree.
"""
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .errors import AfError

# Suppress post-checkout hooks on worktree add. Hooks are not transferred by
# git clone, so this is defense in depth rather than a live hole -- but a
# committed .husky/ plus a previously configured core.hooksPath would
# otherwise execute repository-controlled code on every run.
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                            text=True, env=env)
    if result.returncode != 0:
        raise AfError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _resolve_head(repo: Path) -> str | None:
    """Return HEAD's sha, or None if the branch is unborn (repo has no
    commits yet). Distinct from "not a git repository at all", which is
    checked separately so that case gets its own clear error instead of
    silently being treated as an empty repo.
    """
    result = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"],
                            cwd=str(repo), capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def snapshot_commit(repo: Path) -> str:
    """Create a commit object capturing tracked, staged, and untracked state.

    The working tree is never modified: a temporary index (a fresh file,
    never the repo's real .git/index) is used for the read-tree/add/write-tree
    sequence, so the operator's dirty tree survives the run exactly as it
    was. `git add -A` honors .gitignore, so ignored files (.env and similar)
    are deliberately absent from the snapshot -- friends never see them.

    Works even when `repo` has no commits yet (an unborn HEAD): the snapshot
    becomes a parentless root commit built from whatever untracked,
    non-ignored files are present.
    """
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise AfError(f"not a git repository: {repo}")
    index = repo / ".git" / f"af-snapshot-index-{os.getpid()}-{uuid.uuid4().hex}"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    try:
        head = _resolve_head(repo)
        if head is not None:
            _git(repo, "read-tree", head, env=env)
        _git(repo, "add", "-A", env=env)          # honors .gitignore
        tree = _git(repo, "write-tree", env=env)
        if head is not None:
            return _git(repo, "commit-tree", tree, "-p", head, "-m", "af-snapshot",
                        env=env)
        return _git(repo, "commit-tree", tree, "-m", "af-snapshot", env=env)
    finally:
        index.unlink(missing_ok=True)


def add_worktree(repo: Path, sha: str, dest: Path) -> Path:
    dest = Path(dest)
    _git(Path(repo), *NO_HOOKS, "worktree", "add", "--detach", str(dest), sha)
    return dest


def remove_worktree(repo: Path, dest: Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(dest)],
                   cwd=str(repo), capture_output=True, text=True)


def doc_scope_dir(dest: Path, artifact: Path) -> Path:
    """A bare directory holding only the artifact -- no repository at all.

    This is what makes doc scope containment rather than a prompt request: a
    write-capable friend can write whatever it likes, into a disposable
    directory with no path back to the source tree.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, dest / Path(artifact).name)
    return dest

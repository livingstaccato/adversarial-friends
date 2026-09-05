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
from pathlib import Path
import subprocess
import tempfile

from .errors import AfError
from .secureio import secure_copy, secure_mkdir

# Suppress post-checkout hooks on worktree add. Hooks are not transferred by
# git clone, so this is defense in depth rather than a live hole -- but a
# committed .husky/ plus a previously configured core.hooksPath would
# otherwise execute repository-controlled code on every run.
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]

# Identity stamped on the throwaway snapshot commit (see snapshot_commit).
# Deliberately not the operator's own identity: the object is internal, never
# pushed, and depending on ambient git config made repo scope fail wherever
# none was set.
SNAPSHOT_IDENTITY_NAME = "afriend"
SNAPSHOT_IDENTITY_EMAIL = "af-snapshot@localhost"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise AfError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _resolve_head(repo: Path) -> str | None:
    """Return HEAD's sha, or None if the branch is unborn (repo has no
    commits yet). Distinct from "not a git repository at all", which is
    checked separately so that case gets its own clear error instead of
    silently being treated as an empty repo.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _require_repo_root(repo: Path) -> None:
    """Raise AfError unless `repo` is itself the root of a git repository.

    Checked by identity against `git rev-parse --show-toplevel`, not by
    `.git`'s mere presence: `.git` is a FILE (not a directory) in a linked
    worktree or a submodule, so a bare `.exists()` check can't tell those
    apart from "not a repository at all", and it also can't tell a genuine
    repository root apart from a subdirectory nested inside a larger repo
    (git's own upward `.git` search finds the outer repo either way).
    `--show-toplevel` reports a linked worktree's own root (not the main
    repo it was created from) and a submodule's own root (its own repo
    boundary), so both are correctly accepted; a nested, non-root
    subdirectory reports the *enclosing* repo's root, which does not match
    `repo` and is correctly rejected -- verified directly against real git
    worktrees and submodules before writing this.
    """
    resolved = repo.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(repo), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AfError(f"not a git repository: {repo}")
    toplevel = Path(result.stdout.strip()).resolve()
    if toplevel != resolved:
        raise AfError(f"not the root of its git repository: {repo} (repository root is {toplevel})")


def snapshot_commit(repo: Path) -> str:
    """Create a commit object capturing tracked, staged, and untracked state.

    The working tree is never modified: a temporary index -- a fresh file in
    its own throwaway directory, never anywhere under the repo's own .git --
    is used for the read-tree/add/write-tree sequence, so the operator's
    dirty tree survives the run exactly as it was. Keeping the temp index
    outside .git entirely also means this never has to care whether .git is
    a directory, a file (worktree/submodule), or missing, and can never
    leave a stray lock file inside someone's repository. `git add -A` honors
    .gitignore, so ignored files (.env and similar) are deliberately absent
    from the snapshot -- friends never see them.

    Works even when `repo` has no commits yet (an unborn HEAD): the snapshot
    becomes a parentless root commit built from whatever untracked,
    non-ignored files are present.
    """
    repo = Path(repo)
    _require_repo_root(repo)
    with tempfile.TemporaryDirectory(prefix="af-snapshot-index-") as tmpdir:
        index = Path(tmpdir) / "index"
        # The snapshot is a throwaway commit object that never enters the
        # operator's history and is never pushed, so it must neither require
        # nor borrow their git identity. Without an explicit identity here,
        # `git commit-tree` fails outright with "Author identity unknown"
        # anywhere none is configured -- a fresh container, a CI runner, or a
        # machine where the operator sets identity per-repository rather than
        # globally. That made repo-scope isolation unusable in exactly the
        # environments most likely to run this unattended.
        env = dict(
            os.environ,
            GIT_INDEX_FILE=str(index),
            GIT_AUTHOR_NAME=SNAPSHOT_IDENTITY_NAME,
            GIT_AUTHOR_EMAIL=SNAPSHOT_IDENTITY_EMAIL,
            GIT_COMMITTER_NAME=SNAPSHOT_IDENTITY_NAME,
            GIT_COMMITTER_EMAIL=SNAPSHOT_IDENTITY_EMAIL,
        )
        head = _resolve_head(repo)
        if head is not None:
            _git(repo, "read-tree", head, env=env)
        _git(repo, "add", "-A", env=env)  # honors .gitignore
        tree = _git(repo, "write-tree", env=env)
        if head is not None:
            return _git(repo, "commit-tree", tree, "-p", head, "-m", "af-snapshot", env=env)
        return _git(repo, "commit-tree", tree, "-m", "af-snapshot", env=env)


def add_worktree(repo: Path, sha: str, dest: Path) -> Path:
    dest = Path(dest)
    secure_mkdir(dest, exist_ok=True, root=dest.parent)
    _git(Path(repo), *NO_HOOKS, "worktree", "add", "--detach", str(dest), sha)
    return dest


def remove_worktree(repo: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def doc_scope_dir(dest: Path, artifact: Path) -> Path:
    """A bare directory holding only the artifact -- no repository at all.

    This is what makes doc scope containment rather than a prompt request: a
    write-capable friend can write whatever it likes, into a disposable
    directory with no path back to the source tree.
    """
    dest = Path(dest)
    secure_mkdir(dest, parents=True, exist_ok=True, root=dest.parent)
    target = dest / Path(artifact).name
    if target.exists():
        raise AfError(f"doc scope destination already occupied: {target}")
    secure_copy(artifact, target, root=dest.parent)
    return dest

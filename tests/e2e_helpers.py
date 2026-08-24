"""Shared harness for the `afriend run` end-to-end test files.

Every subprocess launched via these helpers runs under a *constructed* PATH
containing only a symlink to the real `git` binary -- never the real,
inherited PATH. This machine may have codex/claude/agy/opencode installed
for interactive use; inheriting the real PATH would let friend discovery
find them and shell out to real, metered CLIs on every test run. `git` alone
is let through because isolation.py shells out to it for worktree
snapshots, and several tests exercise the repo-scope isolation path on
purpose.

AF_FAKE_FRIEND is the injection point that keeps `--friend fake:<mode>`
entirely off real CLIs: `cmd_run` treats cli == "fake" as a dedicated branch
that runs `$AF_FAKE_FRIEND <mode>` directly, bypassing adapter/build_argv
lookup, capability derivation, and roster resolution entirely (no adapter
named "fake" ever exists in the registry).
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

# The installed console script sits next to whichever interpreter pytest is
# running under -- the real, packaged entry point, not a hand-maintained
# shim. `sys.executable str(AF) ...` at call sites still works: setuptools
# generates console scripts as plain Python files, so passing one as a
# script argument to the interpreter runs it exactly as invoking it
# directly would.
REPO = Path(__file__).resolve().parents[1]
AF = Path(sys.executable).parent / "afriend"
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
        # The safe PATH keeps real agent CLIs out of discovery, but an
        # HTTP friend is found by probing an endpoint, not by PATH -- so a
        # developer running ollama locally would otherwise be enlisted into
        # these runs and tests would pass or fail depending on whether
        # their server happened to be up.
        "AF_NO_HTTP_DISCOVERY": "1",
    }
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    if extra:
        env.update(extra)
    return env


def run_af(tmp_path, artifact, *extra, env_extra=None, mode="report"):
    return subprocess.run(
        [
            sys.executable,
            str(AF),
            "run",
            str(artifact),
            "--mode",
            mode,
            "--out",
            str(tmp_path / "runs"),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=_env(env_extra),
    )


def _git_commit(root: Path, message: str) -> None:
    # -c commit.gpgsign=false: this repo is disposable test scaffolding
    # under tmp_path, not the project's real history -- signing it would
    # only fail because _env() deliberately strips SSH_AUTH_SOCK/GPG_TTY
    # (see the safe-PATH rationale above), on any machine where
    # commit.gpgsign is enabled globally.
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
        env=_env(),
    )


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)

    def run(*a):
        return subprocess.run(a, cwd=root, check=True, capture_output=True, env=_env())

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    return root

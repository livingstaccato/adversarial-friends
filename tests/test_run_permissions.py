import os
from pathlib import Path
import stat
import subprocess
import sys

from e2e_helpers import AF, _env
import pytest

from adversarial_friends.errors import UsageError
from adversarial_friends.runstore import RunStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_umask_022_run_tree_keeps_directories_private_and_files_secret(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# private design\n", encoding="utf-8")

    def set_umask() -> None:
        os.umask(0o022)

    result = subprocess.run(
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
            "fake:good",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        preexec_fn=set_umask,
    )
    assert result.returncode == 0, result.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            continue
        expected = 0o700 if path.is_dir() else 0o600
        assert _mode(path) == expected, f"{path.relative_to(run_dir)} had {_mode(path):o}"


def test_resume_refuses_a_symlinked_run_root_without_touching_its_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    sentinel.chmod(0o644)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UsageError, match="no such run directory"):
        RunStore(runs, "run-linked", resume=True)

    assert _mode(outside) == 0o755
    assert _mode(sentinel) == 0o644
    assert sentinel.read_text(encoding="utf-8") == "unchanged"

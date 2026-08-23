from pathlib import Path
import subprocess
import sys

# The installed console script sits next to whichever interpreter pytest is
# running under (venv bin/, however that venv was created) -- this is the
# real, packaged entry point (`[project.scripts] afriend = ...`), not a
# hand-maintained shim, so a passing test here proves the actual install
# works, not just that some file happens to exist on disk.
AF = Path(sys.executable).parent / "afriend"


def test_af_reports_version():
    result = subprocess.run([str(AF), "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip().startswith("afriend ")


def test_unknown_subcommand_exits_2():
    result = subprocess.run([str(AF), "nonsense"], capture_output=True, text=True)
    assert result.returncode == 2
    # Distinguishes parser rejection from `python3 <missing-file>`, which also exits 2.
    assert "invalid choice" in result.stderr

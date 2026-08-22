import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AF = REPO / "skills" / "adversarial-friends" / "scripts" / "af"


def test_af_reports_version():
    result = subprocess.run([sys.executable, str(AF), "--version"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip().startswith("af ")


def test_unknown_subcommand_exits_2():
    result = subprocess.run([sys.executable, str(AF), "nonsense"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    # Distinguishes parser rejection from `python3 <missing-file>`, which also exits 2.
    assert "invalid choice" in result.stderr

from pathlib import Path
import subprocess
import sys

# The installed console script sits next to whichever interpreter pytest is
# running under (venv bin/, however that venv was created) -- this is the
# real, packaged entry point (`[project.scripts] afriend = ...`), not a
# hand-maintained shim, so a passing test here proves the actual install
# works, not just that some file happens to exist on disk.
AF = Path(sys.executable).parent / "afriend"
REPO = Path(__file__).resolve().parents[1]


def test_af_reports_version():
    """The number, not just the prefix. Asserting only that it starts with
    "afriend " is what let the reported version sit two releases behind
    VERSION: the installed 0.1.2 console script printed 0.1.0 and this
    passed."""
    expected = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    result = subprocess.run([str(AF), "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == f"afriend {expected}", result.stdout


def test_unknown_subcommand_exits_2():
    result = subprocess.run([str(AF), "nonsense"], capture_output=True, text=True)
    assert result.returncode == 2
    # Distinguishes parser rejection from `python3 <missing-file>`, which also exits 2.
    assert "invalid choice" in result.stderr


def test_the_reported_version_matches_the_file_that_drives_the_build():
    """`afriend --version` said 0.1.0 from a 0.1.2 wheel: the string was
    hardcoded in __init__.py and had drifted two releases while VERSION,
    the plugin manifests and the wheel metadata all agreed. It is derived
    now -- from VERSION in a checkout, from distribution metadata when
    installed -- and check_version_sync.py compares it too."""
    from adversarial_friends import __version__

    expected = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == expected

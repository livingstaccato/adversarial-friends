import json
import os
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


def test_status_subcommand_is_available(tmp_path):
    result = subprocess.run(
        [str(AF), "status", "missing-run", "--out", str(tmp_path / "runs")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "no such run" in result.stderr


def test_the_reported_version_matches_the_file_that_drives_the_build():
    """`afriend --version` said 0.1.0 from a 0.1.2 wheel: the string was
    hardcoded in __init__.py and had drifted two releases while VERSION,
    the plugin manifests and the wheel metadata all agreed. It is derived
    now -- from VERSION in a checkout, from distribution metadata when
    installed -- and check_version_sync.py compares it too."""
    from afriend import __version__

    expected = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == expected


def run_afriend(*args: str, config_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)
    return subprocess.run([str(AF), *args], capture_output=True, text=True, env=env)


def test_providers_cli_updates_and_lists_json(tmp_path):
    disabled = run_afriend("providers", "disable", "ollama", config_home=tmp_path)
    assert disabled.returncode == 0, disabled.stderr
    modeled = run_afriend("providers", "set-model", "ollama", "qwen3:0.6b", config_home=tmp_path)
    assert modeled.returncode == 0, modeled.stderr

    listed = run_afriend("providers", "list", "--json", config_home=tmp_path)
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["version"] == 1
    assert payload["providers"]["ollama"] == {"enabled": False, "model": "qwen3:0.6b"}
    assert payload["providers"]["codex"] == {"enabled": True, "model": None}
    assert listed.stdout == json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_providers_cli_human_list_is_clear_and_sorted(tmp_path):
    listed = run_afriend("providers", "list", config_home=tmp_path)
    assert listed.returncode == 0, listed.stderr
    lines = listed.stdout.splitlines()
    assert lines == sorted(lines)
    assert "codex\tenabled\tmodel: default" in lines
    assert "ollama\tenabled\tmodel: default" in lines


def test_providers_cli_reports_unknown_provider_as_usage_error(tmp_path):
    result = run_afriend("providers", "disable", "not-real", config_home=tmp_path)
    assert result.returncode == 2
    assert "afriend:" in result.stderr
    assert "not-real" in result.stderr

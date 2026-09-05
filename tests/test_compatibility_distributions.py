from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_compatibility_distributions_are_exact_metadata_only_aliases() -> None:
    for name in ("adversarial-friends", "afriends"):
        project = ROOT / "compatibility-distributions" / name
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["name"] == name
        assert data["project"]["version"] == VERSION
        assert data["project"]["dependencies"] == [f"afriend=={VERSION}"]
        assert data["tool"]["setuptools"]["packages"] == []
        assert "scripts" not in data["project"]
        assert not any(path.suffix == ".py" for path in project.rglob("*.py"))


def test_release_verifier_builds_and_smokes_all_three_distributions() -> None:
    result = subprocess.run(
        ["bash", "ci/verify_release_distributions.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok: verified afriend, adversarial-friends, and afriends" in result.stdout

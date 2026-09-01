"""End-to-end coverage for provider-scoped external tool authority."""

import json

from e2e_helpers import run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n")
    return path


def _run_json(tmp_path):
    run_dir = sorted((tmp_path / "runs").iterdir())[0]
    return json.loads((run_dir / "run.json").read_text())


def test_allow_external_tools_is_required_value_and_repeatable(tmp_path):
    missing = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--allow-external-tools",
    )
    assert missing.returncode == 2
    assert "expected one argument" in missing.stderr
    assert not (tmp_path / "runs").exists()

    allowed = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--allow-external-tools=codex",
        "--allow-external-tools=agy",
    )
    assert allowed.returncode == 0, allowed.stderr
    assert _run_json(tmp_path)["external_tool_grants"] == ["agy", "codex"]


def test_unknown_or_duplicate_grants_fail_before_run_directory(tmp_path):
    for grants in (("unknown",), ("agy", "agy"), ("*", "agy")):
        run_root = tmp_path / "runs"
        result = run_af(
            tmp_path,
            _artifact(tmp_path),
            "--friend",
            "fake:good",
            *(f"--allow-external-tools={grant}" for grant in grants),
        )
        assert result.returncode == 2
        assert "--allow-external-tools" in result.stderr
        assert not run_root.exists()

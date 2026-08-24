"""End-to-end coverage for §17's remaining flags."""

import json
import subprocess
import sys

from e2e_helpers import AF, _env, run_af


def _artifact(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# spec\n")
    return path


def _run_dir(tmp_path):
    return sorted((tmp_path / "runs").iterdir())[0]


def _run_json(tmp_path):
    return json.loads((_run_dir(tmp_path) / "run.json").read_text())


# --- --json ----------------------------------------------------------------


def test_run_json_prints_the_metadata(tmp_path):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--json")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["mode"] == "report"
    assert parsed["friends"]


def test_without_json_the_path_is_still_what_is_printed(tmp_path):
    """A shell pipeline wants the path; --json is for a caller that would
    otherwise have to go read run.json itself."""
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good")
    assert result.stdout.strip() == str(_run_dir(tmp_path))


def test_doctor_json_is_machine_readable(tmp_path):
    result = subprocess.run(
        [sys.executable, str(AF), "doctor", "--json"], capture_output=True, text=True, env=_env()
    )
    parsed = json.loads(result.stdout)
    assert isinstance(parsed["friends"], list)
    assert all("auth_classifiable" in row for row in parsed["friends"])


# --- --model / --effort (§10.1 layer 4) ------------------------------------


def test_model_and_effort_override_everything(tmp_path):
    """Invocation flags are §10.1's strongest layer -- they outrank a roster
    entry's own values, which is what makes them layer 4 rather than
    another way of spelling the same thing."""
    roster = tmp_path / "roster.toml"
    roster.write_text(
        '[[friend]]\nname = "codex-ops"\ncli = "codex"\nlens = "ops"\n'
        'model = "from-roster"\neffort = "low"\n'
    )
    run_af(
        tmp_path,
        _artifact(tmp_path),
        "--roster",
        str(roster),
        "--model",
        "from-flag",
        "--effort",
        "high",
    )
    friend = _run_json(tmp_path)["friends"][0]
    assert friend["model"] == "from-flag"
    assert friend["effort"] == "high"


# --- --lens / --max-friends ------------------------------------------------


def test_an_unknown_lens_is_refused(tmp_path):
    """A typo would otherwise quietly shrink the run to whichever lenses
    happened to match."""
    result = run_af(tmp_path, _artifact(tmp_path), "--lens", "not-a-lens")
    assert result.returncode == 2
    assert "unknown lens" in result.stderr


def test_max_friends_caps_and_says_so(tmp_path):
    """A silently shortened roster is a run with fewer independent judges
    than the operator thinks it has."""
    roster = tmp_path / "roster.toml"
    roster.write_text(
        '[[friend]]\nname = "a"\ncli = "codex"\nlens = "ops"\n'
        '[[friend]]\nname = "b"\ncli = "claude"\nlens = "security"\n'
    )
    run_af(tmp_path, _artifact(tmp_path), "--roster", str(roster), "--max-friends", "1")
    meta = _run_json(tmp_path)
    assert len(meta["friends"]) == 1
    assert any("--max-friends=1 dropped" in d for d in meta["downgrades"])


# --- --keep ----------------------------------------------------------------


def test_keep_leaves_the_isolation_directory_behind(tmp_path):
    """§12.4. A "kept" worktree at a path that no longer exists would be
    worse than not keeping it, so isolation moves into the run directory,
    which persists."""
    run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:cwd_probe", "--keep")
    kept = _run_dir(tmp_path) / "isolation" / "round-1"
    assert kept.is_dir()
    assert any(kept.iterdir())


def test_without_keep_nothing_survives(tmp_path):
    run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:cwd_probe")
    assert not (_run_dir(tmp_path) / "isolation").exists()


# --- --unsafe-extra-args (§13) ---------------------------------------------


def test_unsafe_extra_args_requires_the_acknowledgement(tmp_path):
    result = run_af(
        tmp_path, _artifact(tmp_path), "--friend", "fake:good", "--unsafe-extra-args", "--verbose"
    )
    assert result.returncode == 2
    assert "--i-accept-unsandboxed" in result.stderr


def test_a_denied_flag_is_refused_even_with_the_acknowledgement(tmp_path):
    """An escape hatch for "I need one more option" is not an escape hatch
    for "run with no guardrails at all"."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        # The = form: argparse only accepts a dash-leading value when it
        # contains a space, so this is the spelling that always works.
        "--unsafe-extra-args=--yolo",
        "--i-accept-unsandboxed",
    )
    assert result.returncode == 2
    assert "disables approval" in result.stderr


def test_accepted_extra_args_are_recorded_as_a_downgrade(tmp_path):
    """A run carrying unvalidated flags has weaker guarantees than its
    friend table implies, so the report has to say so."""
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--unsafe-extra-args",
        "--verbose --colour never",
        "--i-accept-unsandboxed",
    )
    assert result.returncode == 0, result.stderr
    downgrades = " ".join(_run_json(tmp_path)["downgrades"])
    assert "--unsafe-extra-args" in downgrades
    assert "read-only is reported as False" in downgrades


# --- doctor --gc -----------------------------------------------------------


def test_gc_removes_an_abandoned_run(tmp_path):
    """A run directory with no report.md means the process died before
    finishing -- every path out of cmd_run writes one."""
    runs = tmp_path / "runs"
    abandoned = runs / "run-20260101T000000-deadbeef"
    abandoned.mkdir(parents=True)
    (abandoned / "claims.jsonl").write_text("")
    result = subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert not abandoned.exists(), result.stderr


def test_gc_keeps_a_finished_run(tmp_path):
    runs = tmp_path / "runs"
    finished = runs / "run-20260101T000000-cafe0000"
    finished.mkdir(parents=True)
    (finished / "report.md").write_text("# report\n")
    subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert finished.exists()


def test_gc_keeps_a_run_halted_for_the_orchestrator(tmp_path):
    """It is waiting for a RESPONSE.json, not abandoned -- and the halt path
    writes a report precisely so it survives this."""
    runs = tmp_path / "runs"
    run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--merge",
        "orchestrator",
    )
    halted = _run_dir(tmp_path)
    subprocess.run(
        [sys.executable, str(AF), "doctor", "--gc", "--out", str(runs)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert halted.exists()

"""Effective review-profile resolution for fresh and resumed runs."""

from pathlib import Path

import pytest

from adversarial_friends import sessionconfig
from adversarial_friends.cliargs import build_parser
from adversarial_friends.commands.run import cmd_run
from adversarial_friends.commands.runmeta import validate_run_args
from adversarial_friends.errors import UsageError


def _artifact(tmp_path: Path) -> Path:
    artifact = tmp_path / "spec.md"
    artifact.write_text("# Spec\n", encoding="utf-8")
    return artifact


def test_missing_session_preference_uses_quick_and_report(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    args = build_parser().parse_args(["run", str(_artifact(tmp_path))])

    resolved, _ = validate_run_args(args)

    assert resolved.profile == "quick"
    assert resolved.mode == "report"


def test_session_default_supplies_mode_when_mode_was_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sessionconfig.set_default("balanced")
    args = build_parser().parse_args(["run", str(_artifact(tmp_path))])

    resolved, _ = validate_run_args(args)

    assert resolved.profile == "balanced"
    assert resolved.mode == "crossexam"


def test_explicit_profile_overrides_session_default_but_explicit_mode_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sessionconfig.set_default("quick")
    args = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--profile", "thorough", "--mode", "gate"]
    )

    resolved, _ = validate_run_args(args)

    assert resolved.profile == "thorough"
    assert resolved.mode == "gate"


def test_unknown_profile_is_rejected_before_a_run_directory_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    out = tmp_path / "runs"
    args = build_parser().parse_args(
        ["run", str(_artifact(tmp_path)), "--profile", "not-a-profile", "--out", str(out)]
    )

    with pytest.raises(UsageError, match="unknown review profile 'not-a-profile'"):
        cmd_run(args)

    assert not out.exists()

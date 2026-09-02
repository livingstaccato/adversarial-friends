import argparse
import json
from pathlib import Path

import pytest

from adversarial_friends.commands.runmeta import _restore_args
from adversarial_friends.errors import UsageError


def _resume_args(run_dir: Path, **overrides):
    values = dict(
        resume=str(run_dir),
        out=None,
        artifact=None,
        friend=[],
        allow_external_tools=[],
        allow_unsandboxed_friend=False,
        unsafe_extra_args=None,
        i_accept_unsandboxed=False,
        pass_env=[],
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _write_resume_fixture(
    tmp_path: Path,
    invocation: dict[str, object],
    roster: list[dict[str, object]] | None = None,
) -> Path:
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    artifact = str(tmp_path / "spec.md")
    snapshot = {
        "repo_root": None,
        "commit": None,
        "tree": None,
        "artifact_path": artifact,
        "artifact_hash": "sha256:" + "0" * 64,
        "predecessor": None,
    }
    meta = {
        "schema_version": 2,
        "lifecycle_state": "waiting-for-orchestrator",
        "invocation": {"artifact": artifact, "friend": [], **invocation},
        "roster": roster or [],
        "snapshot": snapshot,
        "snapshot_history": [snapshot],
    }
    (tmp_path / "spec.md").write_text("# spec\n")
    round_dir = run_dir / "round-1"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(json.dumps({"question": "merge"}))
    (run_dir / "run.json").write_text(json.dumps(meta))
    return run_dir


def test_unknown_saved_external_tool_grant_is_rejected(tmp_path):
    run_dir = _write_resume_fixture(tmp_path, {"allow_external_tools": ["future"]})
    with pytest.raises(UsageError, match="unknown --allow-external-tools"):
        _restore_args(_resume_args(run_dir))


def test_unknown_current_external_tool_grant_is_rejected(tmp_path):
    run_dir = _write_resume_fixture(tmp_path, {"allow_external_tools": []})
    with pytest.raises(UsageError, match="unknown --allow-external-tools"):
        _restore_args(_resume_args(run_dir, allow_external_tools=["future"]))

"""Shared fixtures and filesystem setup for run metadata migration tests."""

import json
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _resume_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
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


def _run_dir(tmp_path: Path, meta: dict[str, object]) -> Path:
    run_dir = tmp_path / "run-v020"
    run_dir.mkdir()
    round_dir = run_dir / "round-1"
    round_dir.mkdir()
    (round_dir / "REQUEST.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_dir.name,
                "round": 1,
                "question": "merge",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def _resume_meta() -> dict[str, object]:
    meta = load_fixture("run_meta_v020_halted.json")
    meta["invocation"].update(
        {
            "allow_unsandboxed_friend": False,
            "i_accept_unsandboxed": False,
            "unsafe_extra_args": None,
            "pass_env": [],
        }
    )
    return meta

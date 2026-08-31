"""Contract tests for the repository Python file line cap."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_check_max_loc():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_max_loc.py"
    spec = importlib.util.spec_from_file_location("check_max_loc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_file_cap_is_777():
    module = load_check_max_loc()
    assert module.MAX_LINES == 777


def test_violation_boundary_is_strictly_above_777(tmp_path):
    module = load_check_max_loc()
    allowed = tmp_path / "allowed.py"
    rejected = tmp_path / "rejected.py"
    allowed.write_text("x\n" * 777)
    rejected.write_text("x\n" * 778)
    assert module.find_violations([tmp_path]) == [(str(rejected), 778)]

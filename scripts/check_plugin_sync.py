#!/usr/bin/env python3
"""Verify or safely materialize the composite Adversarial Friends plugin skills."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "adversarial_friends" / "assets"
PLUGIN_ROOT = REPO / "plugins" / "adversarial-friends"
SKILLS = PLUGIN_ROOT / "skills"


def project_tree(source: Path, destination: Path) -> dict[Path, bytes]:
    """Return source bytes mapped under a relative plugin destination."""
    return {
        destination / path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and path.name != "__init__.py" and "__pycache__" not in path.parts
    }


def expected_plugin_files() -> dict[Path, bytes]:
    """Build the only allowed file map beneath the plugin's skills directory."""
    expected = project_tree(ASSETS / "entrypoints", Path())
    expected |= project_tree(ASSETS / "adapters", Path("afriend/adapters"))
    expected |= project_tree(ASSETS / "harnesses", Path("afriend/harnesses"))
    expected |= project_tree(ASSETS / "lenses", Path("afriend/lenses"))
    return expected


def collect(root: Path) -> dict[Path, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != "__init__.py" and "__pycache__" not in path.parts
    }


def report_difference(actual: dict[Path, bytes], expected: dict[Path, bytes]) -> int:
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    differing = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )
    if not (missing or unexpected or differing):
        return 0
    print("plugin skills are out of sync:", file=sys.stderr)
    for heading, paths in (
        ("missing", missing),
        ("unexpected", unexpected),
        ("content differs", differing),
    ):
        if paths:
            print(f"  {heading}:", file=sys.stderr)
            for path in paths:
                print(f"    {path}", file=sys.stderr)
    return 1


def is_inside_plugin(path: Path) -> bool:
    try:
        path.resolve().relative_to(PLUGIN_ROOT.resolve())
    except ValueError:
        return False
    return True


def copy_expected(expected: dict[Path, bytes]) -> int:
    """Stage, validate, then replace exactly the resolved plugin skills directory."""
    plugin_root = PLUGIN_ROOT.resolve()
    skills = SKILLS.resolve()
    if not plugin_root.is_dir() or not is_inside_plugin(skills) or skills.parent != plugin_root:
        print("error: resolved skills target is outside the plugin root", file=sys.stderr)
        return 2
    stage_parent = Path(tempfile.mkdtemp(prefix=".skills-stage-", dir=plugin_root))
    try:
        staged_skills = stage_parent / "skills"
        for relative, payload in expected.items():
            target = staged_skills / relative
            if not is_inside_plugin(target) or not target.resolve().is_relative_to(
                stage_parent.resolve()
            ):
                print(f"error: unsafe projected path: {relative}", file=sys.stderr)
                return 2
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if report_difference(collect(staged_skills), expected):
            print("error: staged plugin projection failed validation", file=sys.stderr)
            return 2
        if skills.exists():
            shutil.rmtree(skills)
        staged_skills.replace(skills)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
    return report_difference(collect(SKILLS), expected)


def main(argv: list[str]) -> int:
    if argv not in ([], ["--copy"]):
        print("usage: check_plugin_sync.py [--copy]", file=sys.stderr)
        return 2
    expected = expected_plugin_files()
    if argv == ["--copy"]:
        return copy_expected(expected)
    return report_difference(collect(SKILLS), expected)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

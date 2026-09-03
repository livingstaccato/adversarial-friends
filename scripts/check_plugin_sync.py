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
    """Return source bytes mapped under a relative plugin destination.

    Canonical packaging data must be regular files and directories: following a
    symlink here could copy material outside the reviewed source tree.
    """
    if source.is_symlink():
        raise ValueError(f"canonical source is a symlink: {source}")
    files: dict[Path, bytes] = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"canonical source contains a symlink: {path}")
        if path.is_file() and path.name != "__init__.py" and "__pycache__" not in path.parts:
            files[destination / path.relative_to(source)] = path.read_bytes()
    return files


def expected_plugin_files() -> dict[Path, bytes]:
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
        if path.is_file() and not path.is_symlink() and path.name != "__init__.py"
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
            print(*(f"    {path}" for path in paths), sep="\n", file=sys.stderr)
    return 1


def copy_expected(expected: dict[Path, bytes]) -> int:
    """Stage and validate, then replace skills with rollback on rename failure."""
    plugin_root = PLUGIN_ROOT.resolve()
    if not plugin_root.is_dir() or SKILLS.is_symlink():
        print("error: plugin skills target is unsafe", file=sys.stderr)
        return 2
    if SKILLS.exists() and not SKILLS.is_dir():
        print("error: plugin skills target is not a directory", file=sys.stderr)
        return 2
    stage_parent = Path(tempfile.mkdtemp(prefix=".skills-stage-", dir=plugin_root))
    backup: Path | None = None
    try:
        staged = stage_parent / "skills"
        for relative, payload in expected.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if report_difference(collect(staged), expected):
            return 2
        if SKILLS.exists():
            backup = Path(tempfile.mkdtemp(prefix=".skills-backup-", dir=plugin_root))
            backup.rmdir()
            SKILLS.replace(backup)
        try:
            staged.replace(SKILLS)
        except OSError:
            if backup is not None and backup.exists() and not SKILLS.exists():
                backup.replace(SKILLS)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return report_difference(collect(SKILLS), expected)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def main(argv: list[str]) -> int:
    if argv not in ([], ["--copy"]):
        print("usage: check_plugin_sync.py [--copy]", file=sys.stderr)
        return 2
    try:
        expected = expected_plugin_files()
        return copy_expected(expected) if argv else report_difference(collect(SKILLS), expected)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

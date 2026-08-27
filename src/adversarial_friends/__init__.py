"""The version `afriend --version` reports.

Hardcoded until 0.1.2, where it still said 0.1.0: the string had drifted two
releases behind `VERSION` because nothing compared them. `--version` is the
first thing anyone runs after installing, so it is derived rather than
written down, and `scripts/check_version_sync.py` now checks it alongside
the plugin manifests.

A source checkout answers from `VERSION` at the repository root -- the file
that drives the build -- so a bump is live before anything is reinstalled.
An installed package has no repository above it and answers from its own
distribution metadata, which setuptools filled in from that same file.
"""

from importlib.metadata import PackageNotFoundError, version as _installed_version
from pathlib import Path


def _detect_version() -> str:
    # The repository root is identified by pyproject.toml sitting beside
    # VERSION, so an installed copy under site-packages cannot mistake some
    # unrelated file two directories up for the project's own.
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "VERSION").is_file():
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        return _installed_version("adversarial-friends")
    except PackageNotFoundError:
        # Importable but neither installed nor in a checkout: say so rather
        # than reporting a version that was never built.
        return "0+unknown"


__version__ = _detect_version()

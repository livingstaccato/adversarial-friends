"""Resolve the package's bundled runtime adapter and lens assets.

Assets ship as package-data under `afriend.assets` so they are
found the same way whether this package runs from an editable install, a
built wheel, or (via `python -m afriend`) an unpacked checkout.
`importlib.resources.files()` is the standard way to address that; wrapping
the result in `Path(...)` assumes a filesystem install rather than an
importer serving files out of a zip, which matches every install path this
project supports (wheel, sdist, editable, `pip install -e .`, `uv tool
install`) but would need revisiting if this ever shipped as a zipapp.

Entrypoint skills and their references are distribution payload for plugin
loaders; they are not runtime lookups through this module.
"""

from importlib.resources import files
from pathlib import Path


def assets_root() -> Path:
    """Return the filesystem path to the package's bundled `assets/` directory."""
    return Path(str(files("afriend.assets")))


ADAPTER_DIR = assets_root() / "adapters"
LENS_DIR = assets_root() / "lenses"

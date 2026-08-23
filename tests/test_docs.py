"""Tests for the repository's public-facing documentation and brand assets.

These are not behavioral tests of the runner; they verify that the
documentation tree is present, internally consistent, and safe to render
outside the repository (PyPI, GitHub's raw viewer, a mirrored copy, etc).
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_readme_leads_with_the_banner():
    first = REPO.joinpath("README.md").read_text().splitlines()[0]
    assert first.startswith("![adversarial-friends]")


def test_all_brand_sizes_exist():
    brand = REPO / "docs" / "images" / "brand"
    banner = brand / "adversarial-friends-banner.png"
    assert banner.stat().st_size > 100_000
    # Ceiling: a full-resolution PNG of this illustration is several MB, which
    # does not belong in git history. Regenerate at 1024 if this trips.
    assert banner.stat().st_size < 4_000_000, "banner too large for the repo"
    for size in (128, 256, 512):
        derived = brand / f"adversarial-friends-logo-{size}.png"
        assert derived.exists(), derived
        assert derived.stat().st_size > 0


def test_derived_sizes_have_the_right_dimensions():
    """PNG dimensions live at a fixed offset in the IHDR chunk — no dependency needed."""
    import struct
    brand = REPO / "docs" / "images" / "brand"
    for size in (128, 256, 512):
        data = (brand / f"adversarial-friends-logo-{size}.png").read_bytes()[:24]
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (size, size)


def test_readme_image_links_are_absolute_github_urls():
    """Relative paths break on PyPI and anywhere the README is mirrored."""
    import re
    text = REPO.joinpath("README.md").read_text()
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        assert target.startswith("https://raw.githubusercontent.com/"), target


def test_docs_index_links_only_to_existing_files():
    index = REPO / "docs" / "README.md"
    import re
    for target in re.findall(r"\]\(([^)#][^)]*)\)", index.read_text()):
        if target.startswith("http"):
            continue
        assert (index.parent / target).exists(), target


def test_evals_file_is_valid_and_has_cases():
    data = json.loads((REPO / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "adversarial-friends"
    assert len(data["evals"]) >= 3
    assert all("prompt" in e and "expected_output" in e for e in data["evals"])

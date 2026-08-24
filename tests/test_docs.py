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


def test_readme_image_links_are_absolute():
    """Relative paths break on PyPI and anywhere the README is mirrored.

    The guarantee is that no image resolves relative to the repository tree.
    Absolute https URLs all satisfy that, so third-party badge hosts are
    fine; what must never appear is `![x](docs/...)`.
    """
    import re

    text = REPO.joinpath("README.md").read_text()
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        assert target.startswith("https://"), target


def test_readme_repo_hosted_images_use_raw_githubusercontent():
    """Images served out of this repository (brand assets, rendered
    diagrams) must go through raw.githubusercontent.com specifically --
    a github.com/blob URL serves an HTML page, not an image."""
    import re

    text = REPO.joinpath("README.md").read_text()
    repo_hosted = [
        t
        for t in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        if "adversarial-friends" in t and "shields.io" not in t
    ]
    assert repo_hosted, "expected the README to embed repo-hosted images"
    for target in repo_hosted:
        assert target.startswith(
            "https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/"
        ), target


def test_readme_embedded_diagrams_exist_on_disk():
    """The README embeds rendered PNGs by absolute URL, so a missing or
    unrendered file is invisible locally and 404s only once pushed."""
    import re

    text = REPO.joinpath("README.md").read_text()
    prefix = "https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/"
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        if not target.startswith(prefix):
            continue
        assert (REPO / target[len(prefix) :]).exists(), target


def test_every_puml_source_has_committed_png_and_svg_renders():
    """`make diagrams` output is committed because the README references it
    by URL. A .puml edited without re-rendering ships a stale image."""
    sources = sorted((REPO / "docs" / "architecture").glob("*.puml"))
    assert sources, "expected architecture diagram sources"
    for src in sources:
        assert src.with_suffix(".png").exists(), f"missing PNG render for {src.name}"
        assert src.with_suffix(".svg").exists(), f"missing SVG render for {src.name}"


def _svg_visible_text(svg_path: Path) -> str:
    """Return an SVG's rendered text with XML entities resolved.

    PlantUML emits every space inside <text> as `&#160;`, which unescapes to
    U+00A0 (a non-breaking space) rather than a plain space -- so both the
    raw markup and a naively-unescaped copy fail to match a phrase typed
    with ordinary spaces. Two earlier versions of the guard below were
    silently vacuous for exactly these two reasons; hence the explicit
    whitespace normalization.
    """
    import html
    import re

    raw = svg_path.read_text()
    joined = " ".join(html.unescape(m) for m in re.findall(r"<text[^>]*>([^<]*)</text>", raw))
    return " ".join(joined.split())


def test_rendered_diagrams_carry_no_plantuml_error_banner():
    """PlantUML renders syntax warnings *into* the image rather than failing
    the build -- a deprecated colour form produced a diagram with a warning
    banner across the top that rendered "successfully" and shipped broken.
    Verified to actually catch that case, not just to pass."""
    for svg in sorted((REPO / "docs" / "architecture").glob("*.svg")):
        visible = _svg_visible_text(svg)
        assert "syntax is deprecated" not in visible, svg.name
        assert "Syntax Error" not in visible, svg.name


def test_rendered_diagrams_do_not_leak_markup_into_labels():
    """A `<size:...>` tag inside a cloud/database label leaks its closing
    `</size>` into the rendered label as literal text, and a line carrying
    two `--` sequences is silently parsed as strikethrough."""
    for svg in sorted((REPO / "docs" / "architecture").glob("*.svg")):
        visible = _svg_visible_text(svg)
        assert "</size>" not in visible, svg.name
        assert "<size:" not in visible, svg.name


def test_rendered_diagrams_contain_no_accidental_strikethrough():
    """PlantUML reads two `--` sequences on one line as strikethrough.

    A label like `afriend resolve RUN --claim ID --disposition fixed` renders
    with everything between the markers struck out, which looks deliberate --
    as though the flag were deprecated. Hit twice now: once on a `--mode /
    --preset` label, once on a resolve command. Wrapping the flags in quotes
    fixes the first case; a label with three or more flags has to drop the
    spellings entirely.

    Verified against a deliberately broken render: PlantUML emits
    `text-decoration="line-through"` on the affected <text> element, and
    nothing in these diagrams ever wants that.
    """
    for svg in sorted((REPO / "docs" / "architecture").glob("*.svg")):
        assert "line-through" not in svg.read_text(), (
            f"{svg.name} has struck-through text -- check for a label carrying "
            "two '--' sequences (see this test's docstring)"
        )


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

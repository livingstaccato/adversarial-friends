from pathlib import Path
import subprocess
import sys

SKILL = Path(__file__).resolve().parents[1] / "src" / "adversarial_friends" / "assets"


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    block = text.split("---\n")[1]
    out = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def test_skill_has_name_and_description():
    meta = frontmatter((SKILL / "SKILL.md").read_text())
    assert meta["name"] == "adversarial-friends"
    assert len(meta["description"]) > 80


def test_skill_body_is_under_500_lines():
    assert len((SKILL / "SKILL.md").read_text().splitlines()) < 500


def test_every_lens_file_has_frontmatter():
    lenses = list((SKILL / "lenses").glob("*.md"))
    assert len(lenses) >= 6
    for lens in lenses:
        text = lens.read_text()
        assert text.startswith("---\n"), lens
        assert "requires_failure_scenario:" in text, lens


def test_referenced_files_exist():
    body = (SKILL / "SKILL.md").read_text()
    for name in ("references/modes.md", "references/ledger.md", "references/troubleshooting.md"):
        assert name in body
        assert (SKILL / name).exists()


def test_afriend_console_script_is_installed_and_runs():
    """Package-data misconfiguration (a missing adapter/lens in the wheel) is
    silent at import time -- it only surfaces when the installed entry point
    actually runs. This is the packaging-level equivalent of the old
    bin/af-symlink check: it proves the *installed* `afriend` command works,
    not merely that some file exists on disk."""
    af = Path(sys.executable).parent / "afriend"
    result = subprocess.run([str(af), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "afriend" in result.stdout


def test_lens_filenames_match_roster_expectations():
    """Lens filenames are the lens names roster.resolve assigns via
    cli.available_lenses(), which reads lenses/*.md stems directly."""
    from adversarial_friends import cli as af_cli

    names = {p.stem for p in (SKILL / "lenses").glob("*.md")}
    assert names == set(af_cli.available_lenses())
    assert names >= {"assumptions", "security", "ops", "scope", "testability", "spec-vs-reality"}


def test_scope_lens_is_the_only_advisory_lens():
    for lens in (SKILL / "lenses").glob("*.md"):
        text = lens.read_text()
        meta = frontmatter(text)
        if lens.stem == "scope":
            assert meta["requires_failure_scenario"] == "false"
        else:
            assert meta["requires_failure_scenario"] == "true"

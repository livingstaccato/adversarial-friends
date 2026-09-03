from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "adversarial_friends" / "assets"
ENTRYPOINTS = ASSETS / "entrypoints"
SKILL_NAMES = {"afriend", "review", "status", "configure", "resolve"}
PLUGIN_SKILLS = REPO / "plugins" / "adversarial-friends" / "skills"


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    return {
        key.strip(): value.strip()
        for line in block.splitlines()
        if ":" in line
        for key, _, value in (line.partition(":"),)
    }


def test_entrypoints_are_the_complete_selectable_skill_set():
    found = {path.name for path in ENTRYPOINTS.iterdir() if (path / "SKILL.md").is_file()}
    assert found == SKILL_NAMES


def test_all_entrypoints_have_distinct_portable_metadata():
    descriptions = set()
    for name in SKILL_NAMES:
        meta = frontmatter((ENTRYPOINTS / name / "SKILL.md").read_text())
        assert meta["name"] == name
        assert len(meta["description"]) > 40
        descriptions.add(meta["description"])
    assert len(descriptions) == len(SKILL_NAMES)


def test_afriend_is_the_only_router_and_short_slash_selector():
    text = (ENTRYPOINTS / "afriend" / "SKILL.md").read_text()
    description = " ".join(frontmatter(text)["description"].lower().split())
    body = " ".join(text.lower().replace("`", "").split())

    assert "/afriend" in text
    for trigger in (
        "explicit afriend",
        "ask/use a friend",
        "adversarial friends",
        "directly selects",
    ):
        assert trigger in description, trigger
    for operation in ("review", "status", "configure", "resolve"):
        assert operation in body
    assert "afriend run" in body
    assert "afriend resume" in body
    assert "afriend run --resume" in body
    assert "not new executable aliases" in body
    assert "cli command names remain doctor and run" in body
    for generic_request in ("review this", "poke holes", "second opinion"):
        assert generic_request in body
    assert "friend sent me this" in body


def test_focused_skills_hold_their_operational_boundaries():
    review = " ".join((ENTRYPOINTS / "review" / "SKILL.md").read_text().lower().split())
    status = " ".join((ENTRYPOINTS / "status" / "SKILL.md").read_text().lower().split())
    configure = " ".join((ENTRYPOINTS / "configure" / "SKILL.md").read_text().lower().split())
    resolve = " ".join((ENTRYPOINTS / "resolve" / "SKILL.md").read_text().lower().split())

    for phrase in ("afriend run", "report", "downgrade", "refusal", "failed", "incomplete"):
        assert phrase in review, phrase
    for phrase in ("afriend doctor", "named run", "never dispatch", "never change"):
        assert phrase in status, phrase
    for phrase in ("persistent", "per-run", "external-tool authority", "exact user-requested"):
        assert phrase in configure, phrase
    for phrase in ("named run", "user-supplied disposition", "evidence", "never invent"):
        assert phrase in resolve, phrase
    assert "afriend run --resume" in resolve
    assert "does not require a disposition or evidence" in resolve


def test_afriend_references_are_colocated_and_old_source_paths_are_absent():
    router = ENTRYPOINTS / "afriend"
    for name in ("references/modes.md", "references/ledger.md", "references/troubleshooting.md"):
        assert (router / name).is_file()
    assert not (ASSETS / "SKILL.md").exists()
    assert not (ASSETS / "references").exists()


def test_runtime_assets_project_byte_for_byte_below_router_skill():
    for dirname in ("adapters", "harnesses", "lenses"):
        source = ASSETS / dirname
        for path in source.rglob("*"):
            if path.is_file() and path.name != "__init__.py":
                projected = PLUGIN_SKILLS / "afriend" / dirname / path.relative_to(source)
                assert projected.read_bytes() == path.read_bytes(), projected


def test_plugin_skills_are_the_complete_projection_and_old_skill_is_absent():
    found = {path.name for path in PLUGIN_SKILLS.iterdir() if (path / "SKILL.md").is_file()}
    assert found == SKILL_NAMES
    assert not (PLUGIN_SKILLS / "adversarial-friends").exists()


def test_every_lens_file_has_frontmatter():
    lenses = list((ASSETS / "lenses").glob("*.md"))
    assert len(lenses) >= 6
    for lens in lenses:
        text = lens.read_text()
        assert text.startswith("---\n"), lens
        assert "requires_failure_scenario:" in text, lens


def test_afriend_console_script_is_installed_and_runs():
    af = Path(sys.executable).parent / "afriend"
    result = subprocess.run([str(af), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "afriend" in result.stdout


def test_lens_filenames_match_roster_expectations():
    from adversarial_friends import cli as af_cli

    names = {p.stem for p in (ASSETS / "lenses").glob("*.md")}
    assert names == set(af_cli.available_lenses())
    assert names >= {"assumptions", "security", "ops", "scope", "testability", "spec-vs-reality"}


def test_scope_lens_is_the_only_advisory_lens():
    for lens in (ASSETS / "lenses").glob("*.md"):
        meta = frontmatter(lens.read_text())
        assert meta["requires_failure_scenario"] == ("false" if lens.stem == "scope" else "true")

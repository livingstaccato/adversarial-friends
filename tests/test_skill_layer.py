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


def test_skill_activation_contract_is_command_like_and_narrow():
    text = (SKILL / "SKILL.md").read_text()
    description = " ".join(frontmatter(text)["description"].lower().split())

    for trigger in ("starts with afriend", "afriend to", "adversarial friends", "directly selects"):
        assert trigger in description, trigger
    assert "do not use" in description
    for generic_request in ("poke holes", "second opinion", "architectural-decision"):
        assert generic_request in description, generic_request

    activation = text.split("## When this fires", 1)[1].split("\n## ", 1)[0]
    normalized = " ".join(activation.lower().replace("`", "").split())
    assert "generic requests" in normalized
    assert "ordinary codex work" in normalized
    for generic_request in ("review this", "poke holes", "second opinion"):
        assert generic_request in normalized, generic_request
    assert "do not activate" in normalized


def test_skill_maps_conversational_shorthand_to_the_real_cli_without_inventing_an_artifact():
    body = " ".join((SKILL / "SKILL.md").read_text().lower().replace("`", "").split())

    for shorthand in (
        "afriend this plan",
        "afriend to this plan",
        "afriend docs/design.md",
        "afriend to docs/design.md with crossexam",
    ):
        assert shorthand in body, shorthand
    for contract in (
        "maps to afriend run",
        "existing path",
        "current task's backing file",
        "materialize",
        "exact artifact",
        "ask for a path",
        "default",
        "report",
    ):
        assert contract in body, contract


def test_skill_body_is_under_777_lines():
    assert len((SKILL / "SKILL.md").read_text().splitlines()) < 777


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


def test_skill_explains_safe_provider_selection_and_authority():
    body = " ".join((SKILL / "SKILL.md").read_text().lower().replace("`", "").split())

    for phrase in (
        "host is the orchestrator",
        "--include-self",
        "disabled providers are not probed",
        "--allow-external-tools",
        "external tools are denied by default",
        "legacy-unknown",
    ):
        assert phrase in body, phrase


def test_skill_doctor_contract_uses_provider_readiness_not_binary_presence():
    body = " ".join((SKILL / "SKILL.md").read_text().lower().replace("`", "").split())

    for phrase in (
        "lists every known provider",
        "disabled providers are not probed",
        "ready",
        "reachable-unconfigured",
        "unavailable",
        "policy-blocked",
        "disabled",
        "exits 0 if at least one provider is ready",
        "exits 3 if no provider is ready",
    ):
        assert phrase in body, phrase


def test_skill_distinguishes_report_degradation_from_judging_mode_refusal():
    body = " ".join((SKILL / "SKILL.md").read_text().lower().replace("`", "").split())

    for phrase in (
        "report with one friend",
        "recorded downgrade",
        "crossexam, gate, and loop",
        "exit 3",
        "before a run directory",
    ):
        assert phrase in body, phrase


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

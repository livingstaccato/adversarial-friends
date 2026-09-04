"""Tests for the repository's public-facing documentation and brand assets.

These are not behavioral tests of the runner; they verify that the
documentation tree is present, internally consistent, and safe to render
outside the repository (PyPI, GitHub's raw viewer, a mirrored copy, etc).
"""

import json
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "adversarial_friends" / "assets"
ENTRYPOINTS = ASSETS / "entrypoints"
AFRIEND = ENTRYPOINTS / "afriend"
OPERATOR_DOCS = [AFRIEND / "SKILL.md", *(AFRIEND / "references").glob("*.md")]


def test_readme_leads_with_the_banner():
    first = REPO.joinpath("README.md").read_text().splitlines()[0]
    assert first.startswith("![adversarial-friends]")


def test_scope_selection_docs_explain_artifact_location_and_snapshot_rules():
    readme = REPO.joinpath("README.md").read_text()
    skill = AFRIEND / "SKILL.md"
    troubleshooting = (AFRIEND / "references" / "troubleshooting.md").read_text()

    assert "outside a Git repository" in readme
    assert "repository snapshot" in skill.read_text()
    assert "untracked" in troubleshooting
    assert "ignored" in troubleshooting


def test_modes_docs_explain_zero_response_failure_summary_output():
    modes = (AFRIEND / "references" / "modes.md").read_text()

    assert "--failure-summary terminal" in modes
    assert "--failure-summary report-only" in modes


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


def test_shipped_docs_never_invoke_a_bare_af_command():
    """The console script is `afriend`. `af` was the pre-packaging name and
    does not exist on anyone's PATH.

    The spec and the plan under docs/superpowers/ are signed-off historical
    documents and still say `af` throughout -- that is deliberate (see the
    spec's own divergences section, which records departures rather than
    rewriting the body). Only the docs a *user* follows are checked here, so
    a copied usage line from the spec cannot quietly ship a command that
    fails with "command not found".
    """
    import re

    shipped = [
        REPO / "README.md",
        REPO / "docs" / "README.md",
        REPO / "AGENTS.md",
        *(REPO / "src" / "adversarial_friends" / "assets").rglob("*.md"),
        *(REPO / "plugins").rglob("*.md"),
    ]
    # A bare `af` followed by one of this tool's subcommands. `bin/af` is
    # excluded by requiring a boundary that is not a path separator: two
    # shipped lines mention `bin/af` on purpose, to say it no longer exists.
    pattern = re.compile(r"(?:^|[\s`$(])af\s+(?:run|resolve|init|doctor)\b")
    offenders = []
    for path in shipped:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, "shipped docs invoke `af` instead of `afriend`:\n" + "\n".join(offenders)


def test_shipped_docs_do_not_call_implemented_features_absent():
    """Docs drift the moment a feature lands, and "not in this build" is the
    sentence that ages worst -- it tells a reader not to try something that
    works.

    Caught the README claiming §14.2 extraction was absent two commits after
    it shipped: modes.md had been updated and the README had not.

    Scans PARAGRAPHS, not lines. Line-scoped, it had a blind spot the shape of
    a text wrap: `modes.md` said "Not in this build:" on one line and named a
    flag on the next, and the check saw two unrelated lines. Every doc here is
    hard-wrapped prose, so the sentence this guards is more often split across
    lines than not -- the guard was strictest on exactly the formatting these
    docs do not use.
    """
    import subprocess
    import sys

    help_text = ""
    for sub in ("run", "doctor", "resolve", "init"):
        help_text += subprocess.run(
            [sys.executable, "-m", "adversarial_friends", sub, "--help"],
            capture_output=True,
            text=True,
        ).stdout

    shipped = [
        REPO / "README.md",
        *(REPO / "src" / "adversarial_friends" / "assets").rglob("*.md"),
    ]
    # Anything a doc says is absent, that --help proves is present.
    offenders = []
    for path in shipped:
        line_no = 1
        for block in re.split(r"\n\s*\n", path.read_text()):
            lowered = block.lower()
            if "not in this build" in lowered or "not implemented" in lowered:
                for flag in re.findall(r"--[a-z][a-z-]+", block):
                    if flag in help_text:
                        offenders.append(f"{path.relative_to(REPO)}:{line_no}: {flag} exists")
            line_no += block.count("\n") + 2
    assert not offenders, "docs call an implemented feature absent:\n" + "\n".join(offenders)


def test_docs_index_links_only_to_existing_files():
    index = REPO / "docs" / "README.md"
    import re

    for target in re.findall(r"\]\(([^)#][^)]*)\)", index.read_text()):
        if target.startswith("http"):
            continue
        assert (index.parent / target).exists(), target


def test_evals_file_is_valid_and_has_cases():
    data = json.loads((REPO / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "afriend"
    assert len(data["evals"]) >= 12
    assert all("prompt" in e and "expected_output" in e for e in data["evals"])


def test_evals_cover_narrow_positive_and_negative_activation_boundaries():
    evals = json.loads((REPO / "evals" / "evals.json").read_text())["evals"]
    assert all(type(case.get("should_trigger")) is bool for case in evals)
    positives = [case for case in evals if case["should_trigger"]]
    negatives = [case for case in evals if not case["should_trigger"]]
    positive_prompts = " ".join(case["prompt"].lower() for case in positives)
    negative_prompts = " ".join(case["prompt"].lower() for case in negatives)

    assert "/afriend" in positive_prompts
    assert "afriend to" in positive_prompts
    assert "ask a friend to" in positive_prompts
    assert "adversarial friends" in positive_prompts
    assert "$afriend:afriend" in positive_prompts
    for phrase in (
        "review this",
        "challenge this",
        "poke holes",
        "second opinion",
        "a friend sent me",
    ):
        assert phrase in negative_prompts, phrase
    assert all("af run" not in case["expected_output"].lower() for case in evals)
    assert "afriend" not in negative_prompts
    assert "adversarial friends" not in negative_prompts


def test_evals_cover_guided_session_and_focused_current_workflows():
    cases = json.loads((REPO / "evals" / "evals.json").read_text())["evals"]
    outputs = " ".join(case["expected_output"].lower() for case in cases)
    prompts = " ".join(case["prompt"].lower() for case in cases)

    for phrase in (
        "first-session preflight",
        "task-only",
        "afriend status <run-id-or-path>",
        "afriend doctor",
        "afriend init --guided",
        "profile encode provider authority",
        "afriend resolve <run-id> --list",
        "unique highest-priority",
    ):
        assert phrase in outputs, phrase
    assert "$afriend:status run-123" in prompts


def test_positive_eval_inputs_resolve_and_direct_selector_matches_plugin_namespace():
    evals = json.loads((REPO / "evals" / "evals.json").read_text())["evals"]
    positives = [case for case in evals if case["should_trigger"]]
    manifest = json.loads(
        (REPO / "plugins" / "afriend" / ".codex-plugin" / "plugin.json").read_text()
    )
    skill_names = {path.parent.name for path in ENTRYPOINTS.glob("*/SKILL.md")}
    assert skill_names == {"afriend", "review", "status", "configure", "resolve"}
    expected_selectors = {(manifest["name"], name) for name in skill_names}
    selectors = []

    for case in positives:
        assert "skill" in case and "requires_artifact" in case
        assert case["skill"] in skill_names
        paths = list(case["files"])
        paths.extend(
            re.findall(
                r"(?<![$\w-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md\b",
                case["prompt"],
            )
        )
        if case["requires_artifact"]:
            assert paths, f"positive eval {case['id']} has no resolvable artifact"
        else:
            assert not paths, f"positive eval {case['id']} unexpectedly requires an artifact"
        for path in paths:
            assert REPO.joinpath(path).is_file(), f"positive eval {case['id']}: {path}"
        selectors.extend(re.findall(r"\$([a-z0-9-]+):([a-z0-9-]+)", case["prompt"]))
        assert not re.search(r"\$adversarial-friends:", case["prompt"])

    assert set(selectors) == expected_selectors


def test_current_docs_describe_only_the_five_skill_surface_and_stable_cli():
    current = "\n".join(
        path.read_text()
        for path in (REPO / "README.md", REPO / "AGENTS.md", REPO / "docs" / "README.md")
    ).lower()
    assert "/afriend" in current
    assert "$afriend:afriend" in current
    assert "$adversarial-friends:" not in current
    assert "afriend status" in current and "afriend review" in current
    assert "afriend doctor" in current and "afriend run" in current
    assert "not executable aliases" in current


def test_current_docs_explain_profiles_guided_setup_events_and_run_status():
    """The public README is the CLI's current user-facing contract."""
    readme = REPO.joinpath("README.md").read_text()
    for phrase in (
        "`quick`",
        "`balanced`",
        "`thorough`",
        "afriend init --guided",
        "afriend init --guided --apply",
        "afriend status <run-id-or-path>",
        "`events.jsonl`",
        "afriend profiles",
        "afriend resolve <run-id> --list",
        "afriend resolve <run-id> --next",
    ):
        assert phrase in readme, phrase


def test_skill_routing_diagram_labels_all_skills_and_commands():
    source = (REPO / "docs" / "architecture" / "skill-routing.puml").read_text()
    visible = _svg_visible_text(REPO / "docs" / "architecture" / "skill-routing.svg")
    for label in ("/afriend", "review", "status", "configure", "resolve"):
        assert label in source
        assert label in visible
    for label in ("afriend run", "afriend doctor", "afriend providers", "afriend resolve"):
        assert label in source
        assert label in visible
    assert "afriend run --resume" in source
    assert "afriend run --resume" in visible


def test_skill_routing_diagram_includes_preflight_events_and_read_only_status():
    source = (REPO / "docs" / "architecture" / "skill-routing.puml").read_text()
    visible = _svg_visible_text(REPO / "docs" / "architecture" / "skill-routing.svg")
    for label in ("session preflight", "events.jsonl", "afriend status"):
        assert label in source
        assert label in visible


def test_resume_routes_to_run_without_claim_resolution_inputs():
    router = (AFRIEND / "SKILL.md").read_text().lower()
    resolve = " ".join((ENTRYPOINTS / "resolve" / "SKILL.md").read_text().lower().split())
    assert "afriend resume" in router
    assert "afriend run --resume" in router
    assert "afriend run --resume" in resolve
    assert "does not require a disposition or evidence" in resolve
    resume_eval = next(
        case
        for case in json.loads((REPO / "evals" / "evals.json").read_text())["evals"]
        if case["prompt"].startswith("afriend resume")
    )
    assert resume_eval["skill"] == "afriend"
    assert resume_eval["requires_artifact"] is False
    assert "afriend run --resume" in resume_eval["expected_output"]


def test_status_describes_resume_authority_as_current_command_line_grant():
    status = " ".join((ENTRYPOINTS / "status" / "SKILL.md").read_text().lower().split())
    assert "past run's authority record is descriptive only" in status
    assert "same normalized grant is supplied again" in status


def test_plugin_install_metadata_matches_narrow_activation_and_advisory_host_contract():
    plugin_root = REPO / "plugins" / "afriend"
    codex = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text())
    claude = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((REPO / "plugins" / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(item for item in marketplace["plugins"] if item["name"] == codex["name"])
    descriptions = [
        codex["description"],
        codex["interface"]["shortDescription"],
        codex["interface"]["longDescription"],
        claude["description"],
        marketplace["description"],
        entry["description"],
    ]

    assert marketplace["name"] == "afriend"
    assert claude["name"] == entry["name"] == codex["name"] == "afriend"
    assert claude["version"] == entry["version"]
    assert codex["version"].partition("+")[0] == claude["version"]
    assert codex["interface"]["displayName"] == "afriend"
    for description in descriptions:
        normalized = " ".join(description.lower().split())
        assert "codex" in normalized
        assert "advisory" in normalized
        assert "other agent clis" not in normalized
        assert "independent adversarial reviewers" not in normalized

    prompts = codex["interface"]["defaultPrompt"]
    assert prompts
    assert prompts == [
        "/afriend README.md",
        "$afriend:review README.md",
        "$afriend:status",
        "$afriend:configure",
        "$afriend:resolve",
    ]


def test_codex_local_marketplace_presents_afriend_and_sources_this_plugin():
    marketplace = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text())
    assert marketplace["name"] == "afriend-local"
    assert marketplace["interface"]["displayName"] == "afriend"
    assert marketplace["plugins"] == [
        {
            "name": "afriend",
            "source": {"source": "local", "path": "./plugins/afriend"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }
    ]


def test_the_advertised_test_count_is_the_real_one():
    """The README states a test count in its badge, and nothing kept it
    honest. It once said 365 while the suite had grown past 900, which is the
    most quietly embarrassing kind of stale:
    a number a reader has no way to check and every reason to believe.

    Collection is the source of truth rather than a hand-maintained constant,
    so the only way to change the advertised number is to change the suite.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    collected = sum(
        int(m.group(1)) for m in re.finditer(r"^\S+\.py: (\d+)$", proc.stdout, re.MULTILINE)
    )
    assert collected > 0, f"could not read a collection count from pytest:\n{proc.stdout[-2000:]}"

    readme = REPO.joinpath("README.md").read_text()
    advertised = set(re.findall(r"badge/tests-(\d+)-", readme))
    advertised |= set(re.findall(r"make test\s+# pytest — (\d+) tests", readme))
    assert advertised == {str(collected)}, (
        f"README advertises tests={sorted(advertised)}, pytest collects {collected}"
    )


def test_no_shipped_doc_calls_a_shipped_mode_unimplemented():
    """The flag-scoped guard above missed three years of nothing and then
    three sentences at once: `AGENTS.md` said `report` was the only mode this
    build implements, and `ledger.md` said verdict records were "schema only"
    and an orchestrator merge had "no implementation to produce it yet". All
    three named a feature that ships, and none named a flag, so the existing
    check saw nothing.

    This one keys on the sentence pattern rather than the flag: any paragraph
    claiming absence is checked against the modes and record types the code
    actually has.
    """
    from adversarial_friends.commands.runmeta import IMPLEMENTED_MODES
    from adversarial_friends.ledger import _TYPE_NAMES

    shipped_names = {m.lower() for m in IMPLEMENTED_MODES}
    shipped_names |= {n.lower() for n in _TYPE_NAMES.values()}

    absence = re.compile(
        r"not in this build|not implemented|not produced|no implementation"
        r"|only mode|schema only|reserved for|has no implementation",
        re.IGNORECASE,
    )
    docs = [
        REPO / "README.md",
        REPO / "AGENTS.md",
        REPO / "docs" / "README.md",
        *(REPO / "src" / "adversarial_friends" / "assets").rglob("*.md"),
    ]
    offenders = []
    for path in docs:
        for block in re.split(r"\n\s*\n", path.read_text()):
            if not absence.search(block):
                continue
            for name in sorted(shipped_names):
                if re.search(rf"`{re.escape(name)}`", block):
                    offenders.append(
                        f"{path.relative_to(REPO)}: {name!r} ships, doc says otherwise"
                    )
    assert not offenders, "docs call a shipped feature absent:\n" + "\n".join(offenders)


def test_contract_first_provider_and_authority_guidance_is_shipped():
    docs = {
        "README.md": REPO.joinpath("README.md").read_text(),
        **{path.name: path.read_text() for path in OPERATOR_DOCS},
    }
    joined = " ".join("\n".join(docs.values()).lower().replace("`", "").split())

    for phrase in (
        "host is the orchestrator",
        "--include-self",
        "afriend providers list",
        "afriend providers enable",
        "afriend providers disable",
        "afriend providers set-model",
        "--enable-provider",
        "--disable-provider",
        "disabled providers are not probed",
        "--allow-external-tools",
        "external tools are denied by default",
        "reachable-unconfigured",
        "policy-blocked",
        "snapshot",
    ):
        assert phrase in joined, phrase

    assert "max-loop-iterations" in joined
    assert re.search(r"\bexits? 11\b", joined)


def test_shipped_docs_state_the_one_friend_mode_contract_exactly():
    readme = " ".join(REPO.joinpath("README.md").read_text().lower().split())
    skill = " ".join((AFRIEND / "SKILL.md").read_text().lower().split())
    modes = " ".join((AFRIEND / "references" / "modes.md").read_text().lower().split())
    diagram = " ".join(REPO.joinpath("docs/architecture/run-flow.puml").read_text().lower().split())

    for prose in (readme, skill, modes):
        assert "report" in prose
        assert "one friend" in prose
        assert "recorded downgrade" in prose
        assert all(mode in prose for mode in ("crossexam", "gate", "loop"))
        assert "exit 3" in prose
        assert "before a run directory" in prose

    assert "exactly one resolved friend?" in diagram
    assert "report: record one-friend downgrade" in diagram
    assert "judging mode: exit 3 before run directory" in diagram
    assert "fewer than two independent\\nnon-host friends" in diagram


def test_rendered_run_flow_shows_the_one_friend_mode_contract():
    visible = _svg_visible_text(REPO / "docs" / "architecture" / "run-flow.svg").lower()

    assert "exactly one resolved friend?" in visible
    assert "report: record one-friend downgrade" in visible
    assert "judging mode: exit 3 before run directory" in visible
    assert "fewer than two independent non-host friends" in visible


def test_live_docs_describe_scope_based_isolation_and_exec_environment_filtering():
    readme = " ".join(REPO.joinpath("README.md").read_text().lower().split())
    modes = " ".join((AFRIEND / "references" / "modes.md").read_text().lower().split())
    diagram = " ".join(
        REPO.joinpath("docs/architecture/run-flow.puml")
        .read_text()
        .lower()
        .replace("\\n", " ")
        .split()
    )

    assert "each friend's effective scope selects its isolation directory" in readme
    assert "doc scope gets an artifact-only directory" in readme
    assert "adapter read-only flags and, where required, os confinement" in readme
    assert "every executable friend process" in modes
    assert "effective friend scope is repo?" in diagram
    assert "artifact-only directory" in diagram
    assert "separately apply adapter read-only flags" in diagram
    assert "os filesystem confinement as secondary controls" in diagram
    assert "adapter has a real read-only mode?" not in diagram


def test_live_docs_and_run_flow_explain_doc_scope_warning_and_confined_dns():
    """The artifact-scope warning and Linux resolver bind are user-visible
    safeguards, not implementation trivia.  Both prose and the diagram must
    describe them so a thin review or a DNS failure can be diagnosed.
    """
    readme = " ".join(REPO.joinpath("README.md").read_text().lower().split())
    diagram = " ".join(
        REPO.joinpath("docs/architecture/run-flow.puml")
        .read_text()
        .lower()
        .replace("\\n", " ")
        .split()
    )
    visible = _svg_visible_text(REPO / "docs" / "architecture" / "run-flow.svg").lower()

    assert "doc-scope warning" in readme
    assert "resolv.conf" in readme
    assert "resolver target" in readme
    assert "warn on stderr before dispatch" in diagram
    assert "resolver target" in diagram
    assert "resolver target" in visible


def test_modes_explains_resume_authority_exception_and_doctor_readiness():
    modes = " ".join(
        (AFRIEND / "references" / "modes.md").read_text().lower().replace("`", "").split()
    )

    assert "no other non-authority configuration flags" in modes
    assert "authority grants must be repeated exactly" in modes
    assert "afriend run --resume <run-id> --allow-external-tools=agy" in modes
    assert "lists every known provider" in modes
    assert "disabled providers are not probed" in modes
    assert "exits 0 if at least one provider is ready" in modes
    assert "exits 3 if no provider is ready" in modes
    assert "no other flags" not in modes


def test_unsafe_override_docs_keep_write_protection_separate_from_read_confinement():
    readme = " ".join(REPO.joinpath("README.md").read_text().lower().replace("`", "").split())
    skill = " ".join((AFRIEND / "SKILL.md").read_text().lower().replace("`", "").split())
    troubleshooting = " ".join(
        (AFRIEND / "references" / "troubleshooting.md").read_text().lower().replace("`", "").split()
    )
    modes = " ".join(
        (AFRIEND / "references" / "modes.md").read_text().lower().replace("`", "").split()
    )

    assert "verified self-confining provider" not in " ".join((readme, skill, troubleshooting))
    assert "mode controls writes, not filesystem reads" in readme
    assert "does not replace os read confinement" in skill
    assert "only permits fallback when os confinement is unavailable" in modes
    assert "does not disable an available os sandbox" in modes


def test_operator_docs_explain_advisory_host_and_independent_authority():
    paths = (
        REPO / "README.md",
        *OPERATOR_DOCS,
    )
    docs = " ".join(" ".join(path.read_text().lower().replace("`", "").split()) for path in paths)

    for contract in (
        "codex remains the orchestrator",
        "included as a friend by default",
        "host-self-review (advisory)",
        "independent=false",
        "two independent non-host friends",
        "--require-friends",
        "judging quorum",
        "gate clearance",
        "loop convergence",
        "non-codex hosts",
        "excluded by default",
        "--include-self and --exclude-self are mutually exclusive",
    ):
        assert contract in docs, contract


def test_operator_docs_pin_provider_authority_and_agy_harness_contracts():
    paths = (
        REPO / "README.md",
        *OPERATOR_DOCS,
    )
    docs = " ".join(" ".join(path.read_text().lower().replace("`", "").split()) for path in paths)

    for contract in (
        "--allow-external-tools=provider",
        "--allow-external-tools=*",
        "unknown, duplicate, or mixed",
        "valueless",
        "unsafe-extra-args",
        "global *",
        "do not change provider defaults",
        "staged into the run's isolated workspace",
        "--agent",
        "--disable-slash-commands",
        "--mode plan",
        "--sandbox",
        "external_tools=uncontrolled",
        "explicitly-allowed",
        "global antigravity configuration",
        "best-effort limitation",
        "sandbox does not mean external tools were denied",
    ):
        assert contract in docs, contract


def test_operator_docs_state_real_mode_defaults_and_runtime_expectations():
    skill = " ".join((AFRIEND / "SKILL.md").read_text().lower().replace("`", "").split())
    modes = " ".join(
        (AFRIEND / "references" / "modes.md").read_text().lower().replace("`", "").split()
    )
    combined = f"{skill} {modes}"

    assert "report is the default" in combined
    assert "three total rounds" in combined
    assert "maximum of five iterations" in combined
    assert "two consecutive dry rounds" in combined
    assert "slowest selected friend" in combined
    assert "six minutes" not in combined
    assert "twenty minutes" not in combined

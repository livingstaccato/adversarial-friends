# Multi-skill interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single Adversarial Friends skill with `/afriend` plus focused review, status, configuration, and resolution skills, without losing runtime payload from the plugin.

**Architecture:** `assets/entrypoints/` holds five selectable skills: `afriend` is the sole router and owns its detailed references; focused skills are self-contained. The plugin projection places root runtime assets plus router content in `plugins/adversarial-friends/skills/afriend/` and every focused entrypoint directly below `skills/`. The stable CLI remains `run`, `doctor`, `providers`, and `resolve`.

**Tech Stack:** Python 3.11+, setuptools package data, pytest, standard-library wheel archive validation, PlantUML, Codex/Claude plugin metadata.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `src/adversarial_friends/assets/entrypoints/afriend/` | `/afriend` router and its detailed current references. |
| `src/adversarial_friends/assets/entrypoints/{review,status,configure,resolve}/SKILL.md` | Focused operational skills. |
| `src/adversarial_friends/assets/{adapters,harnesses,lenses}/` | Runtime asset sources mirrored into the router plugin folder. |
| `scripts/check_plugin_sync.py` | Composite source-to-plugin expected-map builder, verifier, and copy command. |
| `ci/verify_wheel_assets.sh` | Exact packaged-asset manifest check. |
| `tests/test_skill_layer.py`, `tests/test_agy_harness.py`, `tests/test_docs.py` | Skill, projection, source relocation, eval, documentation, diagram, and test-count contracts. |
| `README.md`, `AGENTS.md`, `docs/README.md`, `docs/architecture/*.puml` | Current product documentation and diagrams. |

### Task 1: Implement and document the complete `/afriend` cutover

**Files:**
- Delete: `src/adversarial_friends/assets/SKILL.md`
- Delete: `src/adversarial_friends/assets/references/ledger.md`
- Delete: `src/adversarial_friends/assets/references/modes.md`
- Delete: `src/adversarial_friends/assets/references/troubleshooting.md`
- Create: `src/adversarial_friends/assets/entrypoints/afriend/SKILL.md`
- Create: `src/adversarial_friends/assets/entrypoints/afriend/references/{ledger,modes,troubleshooting}.md`
- Create: `src/adversarial_friends/assets/entrypoints/{review,status,configure,resolve}/SKILL.md`
- Modify: `pyproject.toml:57-64`
- Modify: `scripts/check_plugin_sync.py:1-75`
- Modify: `Makefile:23-40`
- Modify: `ci/verify_wheel_assets.sh:1-38`
- Modify: `plugins/adversarial-friends/.codex-plugin/plugin.json:1-35`
- Modify: `evals/evals.json:1-90`
- Modify: `README.md:13,20-180,390-425`
- Modify: `AGENTS.md:1-35`
- Modify: `docs/README.md`
- Modify: `docs/architecture/README.md`
- Modify: `docs/architecture/components.puml`
- Create: `docs/architecture/skill-routing.puml`
- Create: `docs/architecture/skill-routing.png`
- Create: `docs/architecture/skill-routing.svg`
- Modify: `tests/test_skill_layer.py:1-120`
- Modify: `tests/test_agy_harness.py:14-29,69-70`
- Modify: `tests/test_docs.py:20-30,260-377,423-643`

- [ ] **Step 1: Write the failing final-contract tests.**

  In `tests/test_skill_layer.py`, define the entrypoint root and all expected
  selectable skills:

  ```python
  ASSETS = REPO / "src" / "adversarial_friends" / "assets"
  ENTRYPOINTS = ASSETS / "entrypoints"
  SKILL_NAMES = {"afriend", "review", "status", "configure", "resolve"}

  def test_entrypoints_are_the_complete_selectable_skill_set():
      found = {path.name for path in ENTRYPOINTS.iterdir() if (path / "SKILL.md").is_file()}
      assert found == SKILL_NAMES
  ```

  Add a byte-for-byte projection assertion for every canonical adapter,
  harness, and lens at the matching `plugins/adversarial-friends/skills/afriend/`
  path. Point `tests/test_agy_harness.py` at
  `skills/afriend/harnesses/agy/afriend-reviewer.md`. Assert that the old root
  skill/reference paths and `skills/adversarial-friends/` are absent.

  In `tests/test_docs.py`, replace all root-skill/reference constants with:

  ```python
  AFRIEND = ENTRYPOINTS / "afriend"
  OPERATOR_DOCS = [AFRIEND / "SKILL.md", *(AFRIEND / "references").glob("*.md")]
  ```

  Use the joined `OPERATOR_DOCS` text for router/mode/provider contracts.
  Add valid frontmatter/name/description tests for all five skills, so this
  portable suite is the release gate for skill metadata.

  Require every positive eval to include `skill` and `requires_artifact`.
  Require a file path only when `requires_artifact` is true; assert every
  `skill` belongs to `SKILL_NAMES` and direct selectors are exactly the five
  qualified selectors. Add tests for `/afriend`, the stable CLI command set,
  the absence of documented executable `afriend status`/`afriend review`
  aliases, the five labels plus four command labels in `skill-routing.svg`,
  and the dynamically collected README test-badge count.

- [ ] **Step 2: Run the focused tests and verify the current tree fails.**

  Run:

  ```bash
  uv run pytest tests/test_skill_layer.py tests/test_agy_harness.py tests/test_docs.py -q --color=no
  ```

  Expected: FAIL because the current root skill, mirror location, eval schema,
  docs, and diagrams do not satisfy the `/afriend` contract.

- [ ] **Step 3: Create the final skill collection.**

  Move the current router detail into
  `assets/entrypoints/afriend/` and delete the old root skill/references. The
  router frontmatter name is `afriend`; its description makes `/afriend` the
  short direct selector and covers explicit `afriend`, “a friend”, and named
  Adversarial Friends requests. It maps conversational operation words to
  focused skills and the established CLI; it does not introduce CLI aliases.

  Write self-contained focused skills with these boundaries:

  ```markdown
  - `review` uses `afriend run`, defaults to `report`, and reports downgrades, refusals, failed friends, and incomplete judging results.
  - `status` runs `afriend doctor` and may inspect a named run; it does not dispatch friends or change settings.
  - `configure` shows the effective roster and changes a provider default only for the exact user-requested change.
  - `resolve` requires a named run, user-supplied disposition, and concrete evidence; it never invents any of them.
  ```

  Keep provider deny-by-default, advisory host participation, and explicit
  external-tool authority intact in every skill where they affect a decision.

- [ ] **Step 4: Build the composite plugin projection safely.**

  Change `scripts/check_plugin_sync.py` from a root-tree comparison to an
  expected-map builder:

  ```python
  def expected_plugin_files() -> dict[Path, bytes]:
      expected = project_tree(ASSETS / "entrypoints", Path("."))
      expected |= project_tree(ASSETS / "adapters", Path("afriend/adapters"))
      expected |= project_tree(ASSETS / "harnesses", Path("afriend/harnesses"))
      expected |= project_tree(ASSETS / "lenses", Path("afriend/lenses"))
      return expected
  ```

  `project_tree` returns relative file bytes. The checker compares that map to
  `plugins/adversarial-friends/skills`; `--copy` materializes the expected
  map in a temporary directory below the resolved plugin parent, validates it,
  then replaces only that `skills/` directory. Reject unexpected arguments and
  never write outside the plugin root. Update `plugin-sync-copy` to invoke the
  `--copy` command. The checked-in plugin tree is a packaging source; it is
  not the live installed-plugin cache.

  In `pyproject.toml`, replace root skill/reference patterns with:

  ```toml
  "assets/entrypoints/**/*.md",
  ```

  Retain runtime patterns. Change `ci/verify_wheel_assets.sh` to derive the
  exact expected archive list from every canonical adapter (`*.toml`), harness
  (`*.md`), lens (`*.md`), and entrypoint (`*.md`) file. Compare the set to
  archive paths below `adversarial_friends/assets/`, reporting missing and
  unexpected files instead of maintaining an asset count.

- [ ] **Step 5: Update current docs, plugin metadata, evals, and diagrams.**

  Rewrite README, AGENTS.md, docs index, plugin default prompts, and the
  components diagram as the current five-skill product. Use `/afriend` and
  `$adversarial-friends:afriend`; do not present a long selector. State that
  conversational `afriend status` and `afriend review` route to skills while
  the executable commands remain `afriend doctor` and `afriend run`.

  Add direct-selector and router evals for `afriend`, `review`, `status`,
  `configure`, and `resolve`; only review/run cases set
  `requires_artifact: true`. Preserve negative generic-review cases. Create
  `skill-routing.puml` showing `/afriend` routing to the four focused skills,
  then to `run`, `doctor`, `providers`, and `resolve`. Current public docs and
  diagrams must describe only this surface; historical `docs/superpowers/`
  records remain historical records.

- [ ] **Step 6: Render, synchronize, and verify the complete cutover.**

  Run:

  ```bash
  make plugin-sync-copy
  make plugin-sync
  make diagrams
  ci/verify_wheel_assets.sh
  uv run pytest tests/test_skill_layer.py tests/test_agy_harness.py tests/test_docs.py -q --color=no
  PYTEST_ADDOPTS='--color=no' uv run pytest --collect-only -q -p no:cacheprovider
  ```

  Replace the README badge count with the exact final collection count, then
  rerun the `tests/test_docs.py` command above. Expected: PASS. No commit is
  made until the asset tree, plugin projection, wheel, docs, diagrams, evals,
  and test badge agree.

- [ ] **Step 7: Commit the finished cutover.**

  ```bash
  git add src/adversarial_friends/assets pyproject.toml scripts/check_plugin_sync.py Makefile ci/verify_wheel_assets.sh plugins/adversarial-friends/skills plugins/adversarial-friends/.codex-plugin/plugin.json README.md AGENTS.md docs evals/evals.json tests/test_skill_layer.py tests/test_agy_harness.py tests/test_docs.py
  git commit -m "feat: add focused adversarial friend skills"
  ```

### Task 2: Prove the shipped skill surface

**Files:**
- Test: complete repository quality suite and plugin skill selection.

- [ ] **Step 1: Run all portable release gates.**

  Run:

  ```bash
  make quality
  ```

  Expected: lint, strict typing, line cap, composite plugin sync, version
  sync, exact wheel and isolated-install checks, and the complete pytest suite
  pass.

- [ ] **Step 2: Forward-test selection without dispatching friends or changing defaults.**

  In a fresh evaluation context, select `/afriend`,
  `$adversarial-friends:review README.md`,
  `$adversarial-friends:status`, `$adversarial-friends:configure`, and
  `$adversarial-friends:resolve`. Verify that each identifies the correct
  stable command and asks for missing artifact/run data rather than inventing
  it. Do not execute a paid review or provider change in this test.

## Self-review

- One atomic implementation task prevents a committed partial tree, stale
  mirror, dangling source reference, or red documentation suite.
- The runtime adapter, harness, and lens payload remains byte-identical in the
  router plugin folder and the exact wheel manifest proves it ships.
- Evals have explicit skill ownership and artifact requirements; README’s
  collected-test badge is updated in the same verified task.
- Focused skills are self-contained. `/afriend` is the sole router and short
  slash command; no long selector remains.

# afriend Plugin Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `afriend` the stable Codex and Claude plugin identifier and the qualified Codex skill namespace.

**Architecture:** The Python package and runtime identities remain unchanged. The plugin bundle moves from `plugins/adversarial-friends` to `plugins/afriend`; its manifests, marketplaces, source entrypoints, user-facing docs, and tests agree on `afriend` as the plugin namespace.

**Tech Stack:** Python 3.11+, pytest, JSON plugin manifests, Codex CLI.

---

### Task 1: Specify the namespace contract in tests

**Files:**
- Modify: `tests/test_docs.py`
- Modify: `tests/test_skill_layer.py`
- Modify: `evals/evals.json`

- [ ] **Step 1: Write failing assertions for `afriend`**

Assert that the plugin root is `plugins/afriend`, the manifest name is
`afriend`, current docs use `$afriend:afriend`, and no current direct form
starts with `$adversarial-friends:`. Change every qualified evaluation input to
`$afriend:<skill>` and assert all detected selectors use the `afriend`
namespace.

- [ ] **Step 2: Verify the assertions fail before the rename**

Run: `uv run pytest tests/test_docs.py tests/test_skill_layer.py -q`

Expected: FAIL because the manifest, directory, and direct selectors retain the old namespace.

### Task 2: Rename the complete plugin bundle

**Files:**
- Move: `plugins/adversarial-friends/` to `plugins/afriend/`
- Modify: `.agents/plugins/marketplace.json`
- Modify: `plugins/afriend/.codex-plugin/plugin.json`
- Modify: `plugins/afriend/.claude-plugin/plugin.json`
- Modify: `plugins/.claude-plugin/marketplace.json`
- Modify: `scripts/check_plugin_sync.py`
- Modify: `scripts/check_version_sync.py`

- [ ] **Step 1: Rename the plugin directory**

Run: `git mv plugins/adversarial-friends plugins/afriend`

- [ ] **Step 2: Make every plugin and marketplace identifier `afriend`**

Set both manifest `name` values to `"afriend"`. Set the repository Codex
marketplace entry to name `afriend` and source path `./plugins/afriend`. Set
the Claude marketplace name, contained plugin name, and source to `afriend`
and `./afriend`. Point plugin synchronization and Codex version handling at
`plugins/afriend`.

- [ ] **Step 3: Refresh the local Codex cachebuster**

Run: `python3 /Users/tim/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py plugins/afriend`

- [ ] **Step 4: Run the focused tests and guards**

Run: `uv run pytest tests/test_docs.py tests/test_skill_layer.py -q && make plugin-sync && make version-sync`

Expected: PASS.

### Task 3: Replace current direct-selection language

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `src/adversarial_friends/assets/entrypoints/{afriend,review,status,configure,resolve}/SKILL.md`
- Modify: `plugins/afriend/skills/{afriend,review,status,configure,resolve}/SKILL.md` via `make plugin-sync-copy`
- Modify: `evals/evals.json`

- [ ] **Step 1: Replace every current direct form with `$afriend:<skill>`**

Retain `/afriend` and human-readable phrases such as `afriend review`; do not
replace Python package names, repository URLs, or state/config paths.

- [ ] **Step 2: Synchronize the projected plugin skills**

Run: `make plugin-sync-copy`

- [ ] **Step 3: Run the focused tests**

Run: `uv run pytest tests/test_docs.py tests/test_skill_layer.py -q`

Expected: PASS.

### Task 4: Validate the delivered local plugin

**Files:**
- Verify: `plugins/afriend/.codex-plugin/plugin.json`
- Verify: `.agents/plugins/marketplace.json`

- [ ] **Step 1: Run complete verification**

Run: `make quality`

Expected: exit 0 with all tests passing.

- [ ] **Step 2: Replace the installed plugin**

Run: `codex plugin remove adversarial-friends@afriend-local --json && codex plugin add afriend@afriend-local --json && codex plugin list`

Expected: `afriend@afriend-local` is installed and enabled; the old selector is absent.

- [ ] **Step 3: Inspect the installed metadata**

Run: `rg -n '"name": "afriend"|\$afriend:|display_name: "afriend' /Users/tim/.codex/plugins/cache/afriend-local/afriend/*`

Expected: the installed payload exposes `afriend` and all five qualified `$afriend:<skill>` selectors.

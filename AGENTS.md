# Adversarial Friends

This repository ships a Python package (`adversarial_friends`) whose console
script is `afriend`, plus a skill/plugin payload that challenges specs, plans,
and reviews by dispatching them to other agent CLIs as independent adversarial
reviewers.

## Using the tool

Use `/afriend` or `$adversarial-friends:afriend` for explicit product routing;
the five selectable skills are `afriend`, `review`, `status`, `configure`,
and `resolve`. Conversational `afriend review` and `afriend status` route to
focused skills and are not executable aliases: use `afriend run <artifact>`
for a review, `afriend status <run-id-or-path>` for a run, and `afriend doctor`
for readiness. Use `afriend init --guided` for a no-write setup preview and
`afriend profiles` for safe named profiles. All four modes -- `report`,
`crossexam`, `gate`, `loop` -- ship; see
`src/adversarial_friends/assets/entrypoints/afriend/references/modes.md`.
`afriend resume <run-id>` routes to `afriend run --resume <run-id>`, not to
claim resolution; it requires neither disposition nor evidence.

## Layout

- `src/adversarial_friends/` — the runtime package (stdlib-only, no runtime
  dependencies). `cli.py` is a thin entry point; the work lives in
  `cliargs.py`, `prompt.py`, `dispatch.py`, and `commands/`.
- `src/adversarial_friends/assets/` — canonical package data: runtime
  `adapters/`, `harnesses/`, `lenses/`, plus five `entrypoints/` skills.
- `plugins/adversarial-friends/skills/` — the composite projection: focused
  skills map directly; router references and runtime data live below
  `skills/afriend/`. Never edit it directly; edit `assets/` and re-sync.
- `docs/` — prose docs and architecture diagrams. Excluded from `ruff format`
  so embedded code fences in historical specs/plans are left alone.

## Working on it

```bash
make install      # uv sync
make test         # pytest
make quality      # every portable CI gate, wheel checks, and tests
```

`make quality` runs every portable CI gate, including wheel construction and
isolated installation. Linux CI additionally installs bubblewrap and requires
the real OS-confinement tests to execute; macOS cannot reproduce that Linux-
specific assertion locally. Use `make act-ci` for the closest local Linux run.

Two gates are especially easy to trip:

- **`plugin-sync`** fails if the canonical entrypoint/runtime projection and
  the plugin differ. After editing `assets/`, run `make plugin-sync-copy`.
- **`version-sync`** fails if `VERSION` disagrees with the `version` field in
  any plugin manifest under `plugins/`. Bump all of them together.

`mypy --strict` runs against `src/` only; `tests/` is deliberately exempt.

# Adversarial Friends

This repository ships a Python package (`adversarial_friends`) whose console
script is `afriend`, plus a skill/plugin payload that challenges specs, plans,
and reviews by dispatching them to other agent CLIs as independent adversarial
reviewers.

## Using the tool

Read `src/adversarial_friends/assets/SKILL.md` for the workflow. Run the tool
with `afriend run <artifact> --mode report`, and `afriend doctor` when a run
comes back thinner than expected. All four modes -- `report`, `crossexam`,
`gate`, `loop` -- ship; see
`src/adversarial_friends/assets/references/modes.md` for what each one costs
and which exit codes it can produce.

## Layout

- `src/adversarial_friends/` — the runtime package (stdlib-only, no runtime
  dependencies). `cli.py` is a thin entry point; the work lives in
  `cliargs.py`, `prompt.py`, `dispatch.py`, and `commands/`.
- `src/adversarial_friends/assets/` — **canonical** skill payload shipped
  inside the wheel as package data: `SKILL.md`, `adapters/`, `lenses/`,
  `references/`. `paths.py` resolves these via `importlib.resources`.
- `plugins/adversarial-friends/skills/adversarial-friends/` — a byte-identical
  **mirror** of `assets/`, for plugin loaders that cannot install a Python
  package. Never edit the mirror directly; edit `assets/` and re-sync.
- `docs/` — prose docs and architecture diagrams. Excluded from `ruff format`
  so embedded code fences in historical specs/plans are left alone.

## Working on it

```bash
make install      # uv sync
make test         # pytest
make quality      # lint + type-check + all sync gates + tests
```

`make quality` runs the same gates CI does. Two of them are easy to trip:

- **`plugin-sync`** fails if `assets/` and the `plugins/` mirror drift. After
  editing anything under `assets/`, re-copy it into the mirror.
- **`version-sync`** fails if `VERSION` disagrees with the `version` field in
  any plugin manifest under `plugins/`. Bump all of them together.

`mypy --strict` runs against `src/` only; `tests/` is deliberately exempt.

# 📚 afriend — Documentation

Reference documentation for users and contributors. For the introduction and
quickstart, see the [top-level README](../README.md); come here for detail.

---

## 🚀 Start here

| Document | What it covers |
|---|---|
| [/afriend](../src/afriend/assets/entrypoints/afriend/SKILL.md) | The sole router, session preflight, lifecycle completion feedback, and stable CLI routing |
| [review](../src/afriend/assets/entrypoints/review/SKILL.md) | Artifact reviews with `afriend run` and task-only profile/mode choices |
| [status](../src/afriend/assets/entrypoints/status/SKILL.md) | Read-only readiness with `doctor` and named-run inspection with `status` |
| [configure](../src/afriend/assets/entrypoints/configure/SKILL.md) | Guided setup, explicit provider defaults, and safe named profiles |
| [resolve](../src/afriend/assets/entrypoints/resolve/SKILL.md) | Read-only claim discovery and named-run resolutions with supplied evidence |
| [modes](../src/afriend/assets/entrypoints/afriend/references/modes.md) | All four modes — `report`, `crossexam`, `gate`, `loop` — plus claim states, ceilings, and exit codes |

`afriend resume <run-id>` routes through `/afriend` to `afriend run --resume
<run-id>`; it is not a claim-resolution disposition and needs no evidence.

> These entrypoints ship **inside the wheel** as package and plugin distribution
> payload. The runtime resolves adapters, lenses, and staged harness workspace
> assets via `importlib.resources`; it does not resolve entrypoint skills at
> runtime. Edit canonical assets here, never the plugin projection.

---

## 🧠 Core concepts

| Document | What it covers |
|---|---|
| [ledger](../src/afriend/assets/entrypoints/afriend/references/ledger.md) | Claim, verdict, alias, and resolution records — and how to read `claims.jsonl` directly |
| [architecture/](architecture/README.md) | Diagrams: module architecture, run flow, claim lifecycle |

### The diagrams

- **[Module architecture](architecture/components.puml)** — which module owns
  what, and how a run threads through them.
- **[Run flow](architecture/run-flow.puml)** — how `afriend run` resolves
  automatic artifact-derived or explicit `--repo` context, records Git-blob
  binding or an independently frozen artifact, admits providers, applies
  scoped authority, stages harnesses, preserves DNS under Linux confinement,
  dispatches a report fan-out, and records downgrades.
- **[Claim lifecycle](architecture/claim-lifecycle.puml)** — how two friends
  finding the same defect become one corroborated claim without losing either
  attribution.
- **[Cross-examination states](architecture/crossexam-states.puml)** — the
  eight states a claim can reach under `--mode crossexam`, which are terminal,
  and which need a human.
- **[The gate loop](architecture/gate-workflow.puml)** — how `--mode gate` and
  `afriend resolve` fit together, and the two things a resolution can be
  refused for.
- **[Skill routing](architecture/skill-routing.puml)** — how `/afriend` hands
  explicit operations through session preflight to focused skills, event
  feedback, and stable CLI commands.

Rendered PNG and SVG are committed alongside each source. Regenerate with
`make diagrams`.

---

## 🏛 Design history

| Document | What it covers |
|---|---|
| [design spec](superpowers/specs/2026-08-22-adversarial-friends-design.md) | The full design — including §19, the adversarial review history that produced it |
| [core runner plan](superpowers/plans/2026-08-22-adversarial-friends-core-runner.md) | The 14-task implementation plan the runner was built from |

> ⚠️ These two are **historical records**, not live documentation. They are
> excluded from `ruff format` so their embedded code fences stay as originally
> written. Read them for *why* decisions were made; read the pages above for
> *what is true now*.

---

## 🎨 Assets

| Document | What it covers |
|---|---|
| [images/README.md](images/README.md) | Brand asset layout, sizes, and how to regenerate them |

---

## 🛠 Contributing

See [AGENTS.md](../AGENTS.md) for repository layout, the canonical-vs-mirror
rule for skill assets, and the quality gates `make quality` enforces.

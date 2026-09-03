# 📚 Adversarial Friends — Documentation

Reference documentation for users and contributors. For the introduction and
quickstart, see the [top-level README](../README.md); come here for detail.

---

## 🚀 Start here

| Document | What it covers |
|---|---|
| [/afriend](../src/adversarial_friends/assets/entrypoints/afriend/SKILL.md) | The sole router and short selector; stable commands remain `afriend run` and `afriend doctor` |
| [review](../src/adversarial_friends/assets/entrypoints/review/SKILL.md) | Artifact reviews with `afriend run` |
| [status](../src/adversarial_friends/assets/entrypoints/status/SKILL.md) | Read-only readiness and named-run inspection |
| [configure](../src/adversarial_friends/assets/entrypoints/configure/SKILL.md) | Explicit persistent provider-default changes |
| [resolve](../src/adversarial_friends/assets/entrypoints/resolve/SKILL.md) | Named-run resolutions with supplied evidence |
| [modes](../src/adversarial_friends/assets/entrypoints/afriend/references/modes.md) | All four modes — `report`, `crossexam`, `gate`, `loop` — plus claim states, ceilings, and exit codes |

`afriend resume <run-id>` routes through `/afriend` to `afriend run --resume
<run-id>`; it is not a claim-resolution disposition and needs no evidence.

> These entrypoints live under `src/adversarial_friends/assets/` because they ship
> **inside the wheel** as package data — the runner resolves them at runtime
> via `importlib.resources`, and they are mirrored into `plugins/` for plugin
> loaders. Edit them there, never in the mirror.

---

## 🧠 Core concepts

| Document | What it covers |
|---|---|
| [ledger](../src/adversarial_friends/assets/entrypoints/afriend/references/ledger.md) | Claim, verdict, alias, and resolution records — and how to read `claims.jsonl` directly |
| [architecture/](architecture/README.md) | Diagrams: module architecture, run flow, claim lifecycle |

### The diagrams

- **[Module architecture](architecture/components.puml)** — which module owns
  what, and how a run threads through them.
- **[Run flow](architecture/run-flow.puml)** — how `afriend run` admits
  providers, warns when an artifact is doc-scoped, applies scoped authority,
  stages harnesses, preserves DNS under Linux confinement, dispatches a
  report fan-out, and records downgrades.
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
  explicit operations to focused skills and stable CLI commands.

Rendered PNG and SVG are committed alongside each source. Regenerate with
`make diagrams`.

---

## 🏛 Design history

| Document | What it covers |
|---|---|
| [design spec](superpowers/specs/2026-08-22-adversarial-friends-design.md) | The full design — including §19, the adversarial review history that produced it |
| [core runner plan](superpowers/plans/2026-08-22-adversarial-friends-core-runner.md) | The 14-task implementation plan the runner was built from |

> ⚠️ These two are **historical records**, not live documentation. They
> describe the design and plan as written at the time, including paths and a
> `bin/af` entry point that no longer exist — the tool now installs as the
> `afriend` console script. They are excluded from `ruff format` so their
> embedded code fences stay as originally written. Read them for *why*
> decisions were made; read the pages above for *what is true now*.

---

## 🎨 Assets

| Document | What it covers |
|---|---|
| [images/README.md](images/README.md) | Brand asset layout, sizes, and how to regenerate them |

---

## 🛠 Contributing

See [AGENTS.md](../AGENTS.md) for repository layout, the canonical-vs-mirror
rule for skill assets, and the quality gates `make quality` enforces.

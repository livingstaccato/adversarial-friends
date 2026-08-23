# 📚 Adversarial Friends — Documentation

Reference documentation for users and contributors. For the introduction and
quickstart, see the [top-level README](../README.md); come here for detail.

---

## 🚀 Start here

| Document | What it covers |
|---|---|
| [SKILL.md](../src/adversarial_friends/assets/SKILL.md) | The skill itself — when it fires, how to run it, how to read its output |
| [modes.md](../src/adversarial_friends/assets/references/modes.md) | `report`, `crossexam`, `gate`, `loop` — what each means and which are implemented |
| [troubleshooting.md](../src/adversarial_friends/assets/references/troubleshooting.md) | Verified CLI traps, empty reports, timeouts, unauthenticated friends |

> These three live under `src/adversarial_friends/assets/` because they ship
> **inside the wheel** as package data — the runner resolves them at runtime
> via `importlib.resources`, and they are mirrored into `plugins/` for plugin
> loaders. Edit them there, never in the mirror.

---

## 🧠 Core concepts

| Document | What it covers |
|---|---|
| [ledger.md](../src/adversarial_friends/assets/references/ledger.md) | Claim, verdict, alias, and resolution records — and how to read `claims.jsonl` directly |
| [architecture/](architecture/README.md) | Diagrams: module architecture, run flow, claim lifecycle |

### The three diagrams

- **[Module architecture](architecture/components.puml)** — which module owns
  what, and how a run threads through them.
- **[Run flow](architecture/run-flow.puml)** — every step of `afriend run
  --mode report`, including where each downgrade gets recorded.
- **[Claim lifecycle](architecture/claim-lifecycle.puml)** — how two friends
  finding the same defect become one corroborated claim without losing either
  attribution.

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

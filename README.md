![adversarial-friends](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/images/brand/adversarial-friends-banner.png)

# Adversarial Friends

> Hand your spec, plan, or review to **other** agent CLIs — `claude`, `codex`,
> `agy`, `opencode` — as independent adversarial reviewers, then merge their
> critiques into one ranked findings report.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-932-brightgreen)](tests/)

It automates a workflow you may already do by hand: run a review, paste the
findings into a different model, ask whether they hold up, carry the argument
back. Doing that manually means holding a claim ledger in your head. This
keeps the ledger on disk.

---

## 📋 Contents

- [Why more than one model](#-why-more-than-one-model)
- [Install](#-install)
- [Quickstart](#-quickstart)
- [How it works](#-how-it-works)
- [Lenses](#-lenses)
- [What you get back](#-what-you-get-back)
- [What's implemented](#-whats-implemented)
- [Documentation](#-documentation)
- [Development](#-development)

---

## 🎯 Why more than one model

A single reviewer produces confident prose. Several reviewers produce claims
that **can be compared** — and the disagreements are where the real problems
are.

This tool's own design spec was built exactly this way:

| Reviewer | Result |
|---|---|
| `codex` | 17 findings |
| `claude` | 15 findings, plus one marked `unproven` — *"lens leaks attribution"* |
| `agy` | independently reproduced two of `claude`'s findings, **and** caught a shared-worktree race neither of the other two flagged |

That `unproven` claim was later confirmed and fixed. No single reviewer's pass
would have surfaced all of it — see the [revision history in the design
spec](docs/superpowers/specs/2026-08-22-adversarial-friends-design.md#19-revision-history)
for the full account.

---

## 📦 Install

Requires **Python 3.11+** and at least one agent CLI besides the one you're
running under. The runner itself is **stdlib-only** — zero runtime
dependencies.

```bash
uv tool install adversarial-friends
```

<details>
<summary>Other install methods</summary>

```bash
# From git, for a version that is not yet released
uv tool install git+https://github.com/livingstaccato/adversarial-friends

# From a local checkout
git clone https://github.com/livingstaccato/adversarial-friends
cd adversarial-friends
uv tool install .

# Without installing at all
python -m adversarial_friends doctor
```

</details>

Then confirm what's actually available:

```bash
afriend doctor
```

```
agy        found        schema=True readonly=True effort=native /Users/you/.local/bin/agy
claude     found        schema=True readonly=True effort=native /Users/you/.local/bin/claude
codex      found        schema=True readonly=True effort=native /opt/homebrew/bin/codex
ollama     found        schema=False readonly=False effort=none http://127.0.0.1:11434/api/generate
opencode   found        schema=False readonly=False effort=unverified /Users/you/.opencode/bin/opencode
```

For `ollama`, `found` means a reachable endpoint rather than a binary on
`PATH`; it shows `unreachable` when no server is listening.

`doctor` reports what each friend can genuinely **enforce** — schema
validation, a real read-only mode, a verifiable effort level — rather than
what it claims to support. `opencode` showing `readonly=False` is not a bug;
it has no read-only mode, so the tool says so instead of pretending.

---

## 🚀 Quickstart

```bash
afriend run docs/my-design.md --mode report
```

**Stdout** carries one thing — the run directory. Read `report.md` inside it:

```bash
cat "$(afriend run docs/my-design.md --mode report)/report.md"
```

Pick your reviewers and lenses explicitly:

```bash
afriend run spec.md --friend codex:security --friend claude:ops
```

A third slot picks the model — required for `ollama`, which has no default:

```bash
afriend run spec.md --friend ollama:security:qwen3:0.6b
```

> ⚠️ `--friend` **replaces** discovery rather than adding to it. One
> `--friend` flag means a one-friend run — which cannot cross-examine
> anything. The tool records that as a downgrade in `run.json` and
> `report.md` rather than letting it look like a full review.

A run takes minutes, not seconds — a friend is a whole agent CLI reading a
document. Progress goes to **stderr**: a line per friend as it finishes, and
a heartbeat naming whatever is still outstanding, so a quiet run is
distinguishable from a hung one. `--no-progress` silences it for a caller
that captures both streams together.

---

## ⚙️ How it works

![module architecture](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/components.png)

Every friend gets its **own** prompt built from its **own** lens, runs in its
**own** isolated directory, in its **own** process group:

| Stage | What happens |
|---|---|
| 🔍 **Resolve** | Discover agent CLIs on `PATH`, round-robin a lens to each |
| ✍️ **Prompt** | Build a per-friend prompt: shared contract header + that friend's lens prose + the artifact |
| 🔒 **Isolate** | Friends with a real read-only mode get a private `git worktree` from one shared snapshot. A CLI with no read-only mode is confined by the OS instead (`sandbox-exec` / `bwrap`) — or refused |
| ⚡ **Dispatch** | Parallel, one thread per friend, each in its own process group with a kill deadline of `--timeout + 60s` |
| 🧩 **Normalize** | Unwrap the CLI's own JSON envelope, strip ANSI, recover the payload, validate against the claim schema |
| 🔗 **Merge** | Exact-merge identical claims into aliases — accumulating origins so corroboration survives |
| 📄 **Report** | Rank findings, render `report.md`, write the append-only ledger |

The snapshot includes **untracked** files (`git stash create` omits them), and
the working tree is never touched — a friend reviewing your repo can't see a
half-staged index or scribble on your checkout.

<details>
<summary>Full run flow, step by step</summary>

![run flow](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/run-flow.png)

</details>

### Corroboration is the point

Two friends independently reaching the same conclusion is the strongest signal
this tool produces, so deduplication is built to never destroy it:

![claim lifecycle](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/claim-lifecycle.png)

Dedup is **deliberately** exact-match — whitespace and case only. Two friends
describing one defect in different words produce two claims, which costs a
round. Guessing at equivalence would corrupt the ledger, which is worse.

### Claim states in cross-examination

Every claim `--mode crossexam` produces ends in one of eight states. Two of
them — `deadlocked` and `settled-upheld` — deliberately need a human, and the
report says so rather than quietly resolving them.

![crossexam states](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/crossexam-states.png)

### The gate loop

`--mode gate` is the one that fails a build, and clearing it is a
back-and-forth rather than a single command:

![gate workflow](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/gate-workflow.png)

---

## 🔬 Lenses

A lens is prose, not a config string. Its text is injected into that friend's
prompt, so it shapes what the friend actually looks for.

| Lens | Default scope | Requires a failure scenario |
|---|---|---|
| `assumptions` | doc | ✅ |
| `security` | repo | ✅ |
| `ops` | repo | ✅ |
| `testability` | repo | ✅ |
| `spec-vs-reality` | repo | ✅ |
| `scope` | doc | ❌ — advisory only |

`scope` is the one lens that doesn't demand a concrete failure scenario;
"this is more than you need" is a legitimate finding without one. Claims from
it are marked *(advisory)* in the report so they never carry the same weight
as a reproducible defect.

---

## 📂 What you get back

```
<run-dir>/
├── report.md          ← ranked findings, corroboration, downgrades
├── run.json           ← machine-readable: friends, statuses, downgrades
├── claims.jsonl       ← append-only ledger: claims, aliases
├── artifact/          ← frozen copy of what was reviewed, hashed
└── round-1/
    ├── <friend>.prompt ← exactly what this friend was asked
    ├── <friend>.raw    ← its unmodified stdout
    ├── <friend>.err    ← its stderr (always present, even when empty)
    ├── <friend>.meta   ← argv, exit code, duration, timeout, orphan status
    └── <friend>.sandbox ← the OS policy it ran under, when one was applied
```

Runs land under `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/`,
or wherever `--out` points.

Everything a friend was asked and everything it said is on disk. When a run
comes back thin, that's what you read — not a guess.

---

## ✅ What's implemented

**All four modes run.**

| Mode | What it does |
|---|---|
| `report` | One round. Every friend critiques in parallel; claims merge into one ranked report. |
| `crossexam` | Then friends judge the claims they did not write, blind, until each settles or deadlocks. |
| `gate` | Then every non-advisory claim that did not clear needs an explicit resolution — this is the one that fails a build. |
| `loop` | Repeats until two consecutive rounds surface nothing new. |

```bash
afriend run docs/design.md --mode crossexam
afriend run docs/design.md --mode gate       # exit 1 while anything blocks
afriend resolve <run-id> --claim c-0001@1 \
    --disposition fixed --evidence src/auth.py:38
```

Disagreement is the output rather than a problem: two judges who still
disagree at `--max-rounds` leave the claim `deadlocked`, and the report
quotes both sides verbatim instead of resolving it by majority.

A resolution is an **attestation**, and the tool says so. It cannot know a
defect is gone — only whether the location you named actually changed since
the run started. A fix that landed outside the reviewed artifact is fine; a
location it cannot reconstruct is recorded as `unverifiable` rather than
waved through. The one thing it refuses is `--disposition fixed` naming a
location that did not change.

Deduplication is judgment the runner declines to fake. `--merge exact`
(the default) merges only identical claims and always finishes unaided;
`--merge orchestrator` stops with exit `10`, writes the claims to
`REQUEST.json`, and waits for you to say which are duplicates:

```bash
afriend run docs/design.md --merge orchestrator   # exit 10, writes REQUEST.json
# ...fill in the merges, save as RESPONSE.json...
afriend run --resume <run-id>                     # round 1 is not re-run
```

Tired of `--friend` flags? `afriend init` writes a roster from what is
actually installed, and `~/.config/adversarial-friends/roster.toml` is picked
up automatically. A repo-local roster never is — a cloned repo does not get
to choose who reviews it (§13).

The same halt serves unparseable output (§14.2): repair is a pure
transformation with no model call, so when it fails the runner asks you to
read the raw text rather than discarding whatever the friend found.

**There is no `--max-spend-usd`.** A dollar cap needs per-CLI cost reporting
nobody has captured, and a flag that silently never fires is worse than none
— you would set it and believe you were protected. Use `--max-calls`, which
is derived from your roster and actually enforced.

| Friend | Status |
|---|---|
| `claude` | ✅ ships |
| `codex` | ✅ ships |
| `agy` | ✅ ships |
| `opencode` | ✅ ships — no read-only mode, reported honestly |
| `ollama` | ✅ ships — local models over HTTP, no schema/read-only to enforce; needs an explicit model |

There is no `gemini` adapter: the `gemini` CLI returns an ineligible-tier
error on the individual free tier, and Google's own supported path from there
is Antigravity — which is `agy`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | the run reached terminal states with nothing blocked |
| `1` | a `gate` still has claims needing a resolution, or every dispatched friend failed |
| `2` | usage error — bad flag, unknown CLI, missing artifact |
| `3` | no usable friends found at all |
| `10` | `--merge orchestrator` is waiting for you to adjudicate merges |
| `11` | a ceiling was hit — the run was truncated, not decided |
| `12` | `--require-friends N` was set and fewer than `N` friends answered |
| `128+N` | aborted by signal N — isolation torn down, friends killed |

A ceiling outranks everything below it, so a CI wrapper can read `11` as
"retry" and `1` as "block" without ambiguity.

---

## 📚 Documentation

| Where | What |
|---|---|
| [docs/](docs/README.md) | Documentation index |
| [SKILL.md](src/adversarial_friends/assets/SKILL.md) | The skill itself — when it fires, how to read its output |
| [modes.md](src/adversarial_friends/assets/references/modes.md) | `report`, `crossexam`, `gate`, `loop` — and which are real |
| [ledger.md](src/adversarial_friends/assets/references/ledger.md) | Claim, verdict, alias, and resolution records |
| [troubleshooting.md](src/adversarial_friends/assets/references/troubleshooting.md) | Verified CLI traps, empty reports, timeouts |
| [architecture/](docs/architecture/README.md) | Diagrams and their sources |
| [design spec](docs/superpowers/specs/2026-08-22-adversarial-friends-design.md) | The full design, including the adversarial review that produced it |

### Using it as a skill or plugin

The skill payload ships **inside the wheel** as package data, and is mirrored
under [`plugins/`](plugins/) for loaders that can't install a Python package:

```bash
# Claude Code
/plugin marketplace add /path/to/adversarial-friends/plugins
```

The skill invokes `afriend`, so the package must be installed for it to work —
`afriend doctor` is the check.

---

## 🛠 Development

```bash
make install    # uv sync
make test       # pytest — 932 tests
make quality    # lint + type-check + every sync gate + tests
make diagrams   # re-render docs/architecture/*.puml
```

`make quality` runs exactly what CI runs. Two gates catch drift that is
otherwise silent:

- **`plugin-sync`** — `src/adversarial_friends/assets/` is canonical; the
  `plugins/` tree is a byte-identical mirror. Edit assets, then
  `make plugin-sync-copy`.
- **`version-sync`** — `VERSION` must match the `version` field in every
  plugin manifest.

See [AGENTS.md](AGENTS.md) for repository layout and conventions.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

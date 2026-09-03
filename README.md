![adversarial-friends](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/images/brand/adversarial-friends-banner.png)

# Adversarial Friends

> Hand your spec, plan, or review to agent CLIs — `claude`, `codex`,
> Antigravity (`agy`),
> `opencode` — under adversarial lenses, then merge their critiques into one
> ranked findings report.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1999-brightgreen)](tests/)

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
| Antigravity (`agy`) | independently reproduced two of `claude`'s findings, **and** caught a shared-worktree race neither of the other two flagged |

That `unproven` claim was later confirmed and fixed. No single reviewer's pass
would have surfaced all of it — see the [revision history in the design
spec](docs/superpowers/specs/2026-08-22-adversarial-friends-design.md#19-revision-history)
for the full account.

---

## 📦 Install

Requires **Python 3.11+** and at least one agent CLI. Judging modes additionally
require two independent non-host friends. The runner itself is **stdlib-only**
— zero runtime dependencies.

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

`doctor` reports shared readiness — including `ready`,
`reachable-unconfigured`, `unavailable`, `disabled`, `host-excluded`, and
`policy-blocked` — plus what each friend can genuinely enforce. For Ollama,
reachability alone is insufficient because dispatch also requires a model.

---

## 🚀 Quickstart

In an agent host, select `/afriend` to route an explicit Adversarial Friends
request, or select `$adversarial-friends:afriend` directly. It hands review,
status, provider configuration, and resolution requests to focused skills:
`review`, `status`, `configure`, and `resolve`.

Conversational phrases such as `afriend review` and `afriend status` are
routing language, not executable aliases: the stable CLI commands remain
`afriend run` and `afriend doctor`.
`afriend resume <run-id>` similarly routes to `afriend run --resume <run-id>`;
it is not a claim resolution and needs no disposition or evidence.

```bash
afriend run docs/my-design.md --mode report
```

The host is the orchestrator. In Codex, Codex remains the orchestrator and is
included as a friend by default. The report labels it
`host-self-review (advisory)`, `independent=false`: it contributes findings
and advisory verdicts, but cannot satisfy two-independent-friend admission,
`--require-friends` participation, judging quorum, gate clearance, or loop
convergence. Judging modes need two independent non-host friends in addition
to any host; a `report` can be host-only. Non-Codex hosts are excluded by
default. `--include-self` and `--exclude-self` are mutually exclusive
overrides.

Manage persistent user defaults, including a default Ollama model, with:

```bash
afriend providers list
afriend providers enable claude
afriend providers disable opencode
afriend providers set-model ollama qwen3:8b
afriend providers clear-model ollama
```

For one automatic roster, `--enable-provider NAME` and
`--disable-provider NAME` override those defaults. Disabled providers are not
probed. An explicit `--friend` roster remains authoritative and may name the
host or a disabled provider.

External-tool authority is separate from provider selection. The repeatable
required-value form is `--allow-external-tools=PROVIDER`; the explicit global
grant is `--allow-external-tools=*`. Unknown, duplicate, or mixed `*` plus
provider grants are invalid, and the old valueless form is invalid.
`--unsafe-extra-args` requires the global `*` grant plus
`--i-accept-unsandboxed`. Grants do not change provider defaults and must be
repeated as the same normalized set on resume.

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
> `--friend` flag means a one-friend roster. A `report` with one friend is
> allowed as a recorded downgrade in `run.json` and `report.md` rather than
> being presented as a full review. `crossexam`, `gate`, and `loop` require at
> least two independent non-host friends; with fewer, they refuse with exit 3
> before a run directory is created.

Runtime depends on the slowest selected friend, document size, and mode.
Progress goes to **stderr**: a line per friend as it finishes, and a heartbeat
naming whatever is still outstanding, so a quiet run is distinguishable from
a hung one. `--no-progress` silences it for a caller that captures both
streams together; reducing `--timeout` turns slow friends into failures rather
than making them faster.

---

## ⚙️ How it works

![module architecture](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/architecture/components.png)

Every friend gets its **own** prompt built from its **own** lens, runs in its
**own** isolated directory, in its **own** process group:

| Stage | What happens |
|---|---|
| 🔍 **Resolve** | Discover agent CLIs on `PATH`, round-robin a lens to each |
| ✍️ **Prompt** | Build a per-friend prompt: shared contract header + that friend's lens prose + the artifact |
| 🔒 **Isolate** | Each friend's effective scope selects its isolation directory: repo scope gets a private `git worktree` from one shared snapshot, while doc scope gets an artifact-only directory. Adapter read-only flags and, where required, OS confinement (`sandbox-exec` / `bwrap`) are then applied separately as a second line of defense — or the friend is refused |
| 🛂 **Deny remote authority** | External tools are denied by default. An adapter that cannot neutralize provider-managed tools, plugins, apps, or MCP servers is `policy-blocked` unless `--allow-external-tools=PROVIDER` explicitly opts that provider in for this run |
| 🧰 **Stage harnesses** | Adapter-owned workspace assets are copied into each isolated run workspace. Antigravity receives the controlled `afriend-reviewer` agent selected with `--agent`, `--disable-slash-commands`, `--mode plan`, and `--sandbox` |
| ⚡ **Dispatch** | Parallel, one thread per friend, each in its own process group with a kill deadline of `--timeout + 60s` |
| 🧩 **Normalize** | Unwrap the CLI's own JSON envelope, strip ANSI, recover the payload, validate against the claim schema |
| 🔗 **Merge** | Exact-merge identical claims into aliases — accumulating origins so corroboration survives |
| 📄 **Report** | Rank findings, render `report.md`, write the append-only ledger |

The snapshot includes **untracked** files (`git stash create` omits them), and
the working tree is never touched — a friend reviewing your repo can't see a
half-staged index or scribble on your checkout.

Artifact location selects scope automatically. A path outside a Git repository
produces a visible doc-scope warning on stderr before friends start and friends
receive only its text; a path inside one receives a repository snapshot. Put
the artifact in the repository when the review needs code context. Normal
untracked, non-ignored files are included in a snapshot. Gitignored artifacts
are intentionally excluded and fail rather than falling back to a stale `HEAD`
version.

On Linux, a confined friend uses `bwrap` with the required system paths
read-only. If `/etc/resolv.conf` resolves to a safe regular file elsewhere on
the host, the sandbox exposes that resolver target read-only too. DNS therefore
continues to work without granting general access to the host filesystem.

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
| `report` | The default: one critique fan-out. Every friend critiques in parallel; claims merge into one ranked report. |
| `crossexam` | Then friends judge the claims they did not write, blind, for three total rounds by default or until each settles or deadlocks. |
| `gate` | Then every non-advisory claim that did not clear needs an explicit resolution — this is the one that fails a build. |
| `loop` | Repeats for a maximum of five iterations by default, until two consecutive dry rounds surface nothing new. |

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
the run started. A fix that landed outside the reviewed artifact is fine.
`--disposition fixed` requires a verifiably changed location; unchanged or
unverifiable evidence is refused. Use `accepted-risk` when verification is
intentionally unavailable.

Deduplication is judgment the runner declines to fake. `--merge exact`
(the default) merges only identical claims and always finishes unaided;
`--merge orchestrator` stops with exit `10`, writes the claims to
`REQUEST.json`, and waits for you to say which are duplicates:

```bash
afriend run docs/design.md --merge orchestrator   # exit 10, writes REQUEST.json
# ...fill in the merges, save as RESPONSE.json...
afriend run --resume <run-id>                     # round 1 is not re-run
```

Resume verifies the original frozen artifact hash and saved Git snapshot
before dispatch; it never substitutes current files. Security grants are
also never restored from `run.json`: options such as
`--allow-external-tools=PROVIDER` grants (or the global `=*` grant) must be
repeated as the same normalized set. For 0.2.0 metadata, the
report says external authority is `legacy-unknown` instead of inventing a
historical guarantee.

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

Antigravity's controlled reviewer is staged into the run's isolated workspace;
it does not edit global Antigravity configuration. The staged agent and
`--sandbox` are defense in depth, but sandbox does not mean external tools
were denied. Antigravity remains `external_tools=uncontrolled` because its
CLI cannot prove every plugin/MCP integration disabled invocation-locally.
That is an accepted
best-effort limitation: Antigravity is policy-blocked by default, while
`--allow-external-tools=agy` records it as `explicitly-allowed`.

There is no `gemini` adapter: the `gemini` CLI returns an ineligible-tier
error on the individual free tier, and Google's own supported path from there
is Antigravity — which is `agy`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | the run reached terminal states with nothing blocked |
| `1` | a `gate` still has claims needing a resolution, or every dispatched friend failed |
| `2` | usage error — bad flag, unknown CLI, missing artifact |
| `3` | no usable friends found, or a judging mode resolved fewer than two independent non-host friends; refused before a run directory |
| `10` | `--merge orchestrator` is waiting for you to adjudicate merges |
| `11` | a ceiling was hit — including natural `--max-loop-iterations` exhaustion without convergence; the run was truncated, not decided |
| `12` | `--require-friends N` was set and fewer than `N` friends answered |
| `128+N` | aborted by signal N — isolation torn down, friends killed |

A ceiling outranks everything below it, so a CI wrapper can read `11` as
"retry" and `1` as "block" without ambiguity.

---

## 📚 Documentation

| Where | What |
|---|---|
| [docs/](docs/README.md) | Documentation index |
| [/afriend router](src/adversarial_friends/assets/entrypoints/afriend/SKILL.md) | Explicit product router and review workflow |
| [review](src/adversarial_friends/assets/entrypoints/review/SKILL.md) | Start and interpret a review run |
| [status](src/adversarial_friends/assets/entrypoints/status/SKILL.md) | Read-only provider and named-run status |
| [configure](src/adversarial_friends/assets/entrypoints/configure/SKILL.md) | Explicit provider-default changes |
| [resolve](src/adversarial_friends/assets/entrypoints/resolve/SKILL.md) | Named-run claim resolutions |
| [modes](src/adversarial_friends/assets/entrypoints/afriend/references/modes.md) | `report`, `crossexam`, `gate`, and `loop` |
| [architecture/](docs/architecture/README.md) | Diagrams and their sources |
| [design spec](docs/superpowers/specs/2026-08-22-adversarial-friends-design.md) | The full design, including the adversarial review that produced it |

### Using it as a skill or plugin

The skill payload ships **inside the wheel** as package data, and is mirrored
under [`plugins/`](plugins/) for loaders that can't install a Python package:

```bash
# Claude Code
/plugin marketplace add /path/to/adversarial-friends/plugins
```

Plugins package capabilities; the Adversarial Friends plugin provides exactly
five skills: `/afriend`, `review`, `status`, `configure`, and `resolve`.
`/afriend` is the only router and short slash selector; direct qualified
selection is `$adversarial-friends:afriend`. The CLI never runs automatically
by itself. Generic “review this,” “poke holes,” “second opinion,” and
architectural decision requests stay ordinary Codex work.

The package must therefore be installed for the skill to work — `afriend
doctor` is the check. Conversational forms route to stable commands; they are
not executable aliases.

---

## 🛠 Development

```bash
make install    # uv sync
make test       # pytest
make quality    # every portable CI gate, wheel checks, and tests
make diagrams   # re-render docs/architecture/*.puml
```

`make quality` runs every portable CI gate, including wheel construction and
isolated installation. Linux CI additionally installs bubblewrap and requires
the real OS-confinement tests to execute; macOS cannot reproduce that Linux-
specific assertion locally. Use `make act-ci` for the closest local Linux run.

Two gates catch drift that is otherwise silent:

- **`plugin-sync`** — `src/adversarial_friends/assets/` is canonical; its
  entrypoints project directly to plugin skills and runtime assets project
  beneath `skills/afriend/`. Edit assets, then `make plugin-sync-copy`.
- **`version-sync`** — `VERSION` must match the `version` field in every
  plugin manifest.

See [AGENTS.md](AGENTS.md) for repository layout and conventions.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

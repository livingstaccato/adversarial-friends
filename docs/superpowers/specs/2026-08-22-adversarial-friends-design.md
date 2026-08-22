# Adversarial Friends — Design

Date: 2026-08-22
Status: Approved for planning

## 1. Problem

The current manual workflow:

1. Run a `/code-review`-style tool (reviewer A). Collect findings.
2. Paste those findings to a different model (reviewer B) with a prompt like
   *"another llm said this, evaluate it for accuracy/relevance"*.
3. Take B's response back to A: *"the other llm said…"*.
4. Repeat a few times until it feels settled.

This is manual cross-examination with a claim ledger held in the operator's head.
It works, and it is the thing to automate. It has four specific defects:

| Defect | Consequence |
|---|---|
| **Source is named** (*"another llm said"*) | Primes deference or reflexive contrarianism. Neither is judgment. |
| **Whole output pasted as a blob** | The judge grades a lump. It cannot refute claim 3 while upholding claim 7. |
| **No verdict vocabulary** (*"evaluate for accuracy/relevance"*) | Judge free-associates. No evidence requirement, no way to say "I can't tell". |
| **No memory across rounds** | Settled ground gets relitigated. Context balloons. The operator arbitrates by fatigue. |

Adversarial Friends automates the loop and fixes all four.

## 2. Goals

- Automate multi-round cross-examination between heterogeneous coding agents.
- Produce a claim ledger with per-claim verdicts and provenance, not a prose blob.
- Distinguish *settled* from *deadlocked*. A deadlock between two strong models is
  a signal worth human attention, not a failure to converge.
- Run under any harness (Claude Code, Codex, Gemini CLI, opencode, Antigravity),
  not only Claude Code.
- Work with zero configuration on a fresh machine; allow every layer to be overridden.

## 3. Non-goals

- Not a code review tool. It challenges artifacts — specs, plans, and *the output of
  other review tools*. It does not generate the first review.
- Not a fixer. It does not edit the artifact. `loop` mode hands revisions back to
  the orchestrator.
- No live model calls in CI.

## 4. Architecture

Two layers, hard split.

**Runner (`bin/af`)** — deterministic, harness-agnostic, Python stdlib only.
Owns: roster resolution, capability probing, parallel spawn, sandbox flags,
timeouts, session-id reuse, JSON parse and repair, the claim ledger, run-dir
persistence.

**SKILL.md** — judgment. Owns: lens selection for this artifact, claim merge and
dedup, severity calibration, deadlock interpretation, presentation.

Mechanical work lives in code so it is reproducible. Judgment lives in prose so it
is good. The runner never decides whether two claims are the same claim — that is a
judgment call and it goes to the orchestrating model.

Stdlib-only is a hard constraint: the runner must execute under any harness on any
box without an install step. This sets a floor of Python 3.11 (`tomllib` for adapter
and roster parsing). If 3.11 proves too high a bar, adapters move to JSON and the
floor drops to 3.9.

## 5. The attribution fix

Design responses to the four defects in §1:

- **Blind by default.** Claims reach a judge as *"the following claims were made
  about this artifact"*, with no model names. `--attributed` opts back in.
- **One verdict per claim**, referenced by stable claim id.
- **Forced verdict vocabulary**: `upheld`, `refuted`, `amended`, `unproven`,
  `out-of-scope`.
- **Evidence required for refutation.** A `refuted` verdict with no
  `counter_evidence` is automatically downgraded to `unproven`.
- **Ledger, not chat.** Round N+1 shows only still-contested claims plus prior
  verdicts. Settled claims drop out of the prompt.

The `unproven` verdict and the evidence requirement are the two largest quality
levers. Free-form review never says "I cannot tell" — it always produces confident
prose. Forcing that option available is most of the accuracy gain.

## 6. Claim ledger

Append-only `claims.jsonl`. Two record types.

Claim:

```json
{"id":"c-0007","origin":"codex/ops","lens":"ops","round":1,
 "severity":"high","claim":"one sentence",
 "location":"src/auth.py:42","evidence":"file:line or quote",
 "failure_scenario":"concrete inputs -> wrong outcome",
 "suggested_fix":"..."}
```

Verdict:

```json
{"claim_id":"c-0007","judge":"claude/security","round":2,
 "verdict":"refuted","confidence":"high",
 "reasoning":"...","counter_evidence":"src/auth.py:38 already guards this",
 "amended_claim":null}
```

`failure_scenario` is required on every claim. A claim that cannot name concrete
inputs leading to a wrong outcome is an opinion; the runner marks it
`unsubstantiated` before any judge sees it.

**Known tension:** this is strict enough to suppress vague-but-sometimes-right
claims from lenses like YAGNI and scope. Mitigation: lenses declare
`requires_failure_scenario = false`, and claims from those lenses enter the ledger
flagged `advisory`. Advisory claims are cross-examined but never block a `gate`.

## 7. Modes

| Mode | Behavior | Terminates on |
|---|---|---|
| `report` | Round 1 only. All friends critique in parallel. Merge, dedup, rank. | 1 round |
| `crossexam` **(default)** | `report`, then friends verdict each other's claims. Round 3+ revisits only contested claims. | Convergence, deadlock, or max-rounds (default 3) |
| `gate` | `crossexam`, then every surviving non-advisory claim needs explicit resolution (fixed / rejected-with-reason). Nonzero exit while unresolved. | All claims resolved |
| `loop` | `crossexam`, artifact revised by orchestrator, re-run until 2 rounds surface nothing new. | 2 dry rounds |

`crossexam` is the default because it is the workflow being replaced. `report` is
available for a cheap first pass.

### Termination semantics

- A claim is **settled** when all judges agree (`upheld` or `refuted`), or when it
  was `amended` and the amendment was subsequently `upheld`.
- A claim is **deadlocked** when it survives `max_rounds` with split verdicts.
- A round is **dry** when it produces no claims not already in the ledger.
- A run ends when no contested claims remain, `max_rounds` is hit, or (in `loop`)
  two consecutive dry rounds occur.

Deadlocked claims are reported **as deadlocks, with both sides quoted verbatim**.
Never silently resolved by majority or by orchestrator preference.

## 8. Roster resolution

1. Probe `$PATH` for known binaries.
2. Detect the host harness from environment (`CLAUDECODE`, `CODEX_*`, Gemini and
   opencode equivalents) and **drop it from the roster**. `--include-self` overrides.
3. Assign lenses round-robin from `lenses/*.md`.
4. Any layer present in `.adversarial-friends/` overrides the corresponding default.

Self-exclusion default is a judgment call, not a rule: Codex judging Codex output
under a different lens and effort level is sometimes exactly right. The flag exists
for that.

## 9. Lenses

Lenses are markdown files in `lenses/`, not config strings. A lens is a page of
prose describing what to look for and what counts as evidence. Cramming that into a
TOML value guarantees nobody edits it.

Shipped lenses: `assumptions`, `security`, `ops`, `scope`, `testability`,
`spec-vs-reality`.

Lens frontmatter:

```yaml
name: ops
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: repo
```

## 10. Per-friend tuning

Every friend is independently tunable. Roster entry:

```toml
[[friend]]
name       = "codex-ops"
cli        = "codex"
lens       = "ops"
model      = "gpt-5.6-sol"
effort     = "xhigh"        # normalized; mapped per adapter
scope      = "repo"         # repo | doc
timeout    = 300
profile    = "review"       # codex-only: names a config.toml profile
budget_usd = 2.00           # where supported
extra_args = ["-c", "shell_environment_policy.inherit=all"]
```

Normalized `effort` maps per adapter. Verified 2026-08-22:

| Normalized | claude | codex | opencode | gemini |
|---|---|---|---|---|
| model | `--model` | `-m`, or `-p <profile>` | `-m provider/model` | `-m` |
| low | `--effort low` | `-c model_reasoning_effort=low` | `--variant minimal` | unsupported |
| medium | `--effort medium` | `-c model_reasoning_effort=medium` | `--variant medium` * | unsupported |
| high | `--effort high` | `-c model_reasoning_effort=high` | `--variant high` | unsupported |
| xhigh | `--effort xhigh` | `-c model_reasoning_effort=xhigh` | unsupported | unsupported |
| max | `--effort max` | unsupported | `--variant max` | unsupported |
| fallback model | `--fallback-model` | unsupported | unsupported | unsupported |
| budget cap | `--max-budget-usd` | unsupported | unsupported | unsupported |
| show thinking | via `stream-json` | via `--json` events | `--thinking` | via `stream-json` |

\* opencode's `--variant` is provider-specific and its accepted values are not a fixed
set. Only `minimal`, `high`, and `max` appear in its help text; `medium` is assumed
and must be probed per provider before the adapter claims it.

`extra_args` is a raw passthrough escape hatch, appended last, unvalidated.

**Unsupported knobs never fail silently.** Requesting `effort = "max"` on codex
produces a recorded downgrade in `run.json` and a line in the report header.

## 11. Adapters

One declarative record per CLI in `adapters/`. Adding a friend is adding a record.
Verified locally 2026-08-22 (claude 2.1.240, codex 0.149.0, gemini, opencode);
`agy` is unverified and web-sourced.

| Friend | Invoke | Read-only | Structured output | Resume |
|---|---|---|---|---|
| claude | `-p --output-format json --json-schema <f>` | `--permission-mode plan` | native JSON Schema validation | `--session-id <uuid>`, `-r`, `--fork-session` |
| codex | `exec --json --output-schema <f> -o <out>` | `-s read-only` | native schema | `codex resume`, `codex fork` |
| gemini | `-p -o json` | `--approval-mode plan` | prompt-level contract | `--session-id`, `-r` |
| opencode | `run --format json` | **none** | prompt-level contract | `-s <id>`, `-c`, `--fork` |
| agy (antigravity) | `-p --output-format json` | policy-based | prompt-level contract | unknown |

Special case worth using: `codex review --base <branch>` / `--uncommitted` /
`--commit <sha>` accepts custom instructions on stdin and is purpose-built for diff
artifacts.

**Not usable, for the record:** `claude mcp serve` exposes Claude Code's *toolbox*
(26 tools — `Read`, `Bash`, `Agent`, `Skill`, …) to a host. It does not expose the
agent as a callable tool. Only `codex mcp-server` offers genuine agent-as-a-tool
(`codex`, `codex-reply`). Gemini and opencode are MCP clients only. A uniform MCP
transport is therefore impossible, and unnecessary: every friend resumes sessions
by id at the CLI, so shell-out with session reuse is the single transport.

### Capability probing

At roster-resolve time each friend gets a capability set `{schema, readonly,
resume, effort}`. A missing capability produces a documented downgrade, recorded in
`run.json` and printed in the report header. Never silent.

opencode has no read-only mode. It therefore defaults to `scope = "doc"`. Setting
`scope = "repo"` on opencode requires an explicit opt-in flag, because it means an
agent with write capability in the working tree.

## 12. Run directory

```
.adversarial-friends/runs/<run-id>/
  run.json          # config snapshot: roster, capabilities, downgrades, lenses, mode, artifact hash
  artifact/         # frozen copy of what was challenged
  round-N/
    <friend>.raw    # exact stdout
    <friend>.json   # normalized
    <friend>.meta   # argv, exit code, duration, session id, cost
  claims.jsonl
  report.md
```

The artifact is copied and hashed at run start. Friends execute at different times
and the artifact must not shift under them. Raw stdout is always preserved next to
the normalized form — when normalization is wrong, the original is what you need.

## 13. Failure handling

| Condition | Behavior |
|---|---|
| CLI not on `$PATH` | Skip, note in report header |
| Auth failure (nonzero exit + stderr match) | Skip, note, print the remediation command |
| Timeout (default 300s) | Kill, keep partial output, mark run incomplete |
| Malformed JSON | One repair attempt re-prompting with the parse error; then preserve raw and let the orchestrator extract |
| Zero friends available | **Hard error** |

The zero-friends case is a hard error specifically because an empty report reads
like "no problems found".

## 14. Multi-harness packaging

Follows the superpowers layout, which is the working precedent for a single repo
installing into Claude Code, Gemini CLI, Codex, and opencode.

```
adversarial-friends/
  skills/adversarial-friends/SKILL.md   # shared judgment layer
  lenses/*.md
  bin/af                                # runner, stdlib only
  adapters/*.toml
  .claude-plugin/plugin.json
  gemini-extension.json
  skill.json                            # codex
  .opencode/
  AGENTS.md                             # codex/opencode fallback
```

`SKILL.md` is the shared format across Claude Code, Codex (`~/.codex/skills/<name>/SKILL.md`),
and Gemini (`gemini skills install`). Per-harness manifests are thin shims over the
same skill and runner.

## 15. Testing

- Recorded-fixture tests per adapter: canned CLI stdout in, expected normalized
  claims out. Covers every documented downgrade path.
- Mode-driver tests against a fake friend binary emitting scripted claims and
  verdicts. Covers settle, deadlock, dry-round, and max-rounds termination.
- No live model calls in CI. Convergence logic must be deterministically testable.

## 16. Risks

1. **`agy` is unverified.** Every antigravity flag is web-sourced, not probed. The
   adapter ships marked experimental until someone runs it.
2. **Prompt-level JSON contracts drift.** Gemini, opencode, and agy have no native
   schema validation. The repair path is one retry; beyond that, normalization
   quality depends on the orchestrator.
3. **Blind presentation is unverified as an improvement.** It is well-motivated —
   removing source attribution should reduce deference and contrarianism — but it
   has not been measured against the attributed baseline. `--attributed` exists
   partly so the comparison can be run.
4. **Cost scales multiplicatively.** friends x lenses x rounds. Mitigated by
   per-friend budget caps where supported, `--max-friends`, and the round cap; not
   eliminated.

# Adversarial Friends — Design

Date: 2026-08-22
Version: 2 (see §19 for revision history)
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
- Distinguish *settled* from *deadlocked* from *incomplete*. A deadlock between two
  strong models is a signal worth human attention; an incomplete run is not a result
  at all.
- Run under any harness (Claude Code, Codex, Gemini CLI, opencode, Antigravity),
  not only Claude Code.
- Require no configuration file to run. This is not the same as running usefully
  with a single agent CLI installed — see §8.1.

## 3. Non-goals

- Not a code review tool. It challenges artifacts — specs, plans, and *the output of
  other review tools*. It does not generate the first review.
- Not a fixer. It does not edit the artifact. `loop` mode hands revisions back to
  the orchestrator.
- No live model calls in CI.

## 4. Architecture

Two layers, hard split.

**Runner (`bin/af`)** — deterministic, harness-agnostic, Python stdlib only.
Owns: roster resolution, capability probing, isolation, parallel spawn, sandbox
flags, timeouts, JSON parse and repair, the claim ledger, termination arithmetic,
run-dir persistence.

**SKILL.md** — judgment. Owns: lens selection for this artifact, claim merge
decisions, severity calibration, deadlock interpretation, presentation.

Mechanical work lives in code so it is reproducible. Judgment lives in prose so it
is good.

Stdlib-only is a hard constraint: the runner must execute under any harness on any
box without an install step. This sets a floor of Python 3.11 (`tomllib` for adapter
and roster parsing). If 3.11 proves too high a bar, adapters move to JSON and the
floor drops to 3.9.

### 4.1 Rounds are stateless

No friend session is ever resumed. Each friend invocation in each round is a fresh
process receiving exactly three inputs: the frozen artifact, the contested ledger
slice rendered blind, and its lens.

This is a deliberate reversal. Session reuse looked cheaper, but it contradicts §5:
a resumed session still contains the full prior transcript, so pruning settled
claims from the *prompt* prunes nothing from the friend's actual context. Reuse also
lets a stale pre-revision artifact persist across a `loop` iteration. Stateless
rounds cost input tokens and buy the property the whole design rests on — that what
a friend sees in round N is exactly what the ledger says it sees.

Consequence: `resume` is removed from the capability set. No adapter needs it.

### 4.2 Runner and orchestrator exchange work through the run directory

Two operations require model judgment mid-run: deciding whether two claims are the
same claim (§7.3), and extracting claims from output that failed to parse (§14).
The runner is a separate process and cannot perform either.

The runner therefore **halts with a resumable state**:

```
af run …                       → exit 10, writes round-N/REQUEST.json
<orchestrator does the judgment>  writes round-N/RESPONSE.json
af run --resume <run-id>       → continues from that point
```

Exit code 10 means *needs orchestrator*, not *error*. `SKILL.md` teaches the loop.
Given the same `RESPONSE.json`, the runner behaves identically — so mode drivers stay
deterministic and every fixture test can ship a canned response. One mechanism
covers both operations.

## 5. The attribution fix

Design responses to the four defects in §1:

- **Blind by default.** Claims reach a judge as *"the following claims were made
  about this artifact"*, with no model names. `--attributed` opts back in.
- **One verdict per claim**, referenced by exact versioned claim id.
- **Forced verdict vocabulary**: `upheld`, `refuted`, `amended`, `unproven`,
  `out-of-scope`.
- **Every dispositive verdict must engage the claim's evidence** (§6.4).
- **Ledger, not chat.** Round N+1 shows only still-contested claims plus prior
  verdicts. Settled claims are absent from the prompt — and, because rounds are
  stateless (§4.1), absent from the friend's context entirely.

The `unproven` verdict and the evidence requirement are the two largest quality
levers. Free-form review never says "I cannot tell" — it always produces confident
prose. Making that option available and legitimate is most of the accuracy gain.

## 6. Claim ledger

Append-only `claims.jsonl`. Four record types.

### 6.1 Claim

```json
{"type":"claim","id":"c-0007@1","supersedes":null,
 "origin":"codex/ops","lens":"ops","round":1,
 "severity":"high","claim":"one sentence",
 "location":"src/auth.py:42","evidence":"file:line or quote",
 "failure_scenario":"concrete inputs -> wrong outcome",
 "suggested_fix":"..."}
```

Claim ids are **versioned**: `c-0007@1`. An amendment creates `c-0007@2` with
`supersedes: "c-0007@1"`. Every verdict must name the exact version it judges. A
verdict naming a superseded version is retained in the ledger but excluded from the
tally — otherwise a judge upholding the broad original after another judge narrowed
it would report the broad claim as upheld.

`failure_scenario` and `evidence` are both required and both must be non-empty. A
claim missing either is marked `unsubstantiated` and never reaches a judge.

**Known tension:** this is strict enough to suppress vague-but-sometimes-right
claims from lenses like YAGNI and scope. Mitigation: lenses declare
`requires_failure_scenario = false`, and claims from those lenses enter the ledger
flagged `advisory`. Advisory claims are cross-examined but never block a `gate`.

### 6.2 Verdict

```json
{"type":"verdict","claim_id":"c-0007@1","judge":"claude/security","round":2,
 "verdict":"refuted","confidence":"high",
 "evidence_assessment":"disputed",
 "reasoning":"...","counter_evidence":"src/auth.py:38 already guards this",
 "amended_claim":null}
```

### 6.3 Alias

The orchestrator's merge decisions, made durable (§7.3):

```json
{"type":"alias","canonical":"c-0003@1","duplicate":"c-0011@1",
 "round":2,"rationale":"same timeout-orphan failure, different wording"}
```

### 6.4 Resolution

Required for `gate` mode to terminate (§7.5):

```json
{"type":"resolution","claim_id":"c-0007@2","disposition":"fixed",
 "author":"tim","evidence":"commit 3ec2220 adds the guard",
 "artifact_hash_after":"sha256:…","round":3}
```

`disposition` is `fixed`, `rejected`, or `accepted-risk`. A `fixed` resolution whose
`artifact_hash_after` equals the artifact hash recorded at run start is rejected —
nothing changed, so nothing was fixed.

### 6.5 Evidence symmetry

Every **dispositive** verdict (`upheld`, `refuted`, `amended`) carries
`evidence_assessment`:

| Value | Meaning | Effect |
|---|---|---|
| `confirmed` | Judge located the claim's cited evidence and it says what the claim says | Verdict stands |
| `disputed` | Judge located it and it does not support the claim | Verdict stands; `counter_evidence` required |
| `unverifiable` | Judge could not locate or evaluate the cited evidence | **Verdict downgraded to `unproven`** |

This closes the asymmetry where a refutation needed evidence but an endorsement did
not. It is deliberately weaker than requiring independent fresh evidence for every
`upheld` — the claim already had to carry evidence to enter the ledger, so the gap
was validation, not absence.

## 7. Modes and termination

| Mode | Behavior | Terminates on |
|---|---|---|
| `report` | Round 1 only. All friends critique in parallel. Merge, dedup, rank. | 1 round |
| `crossexam` **(default)** | `report`, then friends verdict each other's claims. Round 3+ revisits only contested claims. | Convergence, deadlock, max-rounds, or ceiling |
| `gate` | `crossexam`, then every surviving non-advisory claim needs a Resolution record. Nonzero exit while any remain. Defaults to `--preset thorough` (§10.1). | All claims resolved |
| `loop` | `crossexam`, artifact revised by orchestrator, re-run until two dry rounds. | Two dry rounds, or ceiling |

`crossexam` is the default because it is the workflow being replaced. `gate` is the
one mode that overrides the default preset — it blocks a human, so cheap effort is
false economy. An explicit `--preset` still wins.

### 7.1 Judge set and quorum

For each claim, the **required judges** are every friend in the roster except the
claim's originator.

The originator holds a **standing implicit `upheld` vote** at its stated confidence.
Without this, a two-friend roster can never deadlock: A raises a claim, B is the
only judge, B refutes, "all judges agree" is trivially true, and A's position —
the entire reason the claim exists — is discarded.

**Quorum** is two dispositive verdicts, the originator's implicit vote included.

### 7.2 Claim states

Dispositive verdicts are `upheld`, `refuted`, `amended`. Non-dispositive are
`unproven`, `out-of-scope`, and *missing* (friend timed out, failed auth, or failed
normalization).

| State | Condition | Passes a gate? |
|---|---|---|
| `settled-upheld` | Quorum met; all dispositive verdicts `upheld` | No — needs Resolution |
| `settled-refuted` | Quorum met; all dispositive verdicts `refuted` | Yes |
| `superseded` | Amended, and the successor version reached a terminal state | n/a |
| `contested` | Quorum met; dispositive verdicts disagree; rounds remain | No |
| `deadlocked` | Still contested at `max_rounds` | No |
| `unproven` | Fewer than two dispositive verdicts; all present verdicts non-dispositive | No |
| `incomplete` | Fewer than two dispositive verdicts because required judges are missing | No |

Only `settled-refuted` and claims carrying a Resolution clear a gate. `deadlocked`,
`unproven`, and `incomplete` never do. `incomplete` is not a soft `unproven` — it
means the run did not happen properly, and it is reported as a run-level failure,
not a finding.

Deadlocked claims are reported **as deadlocks, with both sides quoted verbatim**.
Never silently resolved by majority or by orchestrator preference.

### 7.3 Merge and dry rounds

Dry-round detection needs to know which claims are new, which needs deduplication,
which is judgment the runner is forbidden to make. The runner therefore halts at the
end of each round with a merge REQUEST (§4.2); the orchestrator returns Alias
records; the runner resumes. Alias records are durable and replayed in fixtures, so
the mode driver stays deterministic.

A round is **dry** only when both hold:

1. Every required friend completed successfully — no timeout, no auth failure, no
   normalization failure.
2. No claim entered the ledger that is not an alias of an existing claim.

A round with any friend failure is **failed**, not dry, and does not advance the
dry-round counter. Without this, every friend timing out twice reads as convergence.
Likewise, a claim repeatedly re-raised and never resolved does not make a round dry —
it is an alias of an unresolved claim, and unresolved non-advisory claims block
`loop` termination independently.

### 7.4 Global ceilings

`crossexam`'s `max_rounds` bounds one cross-examination. It does not bound `loop`,
which re-enters cross-examination after every revision. If each revision surfaces one
genuinely new finding, two consecutive dry rounds never occur.

Mandatory ceilings, all with defaults, all recorded in `run.json`:

| Ceiling | Default |
|---|---|
| `--max-rounds` (per crossexam) | 3 |
| `--max-loop-iterations` | 5 |
| `--max-wall-clock` | 3600s |
| `--max-calls` | 60 |
| `--max-spend-usd` | unset; enforced natively only where supported, estimated elsewhere |

Hitting any ceiling produces exit status `budget-exhausted`. This is neither success
nor convergence, and the report says so in the header. Per-friend budget caps are
available only on claude (`--max-budget-usd`); the other ceilings are the portable
guarantee.

### 7.5 Gate resolution

`gate` exits zero only when every non-advisory claim not in `settled-refuted` carries
a Resolution record (§6.4). Resolutions are submitted with:

```
af resolve <run-id> --claim c-0007@2 --disposition fixed --evidence "..."
```

The runner validates the artifact hash for `fixed` dispositions and appends the
record. It does not edit artifacts (§3) — it verifies that something else did.

## 8. Roster resolution

1. Probe `$PATH` for known binaries.
2. Detect the host harness from environment (`CLAUDECODE`, `CODEX_*`, Gemini and
   opencode equivalents) and **drop it from the roster**. `--include-self` overrides.
3. Assign lenses round-robin from `lenses/*.md`.
4. Apply user-level config, then repo-local config if and only if it is trusted (§13).

Self-exclusion default is a judgment call, not a rule: Codex judging Codex output
under a different lens and effort level is sometimes exactly right. The flag exists
for that.

### 8.1 Degraded single-friend mode

Zero-configuration is a goal (§2); working usefully with one installed CLI is not.
On a machine with only the host CLI, self-exclusion leaves an empty roster.

- `report` runs in **degraded single-friend mode**: the host is re-included, the
  report header states prominently that cross-examination did not occur and the
  output is a single perspective, and the run exits zero.
- `crossexam`, `gate`, and `loop` **hard-error** with remediation naming the
  supported CLIs. Cross-examination with one participant is not a degraded result;
  it is a different and much weaker thing wearing the same name.

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

```toml
[[friend]]
name       = "codex-ops"       # ^[a-z0-9][a-z0-9_-]{0,31}$ — see §13
cli        = "codex"
lens       = "ops"
model      = "gpt-5.6-sol"
effort     = "xhigh"
scope      = "repo"            # repo | doc
timeout    = 300
profile    = "review"          # codex-only: names a config.toml profile
budget_usd = 2.00              # where supported
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

\* opencode's `--variant` is provider-specific and its accepted values are not a fixed
set. Only `minimal`, `high`, and `max` appear in its help text; `medium` is assumed
and must be probed per provider before the adapter claims it.

`extra_args` is a passthrough escape hatch, appended last, and **subject to the deny
list in §13** — it is not unvalidated.

**Unsupported knobs never fail silently.** Requesting `effort = "max"` on codex
produces a recorded downgrade in `run.json` and a line in the report header.

### 10.1 Defaults resolution

Four layers. Later layers win.

```
1. the friend's own config      <- default: emit no model/effort flags at all
2. preset                        (--preset <name>)
3. roster override               (.adversarial-friends/friends.toml, if trusted)
4. invocation flag               (--effort max, --model ...)
```

**The default is to inherit, not to override.** Each CLI already carries a model and
effort its owner chose deliberately — `~/.codex/config.toml` on the development
machine reads `model = "gpt-5.6-sol"`, `model_reasoning_effort = "high"`. A tool
that silently overrides that produces surprise behavior and surprise cost, and
inheriting is the only policy that is correct on a machine the author has never
seen.

| Preset | Behavior |
|---|---|
| `inherit` **(default, except `gate`)** | Emits no model or effort flags. Every friend runs as its owner configured it. |
| `thorough` **(default for `gate`)** | Maximum *available* effort per friend, strongest available model per friend. |
| `cheap` | Low effort, fast models. For `report`-mode sanity passes. |

**`thorough` is inherently uneven and must say so.** Gemini exposes no effort flag;
opencode's `--variant` values are provider-specific. So `thorough` means "the
maximum this particular friend supports", which is not a level playing field. The
report header must state the model and effort each friend actually received.
Without that, a weak critique from a friend that silently ran at default effort
reads as a signal about the artifact when it is a signal about the flag matrix.

No blocking first-run wizard. It breaks headless and CI invocation, and first-run
interrogation is a common reason tools get uninstalled. `af init` (§17) covers the
same need on demand.

## 11. Adapters

One declarative record per CLI in `adapters/`. Adding a friend is adding a record.
Verified locally 2026-08-22 (claude 2.1.240, codex 0.149.0, gemini, opencode);
`agy` is unverified and web-sourced.

| Friend | Invoke | Read-only | Structured output |
|---|---|---|---|
| claude | `-p --output-format json --json-schema <f>` | `--permission-mode plan` | native JSON Schema validation |
| codex | `exec --json --output-schema <f> -o <out>` | `-s read-only` | native schema |
| gemini | `-p -o json` | `--approval-mode plan` | prompt-level contract |
| opencode | `run --format json` | **none** | prompt-level contract |
| agy (antigravity) | `-p --output-format json` | policy-based | prompt-level contract |

Diff artifacts: top-level `codex review --base <branch>` / `--uncommitted` /
`--commit <sha>` is documented as non-interactive and accepts custom instructions on
stdin. `codex exec review` is the equivalent under `exec`.

**Trap, verified:** `codex resume` and `codex fork` are the *interactive* commands —
`codex resume --help` reads "Resume a previous interactive session (picker by
default)". They carry no `--json`, `--output-schema`, or `-o`. The non-interactive
forms are `codex exec resume` and `codex exec fork`. Version 1 of this spec named the
interactive ones, which would have dropped round 2 into a TUI picker with no parseable
output. Moot in practice now that rounds are stateless (§4.1), but recorded so nobody
reintroduces it.

**Not usable, for the record:** `claude mcp serve` exposes Claude Code's *toolbox*
(26 tools — `Read`, `Bash`, `Agent`, `Skill`, …) to a host. It does not expose the
agent as a callable tool. Only `codex mcp-server` offers genuine agent-as-a-tool
(`codex`, `codex-reply`). Gemini and opencode are MCP clients only. A uniform MCP
transport is therefore impossible, and unnecessary: shell-out is the single transport.

### 11.1 Capability probing

At roster-resolve time each friend gets a capability set `{schema, readonly, effort}`.

**Capabilities are computed from the final effective argv**, after every layer in
§10.1 has been applied — never from the adapter's declared defaults. A roster or
`extra_args` entry that weakens the sandbox must surface as `readonly: false` in the
report header rather than being contradicted by it.

A missing capability produces a documented downgrade, recorded in `run.json` and
printed in the report header. Never silent.

## 12. Isolation and run directory

`scope` is a containment property, not a prompt convention. Version 1 treated
`scope = "doc"` as mitigation for opencode's missing read-only mode; it was not.
An artifact containing "now rewrite src/auth.py" defeats a prompt-level scope
instruction, and prompt injection through a reviewed artifact is the expected case
here, not an exotic one.

Every run is therefore isolated in a git worktree:

1. **Compute diff artifacts first**, before any run file exists.
2. Snapshot: `git stash create` produces a commit object of the dirty tree without
   touching it (or `HEAD` when clean); `git worktree add --detach <dir> <sha>`.
3. `scope = "repo"` friends run with cwd inside the worktree.
4. `scope = "doc"` friends run in a bare directory containing **only** the frozen
   artifact — no repository at all. This is what makes doc scope real: a
   write-capable friend can write whatever it likes, into a disposable directory
   with no path back to the source tree.
5. The run directory lives **outside** the worktree, defaulting to
   `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/<run-id>` with `--out`
   to override. Version 1 put it in the repo, where `codex review --uncommitted` —
   whose help reads "staged, unstaged, **and untracked** changes" — would have
   reviewed the tool's own scratch files as part of the diff.
6. Worktree removed at run end unless `--keep`.

Changes a friend makes inside the worktree are **never** copied back. The worktree is
evidence, not a proposal.

```
<run-dir>/
  run.json          # config snapshot: roster, effective argv, capabilities,
                    # downgrades, lenses, mode, ceilings, artifact hash
  artifact/         # frozen copy of what was challenged
  round-N/
    <friend>.raw    # exact stdout
    <friend>.json   # normalized
    <friend>.meta   # argv, exit code, duration, cost, orphan check
    REQUEST.json    # present when the runner halted for orchestrator judgment
    RESPONSE.json   # the orchestrator's reply
  claims.jsonl
  report.md
```

Raw stdout is always preserved next to the normalized form — when normalization is
wrong, the original is what you need.

## 13. Trust model

**Repo-local `.adversarial-friends/` is untrusted.** A cloned repository is hostile
input. Version 1 let repo-local config supply arbitrary trailing `extra_args`, so a
repository could ship `extra_args = ["--dangerously-bypass-approvals-and-sandbox"]`
and get a run that reports `readonly: true` while running unsandboxed.

- Loading repo-local config requires `--trust-repo-config`, or a recorded approval in
  `~/.config/adversarial-friends/trusted.toml` keyed by repo path **and config hash**.
  Editing the config re-prompts.
- User-level config (`~/.config/adversarial-friends/`) is trusted.
- **Argument deny list**, applied to `extra_args` and to any roster-supplied flag,
  from any source: `--dangerously-*`, `--allow-dangerously-*`, `--yolo`, `-y`,
  `--approve-for-me`, `--auto`, and any flag that sets sandbox, approval, or
  permission mode. A match **aborts the run**; it is not silently dropped, because a
  silently dropped flag produces a run whose config does not match its behavior.
- Friend names match `^[a-z0-9][a-z0-9_-]{0,31}$`. Names are path components
  (`<friend>.raw`), and a roster naming a friend `../../../../tmp/owned` must not
  write outside the run directory. Every resolved output path is additionally
  verified to remain beneath the run directory.
- Capabilities come from the final effective argv (§11.1), so anything that does slip
  through is reported accurately rather than masked.

## 14. Failure handling

| Condition | Behavior |
|---|---|
| CLI not on `$PATH` | Skip, note in report header |
| Auth failure (nonzero exit + stderr match) | Skip, note, print the remediation command |
| Timeout | Kill the process **group** (§14.1), keep partial output, mark the friend's verdicts *missing* |
| Malformed JSON | One in-process repair attempt re-prompting with the parse error; on second failure, halt with an extract REQUEST (§4.2) |
| Zero friends available | Degraded mode for `report` (§8.1); hard error otherwise |

A friend failure never silently becomes a non-dispositive verdict: missing verdicts
push claims toward `incomplete` (§7.2), and any friend failure disqualifies the round
from being dry (§7.3).

### 14.1 Process groups

Coding CLIs spawn descendants — MCP servers, shells, language servers. Killing only
the parent on timeout leaves them running, making network calls and writing files
after the run is marked incomplete.

Each friend is started in its own process group (`start_new_session=True`). Timeout
sends `SIGTERM` to the group, waits a 10-second grace period, then `SIGKILL`s the
group. The runner verifies no descendants survive before finalizing, and records any
orphans in `<friend>.meta`.

## 15. Multi-harness packaging

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

`SKILL.md` is the shared format across Claude Code, Codex
(`~/.codex/skills/<name>/SKILL.md`), and Gemini (`gemini skills install`).
Per-harness manifests are thin shims over the same skill and runner.

## 16. Testing

- **Adapter fixtures**: canned CLI stdout in, expected normalized claims out. Covers
  every documented downgrade path and the deny-list rejections.
- **Mode-driver tests** against a fake friend binary emitting scripted claims and
  verdicts, plus canned `RESPONSE.json` files. Must cover every state in §7.2 —
  including two-friend deadlock, all-`unproven`, and missing-judge `incomplete` —
  and every termination path in §7.3 and §7.4.
- **Isolation tests**: run files must not appear in a `--uncommitted` diff computed
  inside the worktree; a `scope = "doc"` friend must have no repository visible.
- **Trust tests**: a repo-local roster with a denied flag aborts; a traversal name is
  rejected.
- **Process-group test**: a friend that spawns a sleeping child and hangs leaves no
  survivors.
- No live model calls in CI. Convergence logic must be deterministically testable.

## 17. CLI surface

```
af run [ARTIFACT] [--mode report|crossexam|gate|loop]
                  [--preset inherit|thorough|cheap]
                  [--lens NAME ...] [--friend NAME ...] [--max-friends N]
                  [--rounds N] [--max-loop-iterations N] [--max-wall-clock S]
                  [--max-calls N] [--max-spend-usd AMT]
                  [--attributed] [--include-self] [--trust-repo-config]
                  [--model NAME] [--effort LEVEL] [--timeout SECONDS]
                  [--out DIR] [--keep] [--json]
af run --resume RUN_ID
af resolve RUN_ID --claim ID --disposition fixed|rejected|accepted-risk
                  --evidence TEXT
af init   [--force]
af doctor [--json] [--gc]
```

`af run` is the default subcommand; a bare `af <artifact>` is `af run <artifact>`.

**`af init`** probes `$PATH`, checks auth for each discovered CLI, reads each
friend's own config where the format is known, prints what it found, and writes a
commented `.adversarial-friends/friends.toml` reflecting discovered reality. The
output is a file to edit, not a set of answers the user is trapped into.

**`af doctor`** performs the same probe read-only and writes nothing. `--gc` is the
one exception: it removes worktrees and run directories left behind by runs that were
abandoned mid-halt (§18.6). It answers
"why was gemini skipped" — reporting, per friend: binary path, version, auth status,
resolved capability set, and every downgrade a run would record.

### 17.1 Exit codes

| Code | Meaning |
|---|---|
| 0 | Ran to a terminal state; no gate blocked |
| 1 | Gate blocked, or run ended `incomplete` |
| 2 | Usage or configuration error, including a denied argument |
| 3 | No usable friends for the requested mode |
| 10 | Needs orchestrator judgment — `RESPONSE.json` expected, then `--resume` |
| 11 | Ceiling hit (`budget-exhausted`) |

## 18. Risks

1. **`agy` is unverified.** Every antigravity flag is web-sourced, not probed. The
   adapter ships marked experimental until someone runs it.
2. **Prompt-level JSON contracts drift.** Gemini, opencode, and agy have no native
   schema validation. Beyond one repair attempt the runner halts for orchestrator
   extraction, which is correct but costs a round-trip on every flaky friend.
3. **Blind presentation is unverified as an improvement.** It is well-motivated —
   removing source attribution should reduce deference and contrarianism — but it
   has not been measured against the attributed baseline. `--attributed` exists
   partly so the comparison can be run.
4. **Stateless rounds cost tokens.** Every round re-sends the artifact and the
   contested ledger slice. This is the accepted price of §4.1; if it proves
   prohibitive the fix is a smaller ledger slice, not session reuse.
5. **Cost scales multiplicatively** — friends x lenses x rounds x loop iterations.
   Bounded by §7.4, not eliminated.
6. **The orchestrator halt/resume loop is a new failure surface.** An orchestrator
   that writes a malformed `RESPONSE.json`, or never writes one, strands the run.
   The runner validates the response against a schema and the run is resumable, but
   an abandoned run leaves a worktree behind until `af doctor --gc`.

## 19. Revision history

**v2 (2026-08-22)** — Revised after an adversarial review of v1 performed by
`codex exec -s read-only` on codex 0.149.0, which returned 17 findings. All 17 were
accepted; two were amended in scope rather than adopted verbatim, and one was folded
into another. This is the tool's own workflow applied to its own spec.

Changes, mapped to findings:

| Finding | Change |
|---|---|
| Two-friend rosters could never deadlock | §7.1 originator holds a standing implicit `upheld` vote; quorum is two dispositive verdicts |
| Termination undefined for missing / all-non-dispositive verdicts | §7.2 full state table, including `incomplete` |
| Amendments had no stable identity | §6.1 versioned claim ids with `supersedes`; verdicts on superseded versions excluded from tally |
| Dedup was orchestrator judgment but drove deterministic termination | §4.2 halt/resume protocol; §6.3 Alias records |
| Session reuse defeated ledger pruning | §4.1 rounds are stateless; `resume` dropped from the capability set |
| A dry round ignored execution completeness | §7.3 dry requires every required friend to have completed successfully |
| `loop` had no global ceiling | §7.4 mandatory ceilings and a `budget-exhausted` status |
| Gate resolution was unrepresentable | §6.4 Resolution records; §7.5 `af resolve`; hash validation |
| `codex resume` is interactive | §11 corrected to `codex exec resume` / `codex exec fork`, and recorded as a trap |
| Malformed-JSON fallback had no protocol | §4.2 extract REQUEST; §14 second failure halts |
| Repo config could disable safety controls | §13 trust model, deny list, capabilities from effective argv |
| `scope = "doc"` was a prompt convention | §12 worktree isolation; doc scope gets no repository at all |
| Frozen-artifact guarantee was false for repo scope | §12 snapshot before any run file exists; run dir outside the worktree |
| Friend names allowed path traversal | §13 slug validation and path containment |
| Timeouts orphaned descendant processes | §14.1 process groups |
| Zero-config goal contradicted self-exclusion | §2 reworded; §8.4 degraded single-friend mode |
| Evidence enforcement was asymmetric | §6.5 `evidence_assessment` on every dispositive verdict |

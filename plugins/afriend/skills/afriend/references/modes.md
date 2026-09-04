# Modes

| Mode | Status | What it does |
|---|---|---|
| `report` | **implemented** | One round. Every friend critiques in parallel; claims are merged (exact-match only) and ranked by severity in `report.md`. |
| `crossexam` | **implemented** | `report`, then friends judge each other's claims across rounds until every claim settles, deadlocks, or a ceiling is hit. |
| `gate` | **implemented** | Cross-examination, then every surviving non-advisory claim needs an explicit resolution before the gate clears. |
| `loop` | **implemented** | Cross-examination, repeated until two consecutive rounds surface nothing new. |

The effective default profile is `quick`, which selects `report`. `balanced`
selects `crossexam`; `thorough` selects `loop`. Use `afriend run <artifact>
--profile NAME` for a per-run choice. An explicit `--mode` wins over a
profile's mode; explicit safe run flags win over profile settings. In other
words, `report` is the default effective mode for a new installation.

## Minimum roster

A `report` with one friend, including a Codex host self-review, is allowed as
a recorded downgrade in `run.json` and `report.md`; it is one review, not a
cross-examination. A detected Codex host is included as a friend by default
but remains advisory: its row is `host-self-review (advisory)` and
`independent=false`. Its findings remain visible, while its verdicts cannot
settle claims.

`crossexam`, `gate`, and `loop` require at least two independent non-host
friends in addition to any host. With fewer, they refuse with exit 3 before a
run directory is created. The host cannot satisfy admission,
`--require-friends` participation, judging quorum, gate clearance, or loop
convergence. This preflight rule prevents an advisory or single-reviewer
judging run from leaving artifacts that look resumable or authoritative.

## Cross-examination

This is the mode the project exists for: it automates the manual loop of
handing one reviewer's findings to another and carrying the argument back.

Round 1 is a critique round, identical to `report`. From round 2 on, every
friend receives the still-contested claims **it did not write** and returns
one verdict each — `upheld`, `refuted`, `amended`, `unproven`, or
`out-of-scope`.

```bash
afriend run docs/design.md --mode crossexam
afriend run docs/design.md --mode crossexam --max-rounds 4
```

A few properties worth knowing before you read a report:

**A claim's author never judges it.** Judges are the roster minus everything
in the claim's `origin`, and a claim that two friends both raised is judged
by neither. With a small roster this can leave a claim with no judges at
all; that shows up as `unproven`, not as a quiet pass.

**Claims reach judges blind.** A judge is not told who wrote what — not the
friend, and not the lens either, since a round-robin lens assignment
identifies the author just as surely. `--attributed` turns this off if you
want to compare.

**Disagreement is the output, not a failure.** Two judges who disagree leave
the claim `contested`, and still disagreeing at `--max-rounds` leaves it
`deadlocked`. Neither is resolved by majority or by the tool's preference:
the report quotes both sides as written and leaves the call to you.

**A judge that could not check the evidence settles nothing.** A verdict
carrying `evidence_assessment: unverifiable` is downgraded to `unproven`
before anything counts it, so a claim can never be dismissed on the strength
of nobody having looked.

**Amendments create a new claim version.** If judges unanimously amend
`c-0007@1`, the original becomes `superseded` and `c-0007@2` carries the
rewrite into the next round — judged by neither the original author nor the
amenders.

### Claim states

| State | Meaning | Terminal |
|---|---|---|
| `settled-upheld` | Judges unanimously agreed the claim stands | yes |
| `settled-refuted` | Judges unanimously refuted it | yes |
| `superseded` | Amended; a successor claim carries it on | yes |
| `contested` | Judges disagreed; rounds remain | no |
| `deadlocked` | Still contested at `--max-rounds` | yes |
| `unproven` | Below quorum, or no judge could verify it | no |
| `incomplete` | Below quorum because a required judge never reported | no |
| `discarded` | `unproven` twice running with an unchanged verdict set | yes |

### Ceilings

| Flag | Default |
|---|---|
| `--max-rounds` | `3` (three total rounds, including the critique round) |
| `--max-calls` | derived: `ceil(friends × max-rounds × iterations × 1.5)` |
| `--max-wall-clock` | `7200` (seconds) |
| `--max-loop-iterations` | `5` (`loop` only; a maximum of five iterations) |

`--max-calls` is derived from your roster rather than fixed, so adding a
friend does not make the default configuration trip its own ceiling. Hitting
any ceiling is `budget-exhausted`: the run stops, says so in the report, and
exits `11` — it has neither converged nor cleared anything.

## Gate

`gate` is cross-examination plus one rule: every non-advisory claim that did
not clear on its own needs an explicit resolution. Only `settled-refuted`
clears unaided. `settled-upheld` does not, because the judges agreeing a
defect is real is the opposite of a pass; `discarded` does not, because two
rounds of judges unable to verify the evidence is nobody having looked.
`superseded` is exempt: its successor carries the question.

```bash
afriend run docs/design.md --mode gate
# exit 1: gate blocked -- 2 claim(s) need a resolution: c-0001@1, c-0004@1
```

Resolve each one, and the gate re-evaluates as you go:

```bash
afriend resolve <run-id> --claim c-0001@1 \
    --disposition fixed --evidence src/auth.py:38
# c-0001@1 fixed (location-changed)
# afriend: gate blocked -- 1 claim(s) still need a resolution: c-0004@1

afriend resolve <run-id> --claim c-0004@1 \
    --disposition accepted-risk --evidence docs/design.md:12
# gate clear
```

`--disposition` is `fixed`, `rejected`, or `accepted-risk`. Advisory claims
never appear here: their lens deliberately does not demand a failure
scenario, and gating on "this is more than you need" would silence it.

### What a resolution actually proves

**Less than it looks like, and the tool says so.** A resolution is an
attestation. The runner cannot know a defect is gone; it can only check
whether the location your `--evidence` names has changed since the run
started, and it reports which of three things it found:

| `verified` | Meaning |
|---|---|
| `location-changed` | The named location differs from the run's snapshot |
| `location-unchanged` | It does not |
| `unverifiable` | The runner could not reconstruct that location at all |

Three consequences worth knowing:

* **A fix that landed somewhere else is fine.** A valid fix for a claim about
  `docs/design.md` frequently lands in `src/auth.py`. Name the location that
  actually changed; requiring the reviewed artifact to change would force
  dummy edits to clear a gate.
* **`unverifiable` can record `rejected` or `accepted-risk`.** You are told
  the runner checked nothing, so silence is never mistaken for confirmation.
* **`fixed` requires `location-changed`.** Unchanged evidence contradicts the
  attestation, and unverifiable evidence cannot support it. Name the changed
  location, or use `accepted-risk` when verification is intentionally
  unavailable.

`--evidence` must name a location. Prose alone leaves nothing to check, and
recording it would make every resolution look equally well-supported.

## Loop

`loop` repeats the whole cross-examination and stops when two consecutive dry
rounds surface nothing new *and* every non-advisory claim is terminal.

```bash
afriend run docs/design.md --mode loop --max-loop-iterations 5
```

**The runner never edits your artifact.** So a loop buys two things:
convergence detection — evidence that the roster keeps finding the same
things and nothing more, which is the difference between "one round found 3
issues" and "three rounds keep finding those same 3 issues" — and picking up
a revision if something outside the run makes one between iterations.

Each iteration owns its own block of round numbers, so `round-1/` and
`round-4/` are iteration 1 and 2 rather than one overwriting the other. The
call budget is a whole-run total, not per iteration.

## Merge adjudication

Deduplication is judgment, and the runner does not pretend to have it.

**`--merge exact` (the default)** merges two claims only when their text and
location match exactly, ignoring whitespace and case. It under-merges on
purpose: guessing at equivalence corrupts termination arithmetic, and an
unmerged duplicate costs a round while a wrong merge silently deletes a
finding. This is what lets a run always finish unaided.

**`--merge orchestrator`** hands the judgment out. The runner stops after the
critique round, writes the claims to `round-1/REQUEST.json`, and exits `10`:

```bash
afriend run docs/design.md --mode crossexam --merge orchestrator
# afriend: waiting for merge adjudication. Fill in .../round-1/REQUEST.json,
#   save it as RESPONSE.json beside it, then run:
#     afriend run --resume run-20260824T081009-8e820bb6 --out ...
```

`REQUEST.json` ships an empty `merges` array to fill in — edit it and save as
`RESPONSE.json`:

```json
{"version": 1,
 "merges": [{"canonical": "c-0001@1", "duplicate": "c-0004@1",
             "rationale": "same missing guard, different wording"}]}
```

Then resume. **The resuming command line carries no other non-authority
configuration flags** — mode, ceilings, roster and artifact all come from the
run directory, because the same response has to produce the same run:

```bash
afriend run --resume <run-id>
# If and only if the halted invocation granted external tools:
afriend run --resume <run-id> --allow-external-tools=agy
```

The saved snapshot is immutable input identity, not a hint. Resume verifies
the frozen artifact hash and, for repository runs, the saved commit, tree, and
repository-relative artifact blob; it refuses a missing or mismatched snapshot
rather than reviewing current files. Invocation-local authority is the
exception to restored configuration.
`--allow-external-tools=PROVIDER`, unsandboxed execution, extra arguments,
and passed environment variables are the exceptions. These authority grants
must be repeated exactly on the current command line. For example, repeat
`--allow-external-tools=agy` only when the halted invocation recorded that
grant; omitting it, changing the provider set, or newly adding it is refused.

Round 1 is not re-run. Its claims are read back from the ledger, so the
adjudication applies to the ids you were actually shown.

A response is checked before anything is written: an unknown claim id, a
claim merged into itself, a chain (`A→B` and `B→C`), or the same duplicate
named twice are all refused. Each would corrupt the alias graph in a way you
would only notice much later, while wondering where a finding went.

Corroboration survives an adjudicated merge — the merged-away claim's origin
joins its canonical, so `*(corroborated by 2 friends)*` still appears. That
matters most here: these are merges of *differently worded* claims, which is
exactly where independent agreement is the strongest evidence.

`--merge orchestrator` works with `--mode loop` too, and it halts once per
iteration: each one asks for its own adjudication, and the resume re-enters
the iteration it stopped in carrying what earlier iterations already decided.
A five-iteration loop therefore wants five responses, which is the honest
cost of asking a human to adjudicate every merge.

## Choosing friends without repeating flags

`--friend cli:lens` on every invocation gets old. A roster file is the same
thing, checked in:

```toml
# ~/.config/adversarial-friends/roster.toml
[[friend]]
name = "codex-ops"
cli  = "codex"
lens = "ops"

[[friend]]
name = "claude-security"
cli    = "claude"
lens   = "security"
effort = "high"        # optional; also model, scope, timeout
```

`afriend init` writes one from what is actually installed, with the caveats
for each CLI as comments. It asks nothing and refuses to overwrite without
`--force` — it is a file you are meant to edit.

**Where a roster may come from is a security question** (§13). A cloned
repository is hostile input, and a roster decides who reviews your code:

| Location | Picked up automatically? |
|---|---|
| `~/.config/adversarial-friends/roster.toml` | **Yes** — your own machine-wide config |
| Anywhere named with `--roster FILE` | Yes — naming it is your explicit act |
| Repo-local `.adversarial-friends/` | **Never** |

A roster supplies *values only*, for `name`, `cli`, `lens`, `model`,
`effort`, `scope` and `timeout`. There is no mechanism for a file to inject a
flag; `--unsafe-extra-args` exists only on the command line.

### Effort presets

| Preset | Behaviour |
|---|---|
| `inherit` **(default)** | Emit no model or effort flags at all |
| `thorough` **(default for `gate`)** | Maximum *available* effort per friend |
| `cheap` | Lowest available effort |

`inherit` is the default because each CLI already carries an effort its owner
chose deliberately; overriding silently produces surprise behaviour and
surprise cost.

`thorough` is uneven by construction — claude reaches `max`, codex `xhigh`,
agy stops at `high`, ollama has no effort concept at all — which is why the
report states the effort each friend actually received. Otherwise a weak
critique from a friend that topped out low reads as a signal about the
artifact when it is a signal about the flag matrix.

**A preset promises nothing for `opencode`.** Its effort flag accepts any
string silently, so the level it ran at cannot be confirmed; the run records
a note saying so rather than implying the preset was honoured.

Precedence, strongest last: adapter default → `--preset` → roster entry →
`--friend`. Each layer fills only what the one above left unset, so you can
keep a roster and still override a single run from the command line.

### When a friend's output cannot be parsed

Repair is a pure transformation — fenced-block extraction, brace balancing,
trailing-comma stripping. **Never a model call**: re-prompting reaches a
fresh process that never produced the broken output and would silently redo
the whole critique at full cost.

So when repair fails, `--merge orchestrator` halts and asks you to read the
raw output instead of discarding it:

```bash
afriend run docs/design.md --merge orchestrator
# afriend: codex-ops produced output that could not be parsed into claims.
#   Fill in `findings` for each in .../round-1/REQUEST.json ...
```

Extracted findings go through the same schema a friend's own output does —
severity, claim, evidence, failure_scenario, suggested_fix all required. An
orchestrator is trusted to read, not to bypass the contract. The friend keeps
authorship, so it still cannot judge its own claims.

Under `--merge exact` the friend is simply marked failed, which is what keeps
the default usable with no harness attached.

## Everything else

### Provider selection and readiness

The host is the orchestrator. In Codex, Codex remains the orchestrator and is
included as a friend by default; it is marked `host-self-review (advisory)`
and `independent=false`. Non-Codex hosts are excluded by default.
`--include-self` and `--exclude-self` are mutually exclusive overrides.
Provider preferences live in the user's XDG configuration, never in the
reviewed repository:

```bash
afriend providers list
afriend providers enable claude
afriend providers disable opencode
afriend providers set-model ollama qwen3:8b
afriend providers clear-model ollama
```

`--enable-provider NAME` and `--disable-provider NAME` override persistent
settings for one automatically discovered run. Disabled providers are not
probed. Readiness is assessed before `--max-friends`: only `ready` providers
consume capacity; `reachable-unconfigured`, `unavailable`, `disabled`,
`host-excluded`, and `policy-blocked` candidates do not. `afriend doctor`
lists every known provider with one of those readiness states, its policy
source, and remediation. It exits `0` if at least one provider is ready and
exits `3` if no provider is ready.

External tools are denied by default. The denial is distinct from local
read-only/OS confinement and covers provider-managed tools, plugins, apps,
and MCP servers. A provider that cannot enforce denial is `policy-blocked`
unless the operator explicitly passes the repeatable, required-value
`--allow-external-tools=PROVIDER` for that run. Use
`--allow-external-tools=*` only for an explicit global grant. Unknown,
duplicate, or mixed `*` plus provider grants are invalid; the old valueless
form is invalid too. No persistent or repository configuration can grant
this authority, and these grants do not change provider defaults.

| Flag | Effect |
|---|---|
| `--model NAME`, `--effort LEVEL` | Override every friend; §10.1's strongest layer |
| `--lens NAME` (repeatable) | Restrict which lenses discovery assigns |
| `--max-friends N` | Cap the roster; reports what it dropped |
| `--require-friends N` | Fail the run (exit 12) if fewer than `N` friends answered; opt-in, unset by default |
| `--keep` | Leave friend worktrees under the run directory to inspect |
| `--json` | Print run.json instead of the run directory path |
| `--failure-summary terminal`, `--failure-summary report-only` | For a zero-response review, `terminal` (the default) prints the incomplete summary to stderr; `report-only` keeps it in run metadata, `report.md`, and status only |
| `--attributed` | Show judges who wrote each claim (§5 defaults to blind) |
| `--include-self`, `--exclude-self` | Mutually exclusive overrides for host participation; host review remains advisory and non-independent |
| `--enable-provider NAME`, `--disable-provider NAME` | Override persistent provider policy for this automatic roster only |
| `--allow-external-tools=PROVIDER` | Explicitly inherit that provider's managed tools and connectors for this run; repeat per provider, or use `=*` globally |
| `--pass-env VAR` (repeatable) | Allow an exported `VAR` through the filtered environment to every executable friend process; HTTP friends have no child environment |
| `--no-progress` | Suppress the per-friend progress on stderr; stdout is unaffected |
| `--allow-unsandboxed-friend` | Explicit risk acceptance for a provider without a verified read-only mode: only permits fallback when OS confinement is unavailable; it does not disable an available OS sandbox. In that fallback, it retains same-user filesystem read access (§12.2) |
| `--unsafe-extra-args='...'` | Pass unvalidated flags; needs `--i-accept-unsandboxed` |

### Guided setup and profiles

`afriend init --guided` previews setup with no writes. It reports the
available built-in profiles, provider readiness, the advisory host role when
detected, and that external tools remain denied. Add exact selections and
`--apply` to persist only those selections plus the generated roster:

```bash
afriend init --guided --default-profile balanced --enable-provider claude
afriend init --guided --apply --default-profile balanced --enable-provider claude
```

Plain `afriend init` writes only a discovered roster. `afriend profiles list`,
`show`, `create`, `update`, `delete`, and `set-default` manage named profiles
in the user configuration. Their safe values are mode, preset, lenses,
friend/timeout ceilings, and round/iteration ceilings; profiles cannot select
providers, friends, models, credentials, forwarded environment, external
tools, unsafe arguments, or sandbox exceptions.

### Run status and claim discovery

`afriend status <run-id-or-path>` is read-only inspection. It reports mode,
scope, profile, lifecycle state, friends, rounds, claims, downgrades, and a
recommended next action. `--json` returns a versioned machine view; `--watch`
follows new `events.jsonl` records until the terminal event and treats an
unterminated tail as still being written.

`afriend resolve <run-id> --list` shows unresolved claims without writing.
`--next` shows a claim only when the highest-priority choice is unique. A
resolution still needs `--claim`, a user-supplied disposition, and evidence.

`afriend doctor` takes `--json` and `--gc`. GC removes run directories with
no `report.md` — every path out of a run writes one, so its absence means the
process died. A run halted for the orchestrator keeps its report and survives.

**`--unsafe-extra-args` is the only way arbitrary flags reach a friend**, and
it is deliberately awkward. It is command-line only (never from a roster),
requires `--i-accept-unsandboxed` plus the global `*` external-tool grant,
still refuses flags that disable approval outright, and forces `read-only:
False` in the report for every friend —
the runner cannot know what an unvalidated flag re-enabled. Use the `=` form:
`--unsafe-extra-args='--foo'`.

**There is no `--max-spend-usd`.** Bounding spend in dollars needs per-CLI
cost reporting nobody has captured, and a flag that silently never fires is
worse than none — you would set it and believe you were protected. Use
`--max-calls`, which is derived from your roster and actually enforced.

Resuming is narrower than it looks. `--resume` takes an orchestrator halt and
nothing else — a run that ended any other way starts over.

Run `afriend run --help` for the full flag list.

## Exit codes

`afriend run` and `afriend doctor` use these exit codes; not every code is reachable by
every command in this build:

| Code | Meaning | Reachable today via |
|---|---|---|
| `0` | success | a run that reached terminal states with nothing blocked; `afriend doctor` when at least one provider is ready |
| `1` | gate blocked, or run incomplete | every dispatched friend failed; a `crossexam` that left claims undecided or lost a required friend mid-round; or a `gate` with claims still needing a resolution |
| `2` | usage/config error | a missing artifact, a malformed `--friend` value, an unknown `cli` in `--friend`, an invalid model in a `cli:lens:model` value, `--max-rounds 1` with a judging mode, a `--resume` naming a run that does not exist or did not halt for the orchestrator, an existing `--out` directory, or an `afriend resolve` naming no location / an unknown claim / a `fixed` without verifiably changed evidence |
| `3` | no usable friends for the requested mode | `afriend run` when discovery finds nothing usable, or when `crossexam`, `gate`, or `loop` resolves fewer than two independent non-host friends; `afriend doctor` when no provider is ready |
| `10` | needs orchestrator | `--merge orchestrator` halting for merge adjudication; resume with `afriend run --resume` |
| `11` | ceiling hit | a judging mode hitting `--max-calls`, `--max-rounds` budget, `--max-wall-clock`, or `--max-loop-iterations` |
| `12` | below quorum | `--require-friends N` set, and fewer than `N` friends produced a usable answer this run |

A ceiling outranks every outcome below it, quorum included: a truncated run
has not evaluated anything, so a CI wrapper can treat `11` as "retry" and `1`
as "block" without ambiguity. Quorum in turn outranks gate and crossexam
completeness -- a run below the declared floor has not produced the review
its exit code would otherwise claim, whatever state the few claims it did
get are in.

In `loop`, natural exhaustion of the `--max-loop-iterations` range is not a
successful completion. If convergence was not reached first, the run records
the iteration ceiling and exits `11`.

`--require-friends` is unenforced when unset, and it is unenforced -- not
guessed at -- on a `--resume` of `--merge orchestrator`: that path applies
stored merges and goes straight to judging, so the resuming process never
dispatches a fresh critique round to count. A resumed run's quorum question
was already answered before the halt.

A deadlock exits `0` under `crossexam`: it is a completed run whose answer
happens to be "the friends disagree". Under `gate` it blocks, because that is
exactly what a gate is for.

A run cancelled by `SIGINT`/`SIGTERM` exits `128 + signal number` instead of
any of the above, and `afriend` prints `aborted by signal N` to stderr.

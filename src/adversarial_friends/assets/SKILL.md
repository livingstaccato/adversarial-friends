---
name: adversarial-friends
description: Cross-examine a spec, plan, design doc, or another reviewer's findings by dispatching it to other agent CLIs (claude, codex, agy, opencode) as independent adversarial reviewers, then merging their critiques into one ranked findings report. Use this whenever the user wants a design or plan challenged, wants a second or third opinion on a review, says something like "poke holes in this", "what's wrong with this plan", "have another model check this", "review my spec", "sanity-check this doc", "tell me why this is a bad idea" — or is about to commit to an architectural decision and wants more than one model's eyes on it before doing so.
---

# Adversarial Friends

Challenge an artifact by having several *other* agent CLIs attack it
independently, then merge what they find.

The point is not more review — it is **disagreement you can see**. One model
reviewing a document tends to produce confident prose. Several models
reviewing it separately produce claims that can be compared, and the places
they disagree are usually where the real problem is.

## When this fires

Use it for specs, design docs, implementation plans, and — the highest-value
case — **another reviewer's findings**. Challenging a review is what this was
built for: a finding that survives a second model's scrutiny is worth acting
on, and one that does not is worth dropping before it costs you a day.

Do not use it to generate a first review of code. It challenges artifacts; it
does not produce the initial critique.

## Running it

```bash
afriend run <artifact> --mode report      # one round of parallel critique
afriend run <artifact> --mode crossexam   # then friends judge each other
afriend run <artifact> --mode gate        # then every claim needs a resolution
afriend run <artifact> --mode loop        # repeat until nothing new appears
```

This skill drives the `afriend` console script, which comes from the
`adversarial-friends` Python package. If `afriend` is not on `PATH`, the
skill cannot run — install it with
`uv tool install git+https://github.com/livingstaccato/adversarial-friends`
(or `uv tool install .` from a checkout), then confirm with `afriend doctor`.

`<artifact>` is a path to a file — a spec, a plan, a review someone else
wrote, saved to disk. All four modes run; see `references/modes.md` for the
full rules.

Every mode dispatches the artifact to every discovered friend in parallel and
writes a run directory (under `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/`,
or `--out DIR`) containing `claims.jsonl`, `report.md`, `run.json`, a frozen
`artifact/` copy, and per friend under `round-N/`: `<friend>.prompt` (exactly
what it was asked), `.raw` (its unmodified stdout), `.err` (its stderr —
always written, even when empty), `.meta` (argv, exit code, duration,
timeout and orphan status), and `.sandbox` (the OS confinement policy it ran
under, when one was applied). `afriend run` prints only the run directory
path to stdout; read `report.md` from there and present the findings.

### Choose ready friends, not merely installed CLIs

The host is the orchestrator, not an independent reviewer. A Codex-hosted
run therefore excludes `codex` from automatic discovery by default; the same
rule applies to every detected host. Use `--include-self` only when a
deliberate self-review is useful. An explicit `--friend` roster is already a
deliberate choice and may name the host or a disabled provider.

Persistent provider defaults are user-owned, outside the reviewed repository:

```bash
afriend providers list
afriend providers enable claude
afriend providers disable opencode
afriend providers set-model ollama qwen3:8b
afriend providers clear-model ollama
```

For one run, `--enable-provider NAME` and `--disable-provider NAME` override
those defaults during automatic discovery. Disabled providers are not
probed. A friend must be `ready` before it consumes `--max-friends` capacity:
other states include `reachable-unconfigured` (for example, Ollama without a
model), `unavailable`, `disabled`, `host-excluded`, and `policy-blocked`.
`afriend doctor` reports the effective state, policy layer, and remediation.

External tools are denied by default, separately from filesystem/process
confinement. Adapters must neutralize provider-managed tools, plugins, apps,
and MCP servers or become `policy-blocked`; use `--allow-external-tools` only
as an explicit per-run opt-in. Security grants are never restored by
`--resume`: repeat them exactly on the current command line. A run written by
0.2.0 cannot prove its inherited connector authority, so reports mark it
`legacy-unknown` rather than claiming tools were denied.

### This takes minutes, not seconds

Budget for it, and tell whoever asked. A friend is a whole agent CLI reading
a document and writing a critique: measured here at **around six minutes for
a single friend in a single round**, against a default `--timeout` of 15
minutes each. Friends within a round run in parallel, so a round costs about
what its slowest friend costs — but `crossexam` runs up to three rounds, so
**twenty minutes or more is normal, not a symptom**.

Do not kill a run because it has gone quiet. Progress goes to **stderr**: a
line per friend as it finishes, and every 30 seconds a line naming whatever
is still outstanding and how long it has been running. If those heartbeat
lines are still appearing, the run is working. Watch stderr rather than
polling the run directory, and keep stdout clean — it carries the run path
and nothing else.

If you need an answer sooner, `--mode report` is one round instead of
several, and `--max-rounds 2` shortens a crossexam. Reducing `--timeout`
does not make friends faster; it only converts slow ones into failures.

Which mode to reach for:

* **`report`** — a first look at a document. One fan-out, no judging.
* **`crossexam`** — when the question is *which of these findings are real*
  rather than *what might be wrong*. Costs a fan-out per round.
* **`gate`** — when something downstream should stop until a human has
  answered each finding. This is the mode that fails a build.
* **`loop`** — when the question is *did we find everything*. It repeats
  until two consecutive rounds surface nothing new, which is the difference
  between "one round found 3 issues" and "three rounds keep finding those
  same 3 issues".

Do not reach for `gate` or `loop` on a user's behalf without saying so: both
cost several times what `report` does, and `gate` deliberately exits
non-zero until every claim is answered.

Exit codes: `0` the run reached terminal states with nothing blocked; `1` a
`gate` still has claims needing a resolution, every dispatched friend
failed, or a `crossexam` left claims undecided or lost a required friend
mid-round; `2` a usage or config error — a missing artifact, an
unrecognized `--friend` value, `--max-rounds 1` with a judging mode; `3` no
usable friend could be found at all (install a second agent CLI, or pass
`--include-self` to let the host CLI review its own artifact); `10`
`--merge orchestrator` is waiting for you to adjudicate merges (see
`references/modes.md`); `11` a judging mode stopped at a ceiling, having
neither converged nor cleared anything; `12` `--require-friends N` was set
and fewer than `N` friends produced a usable answer -- opt-in, unset by
default. A run cancelled by a signal exits `128 + signal number`.

For `loop`, naturally reaching `--max-loop-iterations` without convergence is
also a ceiling: it records that stop reason and exits `11`.

A `gate` run exits `1` while any claim still needs an answer. Resolve them
one at a time; each call re-reports what is left:

```bash
afriend resolve <run-id> --claim c-0001@1 \
    --disposition fixed|rejected|accepted-risk --evidence src/auth.py:38
```

`--evidence` must name a location, not prose. A resolution is an
attestation: the runner checks only whether that location changed since the
run started, and records `location-changed`, `location-unchanged`, or
`unverifiable`. Never present a recorded resolution to the user as proof the
defect is gone — say what was actually verified. `fixed` requires
`location-changed`; unchanged or unverifiable evidence is refused. Use
`accepted-risk` when verification is intentionally unavailable.

Check what is available first when a run comes back thin:

```bash
afriend doctor
```

It prints, per friend: whether the binary was found, whether it can enforce a
schema, whether it has a real read-only mode, and whether its effort level
can be verified. A friend missing from that list is why your report is short.
`doctor` exits `0` if at least one friend was found, `3` if none were.

## Reading the results like a reviewer, not a stenographer

The report is input to your judgment, not output to relay. Three things
deserve your attention before you hand anything to the user:

**Failed friends are not silent.** The friend table in `report.md` shows
status per friend. A run where two of three friends failed is not a clean bill
of health, and saying "no issues found" would be wrong. Say what did not run.

**Exit status lies.** Several CLIs exit 0 while producing nothing usable —
answering a different prompt, writing output to a file instead of stdout,
returning prose where JSON was asked for. The runner already treats these as
failures (see `references/troubleshooting.md`); your job is to notice when
the *pattern* suggests a misconfigured adapter rather than a quiet artifact.

**A friend that stops being dispatched has been ruled broken, not skipped.**
A downgrade saying a friend "will not be dispatched again this run" means it
failed identically twice; the runner stopped spending calls on it. Report
what it was doing wrong rather than treating the run as complete.

**A refused friend is a security refusal, not a bug.** A friend reported as
`refused: ... no OS sandbox ... available to confine it` was never started.
Its CLI has no read-only mode, so nothing constrains what it reads, and an
artifact under review is untrusted text that could tell it to read anything
the user can. Do not suggest `--allow-unsandboxed-friend` as the fix without
saying what it gives up; installing `bubblewrap`, or using a friend that
confines itself, is the better answer for an artifact the user did not write.

**Duplicates are under-merged on purpose.** The default merge only combines
claims with identical text and location, so two friends describing one defect
in different words appear twice. Merge them in your presentation — that is
judgment the runner deliberately declines to make.

## Reading a cross-examination

`--mode crossexam` adds a state per claim. The states are not a ranking, and
flattening them into one would throw away the thing the mode exists to
produce.

**`deadlocked` is a result, not an error.** Judges looked and disagreed. The
report quotes both sides verbatim because the runner is not entitled to pick
one — and neither are you, by default. Present the disagreement: what each
side actually argued, and what would settle it. A deadlock on a load-bearing
claim is usually the single most valuable line in the report.

**`settled-refuted` means the judges disagreed with the author, not that the
claim was noise.** It is worth one line in your summary, not silence — a
finding that two independent models rejected is still information about where
the document reads as alarming.

**`unproven` and `discarded` usually mean the evidence could not be found.**
Often that is a claim citing a path or line that does not exist. Check the
claim's `evidence` field before treating it as a real defect that nobody
could confirm.

**A claim with no judges is not a passed claim.** If every friend co-authored
it, nobody independent was left to judge, and it lands `unproven`. The
downgrade list in `run.json` says when this happened.

**`budget-exhausted` invalidates the summary, not just the last round.** The
run stopped early; claims still `contested` were mid-argument, not settled.
Say the run was truncated before reporting anything as resolved.

## Choosing lenses

Each friend runs under one lens, a prose file in `lenses/` describing what to
look for and what counts as evidence. Its full text — frontmatter stripped —
is prepended to that friend's prompt, so a `security`-assigned friend is
actually asked to attack trust boundaries while an `ops`-assigned friend is
asked what happens at 3am; they are not just labeled differently after the
fact. Every friend's exact prompt is written to
`round-1/<friend>.prompt` in the run directory, so you can always check what
a given friend was actually asked. The default — no `--friend` flag at all —
is round-robin lens assignment over every discovered friend.

**`--friend cli:lens` (repeatable) does not add to or bias that default
roster — it replaces it entirely.** Any `--friend` flag switches `afriend run`
from auto-discovery to exactly the friends you listed and no others: `afriend run
spec.md --friend agy:security` runs with *one* friend, not the normal
discovered set plus a nudge toward `security`. To emphasize a lens on part
of an otherwise-normal run, list every friend you want the run to have, one
`--friend cli:lens` per friend — e.g. `--friend codex:ops --friend
agy:security --friend opencode:scope` — never a single `--friend` layered on
top of discovery. A `--friend`-built (or discovered) roster with fewer than
two friends cannot cross-examine anything; `afriend run` records this as a
downgrade in `run.json` and `report.md` rather than letting a single-reviewer
run look like the real thing.

A lens name with no matching file falls back to the generic prompt alone and
is recorded as a downgrade in `run.json`, rather than failing the run or
silently pretending the friend had lens guidance.

Lenses marked `requires_failure_scenario: false` (currently only `scope`)
produce claims flagged `advisory` in `claims.jsonl` and rendered with an
`*(advisory)*` tag in `report.md` — real feedback that should never block a
decision, because "this is more than you need" is judgment rather than a
defect, and demanding a failure scenario for it would silence the lens
entirely. One thing this does *not* do yet: the claim schema still requires
every finding to include a non-empty `failure_scenario` field regardless of
lens, so a `scope`-lens friend must still supply something in that field
even though the design intends it to be optional for advisory lenses — a
known divergence, not something to paper over when you see it.

## Further reading

- `references/modes.md` — what `report`, `crossexam`, `gate`, and `loop` do,
  and which are implemented
- `references/ledger.md` — the claim/verdict/alias/resolution record types and
  how to read `claims.jsonl` directly
- `references/troubleshooting.md` — verified CLI invocation traps, what a
  failed friend usually means, and how to diagnose an empty report

---
name: afriend
description: Use for /afriend, when the user directly selects this skill, explicit afriend requests, operative “ask/use a friend to …” requests, and explicit requests to use Adversarial Friends. It is the sole router for review, status, configuration, and resolution; do not use for generic review, challenge, poke-holes, second-opinion, architecture requests, or incidental mentions of a friend.
---

# /afriend — Adversarial Friends router

Challenge an artifact by dispatching it to agent CLIs under distinct lenses,
then merge what they find.

The point is not more review — it is **disagreement you can see**. One model
reviewing a document tends to produce confident prose. Several models
reviewing it separately produce claims that can be compared, and the places
they disagree are usually where the real problem is.

## When this fires

`/afriend` is the short direct selector and the only router. Activate only for
direct selection, command-like intent that starts with `afriend` or uses
`afriend to ...`, an operative “ask/use a friend to …” request, or an explicit
request to use **Adversarial Friends**. An incidental mention such as “a friend
sent me this” does not activate this skill. Equivalent review forms include `afriend this plan`,
`afriend to this plan`, `afriend docs/design.md`, and `afriend to
docs/design.md with crossexam`.

Route an explicit conversational operation before doing any work:

| Conversation intent | Focused skill | Stable executable command |
| --- | --- | --- |
| review an artifact | `review` | `afriend run` |
| named-run status | `status` | `afriend status <run-id-or-path>` |
| provider readiness | `status` | `afriend doctor` |
| setup, defaults, or profiles | `configure` | `afriend init --guided`, `afriend providers`, or `afriend profiles` |
| inspect or resolve an existing run | `resolve` | `afriend resolve` |
| resume an existing run | this router | `afriend run --resume <run-id>` |

Phrases such as “afriend status” and “afriend review” are routing language,
not new executable aliases. The CLI command names remain `status`, `doctor`,
and `run`.
Likewise, `afriend resume <run-id>` resumes the run with `afriend run --resume
<run-id>`; it is not claim resolution and does not need a disposition or
evidence.

Conversational shorthand maps to `afriend run`; it is not a new CLI alias. If
the request names an existing path, pass that path to `afriend run`. If “this”
unambiguously refers to the current task's backing file, use that file. Only
materialize an exact artifact when its complete contents are already within
the request. Otherwise ask for a path rather than inventing or reconstructing
the artifact.

Generic requests such as `review this`, `poke holes in this`, `give me a
second opinion`, or architectural-decision language do not activate the skill
when `afriend` and the full product name are absent. Those remain ordinary
Codex work. Do not use this skill to generate a first review of code.

## Session preflight and feedback

On the first review request in a host task, and before every requested new
loop iteration, pause before dispatch and state the resolved run:

> About to start Adversarial Friends to review `<artifact>` in `<mode>` mode
> with `<profile>`. Scope: `<repository snapshot|document only>`. Friends:
> `<name, provider, lens, role>`; external tools: `<denied|explicit grant>`.

Accept the resolved default, a task-only profile or mode, a task-only enabled
roster, or stop. Do not repeat the preflight for later work in the same review
session unless the user requests a new loop iteration. The preflight is
descriptive: it does not grant provider enablement, external tools,
unsafe-extra arguments, or sandbox exceptions. A direct CLI command remains
non-interactive and uses its effective profile plus explicit flags.

As a run progresses, report when each friend finishes or fails, including its
provider, lens, result, and any downgrade. At completion, read the final
`events.jsonl` record and `report.md`, then say what finished and whether the
next action is to inspect, resolve, resume, fix configuration, retry, or start
another iteration. Do not call a failed, incomplete, downgraded, or
single-friend run a completed independent review.

## Running it

```bash
afriend run <artifact>                    # report: one parallel critique fan-out
afriend run <artifact> --mode report      # explicit spelling of the default
afriend run <artifact> --mode crossexam   # then friends judge each other
afriend run <artifact> --mode gate        # then every claim needs a resolution
afriend run <artifact> --mode loop        # repeat until nothing new appears
```

This skill drives the `afriend` console script, which comes from the
`adversarial-friends` Python package. If `afriend` is not on `PATH`, the
skill cannot run — install it with
`uv tool install git+https://github.com/livingstaccato/adversarial-friends`
(or `uv tool install .` from a checkout), then confirm with `afriend doctor`.

The CLI never runs automatically by itself. A plugin packages capabilities;
its underlying skill may be implicitly selected only by the narrow triggers
above, or directly selected by the user, and then invokes the CLI.

`<artifact>` is a path to a file — a spec, a plan, a review someone else
wrote, saved to disk. All four modes run; see `references/modes.md` for the
full rules. The effective default profile is `quick`, whose mode is `report`.
Select another mode only when the user names it or clearly requests its
semantics.

### Profiles

`quick` is the default built-in profile and keeps one report fan-out.
`balanced` selects `crossexam`; `thorough` selects `loop`. The user can set a
persistent default in `~/.config/adversarial-friends/session.json`, or select
one task-only with `afriend run <artifact> --profile NAME`. An explicit
`--mode` wins over the profile's mode, as do explicit safe run settings.

Use `afriend profiles list`, `show`, `create`, `update`, `delete`, and
`set-default` for user-owned named profiles. A profile may express review-safe
mode, preset, lenses, friend/timeout ceilings, and round/iteration ceilings;
it cannot select providers, a friend roster, models, credentials, environment
forwarding, external tools, unsafe arguments, or sandbox exceptions.

Artifact location selects the automatic scope. An artifact inside a Git
repository gets a repository snapshot; an artifact outside a Git repository
gets doc scope only, with a warning before friends start. Put it inside the
target repository when code inspection is intended. Normal untracked,
non-ignored files are included in the snapshot. Gitignored artifacts are
deliberately excluded and fail rather than silently reviewing stale `HEAD`
contents.

Every mode dispatches the artifact to every discovered friend in parallel and
writes a run directory (under `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/`,
or `--out DIR`) containing `events.jsonl`, `claims.jsonl`, `report.md`,
`run.json`, a frozen `artifact/` copy, and per friend under `round-N/`:
`<friend>.prompt` (exactly
what it was asked), `.raw` (its unmodified stdout), `.err` (its stderr —
always written, even when empty), `.meta` (argv, exit code, duration,
timeout and orphan status), and `.sandbox` (the OS confinement policy it ran
under, when one was applied). By default, `afriend run` prints only the run
directory path to stdout; `--json` prints the saved run metadata instead. Read
`report.md` from the run directory and present the findings.

### Choose ready friends, not merely installed CLIs

The host is the orchestrator. In Codex, Codex remains the orchestrator and is
included as a friend by default. Its report row is labeled
`host-self-review (advisory)` with `independent=false`. It may contribute
findings and advisory verdicts, but cannot satisfy the two-independent-friend
admission rule, `--require-friends` participation, judging quorum, gate
clearance, or loop convergence. Judging modes therefore need two independent
non-host friends in addition to any host; `report` may run host-only as a
recorded downgrade.

Non-Codex hosts remain excluded by default. `--include-self` and
`--exclude-self` are mutually exclusive per-run overrides. An explicit
`--friend` roster remains a deliberate selection and may name the host or a
disabled provider, but host-role marking and independent-authority rules still
apply. Explicit friends preflight executable/endpoint availability, required
models, adapter policy, and external-tool authority before a run directory is
created.

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
and MCP servers or become `policy-blocked`. The required-value flag is
repeatable: use `--allow-external-tools=PROVIDER` for a provider or the
explicit global grant `--allow-external-tools=*`. Unknown, duplicate, or
mixed `*` plus provider grants are invalid, as is the old valueless form.
`--unsafe-extra-args` additionally requires the global `*` grant and its own
acknowledgement.

External-tool authority is independent of persistent and per-run provider
enable/disable selection. Grants do not change provider defaults. Security
grants are never restored by `--resume`: repeat the same normalized set
exactly on the current command line.

### Runtime depends on the run

A friend is a whole agent CLI reading a document and writing a critique.
Runtime depends on the slowest selected friend, document size, and mode;
friends within a round run in parallel. `report` is one critique fan-out.
Judging modes use three total rounds by default. `loop` permits a maximum of
five iterations by default and requires two consecutive dry rounds for
convergence.

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

* **`report`** — the default. One critique fan-out, no judging.
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
usable friend could be found, or a judging mode resolved fewer than two
independent non-host friends (install additional independent agent CLIs, or
use `report` for a host-only/single-reviewer result); `10`
`--merge orchestrator` is waiting for you to adjudicate merges (see
`references/modes.md`); `11` a judging mode stopped at a ceiling, having
neither converged nor cleared anything; `12` `--require-friends N` was set
and fewer than `N` friends produced a usable answer -- opt-in, unset by
default. A run cancelled by a signal exits `128 + signal number`.

For `loop`, naturally reaching `--max-loop-iterations` without convergence is
also a ceiling: it records that stop reason and exits `11`.

A `gate` run exits `1` while any claim still needs an answer. Discover the
unresolved claims, then resolve one at a time:

```bash
afriend resolve <run-id> --list
afriend resolve <run-id> --next
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

It lists every known provider and its effective readiness state: `ready`,
`reachable-unconfigured`, `unavailable`, `disabled`, `host-excluded`, or
`policy-blocked`. Disabled providers are not probed. For each provider it also
reports whether schema and read-only enforcement are available and whether
effort can be verified. `doctor` exits `0` if at least one provider is ready;
it exits `3` if no provider is ready.

To inspect a named run without dispatching or changing anything, use:

```bash
afriend status <run-id-or-path>
afriend status <run-id-or-path> --watch
afriend status <run-id-or-path> --json
```

`status` reports identity, mode, scope, profile, lifecycle state, friend
completion or failure, current round, claim states, downgrades, and a next
action. `--watch` tails new lifecycle events until the terminal event; an
unterminated final JSONL line is simply still being written. Existing runs
without `events.jsonl` remain inspectable from their saved artifacts.

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
the user can. Prefer making `sandbox-exec` (macOS) or `bwrap` (Linux)
available, or using a verified self-confining provider. A provider with a
verified read-only mode does not need `--allow-unsandboxed-friend`; that flag
is explicit risk acceptance, not a normal fix, and lets the affected provider
run without OS confinement with same-user filesystem read access.

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
top of discovery. A `report` with one friend is allowed as a recorded
downgrade in `run.json` and `report.md`; present it as a single review, not a
cross-examination. `crossexam`, `gate`, and `loop` require at least two
independent non-host friends. With fewer, they refuse with exit 3 before a run
directory is created, so there is no partial judging run to interpret or
resume.

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

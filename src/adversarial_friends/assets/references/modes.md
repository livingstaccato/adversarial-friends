# Modes

| Mode | Status | What it does |
|---|---|---|
| `report` | **implemented** | One round. Every friend critiques in parallel; claims are merged (exact-match only) and ranked by severity in `report.md`. |
| `crossexam` | **implemented** | `report`, then friends judge each other's claims across rounds until every claim settles, deadlocks, or a ceiling is hit. |
| `gate` | planned | Cross-examination, then every surviving non-advisory claim needs an explicit resolution. |
| `loop` | planned | Cross-examination, artifact revised, repeated until two rounds surface nothing new. |

`afriend run` rejects `--mode gate` and `--mode loop` with a usage error (exit 2)
rather than pretending to support them:

```
afriend: mode 'gate' is not implemented yet; 'report' and 'crossexam' are available
```

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
| `--max-rounds` | `3` |
| `--max-calls` | derived: `ceil(friends × max-rounds × 1.5)` |
| `--max-wall-clock` | `7200` (seconds) |

`--max-calls` is derived from your roster rather than fixed, so adding a
friend does not make the default configuration trip its own ceiling. Hitting
any ceiling is `budget-exhausted`: the run stops, says so in the report, and
exits `11` — it has neither converged nor cleared anything.

Not in this build: `--merge=orchestrator`, `--resume`, `af resolve`, and
`af init`. Run `afriend run --help` to see the flags this build accepts.

## Exit codes

`afriend run` and `afriend doctor` use these exit codes; not every code is reachable by
every command in this build:

| Code | Meaning | Reachable today via |
|---|---|---|
| `0` | success | a run that reached terminal states with nothing blocked, `afriend doctor` (at least one friend found) |
| `1` | gate blocked, or run incomplete | every dispatched friend failed; or a `crossexam` that left claims undecided or lost a required friend mid-round |
| `2` | usage/config error | a missing artifact, a malformed `--friend` value, an unknown `cli` in `--friend`, an invalid model in a `cli:lens:model` value, `--mode gate`/`loop`, `--max-rounds 1` with `--mode crossexam`, or `--preset` set to anything but `inherit` |
| `3` | no usable friends for the requested mode | `afriend run` when discovery finds nothing usable; `afriend doctor` when no friend binary is found |
| `10` | needs orchestrator | reserved for `--merge=orchestrator` and parse-halt recovery — not implemented in this build |
| `11` | ceiling hit | `crossexam` hitting `--max-calls`, `--max-rounds` budget, or `--max-wall-clock` |

A ceiling outranks every outcome below it: a truncated run has not evaluated
anything, so a CI wrapper can treat `11` as "retry" and `1` as "block"
without ambiguity.

A deadlock exits `0`. It is a completed run whose answer happens to be "the
friends disagree" — blocking on that is `gate` mode's job, and `gate` is not
in this build. Read the report.

A run cancelled by `SIGINT`/`SIGTERM` exits `128 + signal number` instead of
any of the above, and `afriend` prints `aborted by signal N` to stderr.

# Modes

| Mode | Status | What it does |
|---|---|---|
| `report` | **implemented** | One round. Every friend critiques in parallel; claims are merged (exact-match only) and ranked by severity in `report.md`. |
| `crossexam` | **implemented** | `report`, then friends judge each other's claims across rounds until every claim settles, deadlocks, or a ceiling is hit. |
| `gate` | **implemented** | Cross-examination, then every surviving non-advisory claim needs an explicit resolution before the gate clears. |
| `loop` | **implemented** | Cross-examination, repeated until two consecutive rounds surface nothing new. |

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
| `--max-calls` | derived: `ceil(friends × max-rounds × iterations × 1.5)` |
| `--max-wall-clock` | `7200` (seconds) |
| `--max-loop-iterations` | `5` (`loop` only) |

`--max-calls` is derived from your roster rather than fixed, so adding a
friend does not make the default configuration trip its own ceiling. Hitting
any ceiling is `budget-exhausted`: the run stops, says so in the report, and
exits `11` — it has neither converged nor cleared anything.

## Gate

`gate` is cross-examination plus one rule: every non-advisory claim that did
not clear on its own needs an explicit resolution. Only `settled-refuted`,
`superseded` and `discarded` clear unaided — `settled-upheld` does not,
because the judges agreeing a defect is real is the opposite of a pass.

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
* **`unverifiable` is recorded, not refused.** You are told the runner
  checked nothing, so silence is never mistaken for confirmation.
* **One thing is refused:** `--disposition fixed` naming a location that did
  not change. That is the single case the runner can positively contradict.

`--evidence` must name a location. Prose alone leaves nothing to check, and
recording it would make every resolution look equally well-supported.

## Loop

`loop` repeats the whole cross-examination and stops when two consecutive
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

Not in this build: `--merge=orchestrator`, `--resume`, and `af init`. Run
`afriend run --help` to see the flags this build accepts.

## Exit codes

`afriend run` and `afriend doctor` use these exit codes; not every code is reachable by
every command in this build:

| Code | Meaning | Reachable today via |
|---|---|---|
| `0` | success | a run that reached terminal states with nothing blocked, `afriend doctor` (at least one friend found) |
| `1` | gate blocked, or run incomplete | every dispatched friend failed; a `crossexam` that left claims undecided or lost a required friend mid-round; or a `gate` with claims still needing a resolution |
| `2` | usage/config error | a missing artifact, a malformed `--friend` value, an unknown `cli` in `--friend`, an invalid model in a `cli:lens:model` value, `--max-rounds 1` with a judging mode, `--preset` set to anything but `inherit`, or an `afriend resolve` naming no location / an unknown claim / a `fixed` at an unchanged location |
| `3` | no usable friends for the requested mode | `afriend run` when discovery finds nothing usable; `afriend doctor` when no friend binary is found |
| `10` | needs orchestrator | reserved for `--merge=orchestrator` and parse-halt recovery — not implemented in this build |
| `11` | ceiling hit | a judging mode hitting `--max-calls`, `--max-rounds` budget, `--max-wall-clock`, or `--max-loop-iterations` |

A ceiling outranks every outcome below it: a truncated run has not evaluated
anything, so a CI wrapper can treat `11` as "retry" and `1` as "block"
without ambiguity.

A deadlock exits `0` under `crossexam`: it is a completed run whose answer
happens to be "the friends disagree". Under `gate` it blocks, because that is
exactly what a gate is for.

A run cancelled by `SIGINT`/`SIGTERM` exits `128 + signal number` instead of
any of the above, and `afriend` prints `aborted by signal N` to stderr.

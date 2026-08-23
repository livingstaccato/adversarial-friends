---
name: adversarial-friends
description: Cross-examine a spec, plan, design doc, or another reviewer's findings by dispatching it to other agent CLIs (claude, codex, agy, opencode, local ollama models) as independent adversarial reviewers, then merging their critiques into one ranked findings report. Use this whenever the user wants a design or plan challenged, wants a second or third opinion on a review, says something like "poke holes in this", "what's wrong with this plan", "have another model check this", "review my spec", "sanity-check this doc", "tell me why this is a bad idea" — or is about to commit to an architectural decision and wants more than one model's eyes on it before doing so.
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
bin/af run <artifact> --mode report
```

`<artifact>` is a path to a file — a spec, a plan, a review someone else
wrote, saved to disk. `report` is the only mode this build implements; see
`references/modes.md` for the others and their status. This dispatches the
artifact to every discovered friend in parallel and writes a run directory
(under `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/`, or
`--out DIR`) containing `claims.jsonl`, `report.md`, `run.json`, and each
friend's raw output under `round-1/`. `af run` prints only the run directory
path to stdout; read `report.md` from there and present the findings.

Exit codes from `af run --mode report`: `0` at least one friend produced a
usable critique; `1` every dispatched friend failed (the run mechanism still
completed and wrote a report, but nothing in it is trustworthy); `2` a usage
or config error — a missing artifact, an unrecognized `--friend` value, or a
mode other than `report`; `3` no usable friend could be found at all (install
a second agent CLI, or pass `--include-self` to let the host CLI review its
own artifact).

Check what is available first when a run comes back thin:

```bash
bin/af doctor
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

**Duplicates are under-merged on purpose.** The default merge only combines
claims with identical text and location, so two friends describing one defect
in different words appear twice. Merge them in your presentation — that is
judgment the runner deliberately declines to make.

## Choosing lenses

Each friend is labeled with one lens, a prose file in `lenses/` describing
what to look for and what counts as evidence. The default assignment is
round-robin over discovered friends; override it with `--friend cli:lens`
(repeatable) when the artifact has an obvious weak flank — a deployment plan
wants `ops`, an auth design wants `security`, a spec that keeps growing wants
`scope`.

Today the lens label organizes the *output* — it tags every claim's origin
and names the friend's own directory under `round-N/` — but this build's
`report` pipeline does not yet inline the lens's prose into the prompt a
friend receives: every friend is asked the same generic "challenge this
artifact" question. Read the lens file yourself before interpreting a batch
of claims labeled with it, and do not expect a `security`-labeled friend's
output to differ in kind from an `ops`-labeled one on that basis alone.

Lenses marked `requires_failure_scenario: false` (currently only `scope`) are
meant to produce claims you should treat as *advisory* — real feedback that
should never block a decision, because "this is more than you need" is
judgment rather than a defect, and demanding a failure scenario for it would
silence the lens entirely. The runner's claim schema does not yet vary by
lens (every finding must include a `failure_scenario` field to pass
validation), so apply this judgment yourself when you present `scope`
findings, rather than expecting the report to have already flagged them.

## Further reading

- `references/modes.md` — what `report`, `crossexam`, `gate`, and `loop` do,
  and which are implemented
- `references/ledger.md` — the claim/verdict/alias/resolution record types and
  how to read `claims.jsonl` directly
- `references/troubleshooting.md` — verified CLI invocation traps, what a
  failed friend usually means, and how to diagnose an empty report

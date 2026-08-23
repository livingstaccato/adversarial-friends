# The claim ledger

`claims.jsonl` is append-only. Each line is one JSON record with a `type`
field. The schema defines four record types; **only `claim` and `alias` are
ever written by this build.** `verdict` and `resolution` exist in the schema
and can be read back, but nothing in `af run --mode report` produces them —
they belong to `crossexam`, `gate`, and `af resolve`, none of which are
implemented yet (see `modes.md`).

**claim** — an assertion about the artifact. `id` is versioned (`c-0007@2`);
an amendment would create a new version rather than editing in place, so a
verdict is never ambiguous about which wording it judged — versioning exists
in the id format today even though nothing yet amends a claim. `origin` is a
list of `cli/lens` strings identifying which friend(s) produced it. `advisory`
is derived from the originating lens's `requires_failure_scenario` frontmatter
field (`false` there means `true` here) — currently only `scope`-lens claims
come back `advisory: true`; every other lens's claims are `false` (see
`SKILL.md`'s "Choosing lenses" section for what that means in practice, and
for the one thing this does *not* do: the schema still requires
`failure_scenario` on every finding regardless of lens).

Example, taken from a real run:

```json
{"type":"claim","id":"c-0001@1","supersedes":null,
 "origin":["fake/security"],"lens":"security","round":1,"advisory":false,
 "severity":"high","claim":"the guard is missing",
 "location":"src/auth.py:42","evidence":"src/auth.py:38",
 "failure_scenario":"expired token reaches the handler",
 "suggested_fix":"check exp before dispatch"}
```

**verdict** *(schema only — not produced by this build)* — one judge's ruling
on one claim version: `upheld`, `refuted`, `amended`, `unproven`, or
`out-of-scope`. `evidence_assessment` would record whether the judge could
actually find the evidence the claim cited.

**alias** — a merge decision. `source` is `exact` for the deterministic merge
`report` performs (the only merge strategy this build has); `orchestrator`
is reserved for a judgment-call merge that has no implementation to produce
it yet.

```json
{"type":"alias","canonical":"c-0001@1","duplicate":"c-0002@1",
 "round":1,"source":"exact","rationale":"identical claim text and location"}
```

**resolution** *(schema only — not produced by this build)* — how a claim
would be disposed of: `fixed`, `rejected`, or `accepted-risk`. There is no
`af resolve` command in this build to create one.

## Reading it directly

```bash
jq -c 'select(.type=="claim")' claims.jsonl
```

Every claim in a `report`-mode run has `round: 1`, since there is only ever
one round. `exact_merge` normalizes only whitespace runs and case (plus a
bare strip on `location`) before comparing claim text — it deliberately
under-merges rather than guess at equivalence, so two friends describing the
same defect in different words will usually appear as two separate `claim`
records rather than one, and duplicates you notice yourself should be merged
in your presentation, not in the ledger.

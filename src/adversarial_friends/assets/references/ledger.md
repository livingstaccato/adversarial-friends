# The claim ledger

`claims.jsonl` is append-only. Each line is one JSON record with a `type`
field. **All four record types are written by this build**, by different
modes: `--mode report` writes `claim` and `alias`; `--mode crossexam` (and
`gate`, and `loop`) also write `verdict` records plus the successor `claim`
records a unanimous amendment produces; `afriend resolve` writes
`resolution`.

**claim** — an assertion about the artifact. `id` is versioned (`c-0007@2`);
an amendment creates a new version rather than editing in place, so a
verdict is never ambiguous about which wording it judged. A successor carries
`supersedes` naming the exact version it replaces, and an `origin` that is the
union of the original author and every amending judge — none of whom may then
judge the rewrite. `origin` is a
list of `cli/lens` strings identifying which friend(s) produced *that
record* — not, in general, every friend who ever agreed with it. The ledger
is append-only, so when a later friend's claim turns out to be an exact
duplicate of an earlier one, the earlier (canonical) claim's own record is
never rewritten in place to add the newcomer's origin — see **alias** below
for where that corroboration actually lives, and "Reading it directly" for
how to recover it. `advisory` is derived from the originating lens's
`requires_failure_scenario` frontmatter field (`false` there means `true`
here) — currently only `scope`-lens claims come back `advisory: true`; every
other lens's claims are `false` (see `SKILL.md`'s "Choosing lenses" section
for what that means in practice, and for the one thing this does *not* do:
the schema still requires `failure_scenario` on every finding regardless of
lens).

Example, taken from a real run:

```json
{"type":"claim","id":"c-0001@1","supersedes":null,
 "origin":["fake/security"],"lens":"security","round":1,"advisory":false,
 "severity":"high","claim":"the guard is missing",
 "location":"src/auth.py:42","evidence":"src/auth.py:38",
 "failure_scenario":"expired token reaches the handler",
 "suggested_fix":"check exp before dispatch"}
```

**verdict** — one judge's ruling on one claim version, written by every
judging mode (`crossexam`, `gate`, `loop`) from round 2 on. `verdict` is
`upheld`, `refuted`, `amended`, `unproven`, or `out-of-scope`; the first
three are dispositive and the last two are not, which is what decides
whether a claim can settle. `judge` is that friend's roster identity and
`round` is the round it was cast in — a judge appearing twice on one claim
in different rounds is normal, and only its **latest** verdict counts toward
the claim's state.

`evidence_assessment` (`confirmed`, `disputed`, or `unverifiable`) records
whether the judge could actually check the evidence the claim cited, and is
required on the dispositive verdicts. `unverifiable` there downgrades the
verdict to `unproven` before anything counts it, so a claim is never
dismissed on the strength of nobody having looked; `disputed` requires
`counter_evidence` saying what the judge found instead. `amended_claim`
carries the rewrite on an `amended` verdict — unanimous amendments mint the
successor `claim` record that `supersedes` this one.

```json
{"type":"verdict","claim_id":"c-0001@1","judge":"fake/ops","round":2,
 "verdict":"upheld","confidence":"high","evidence_assessment":"confirmed",
 "reasoning":"dispatch happens before any exp check",
 "counter_evidence":null,"amended_claim":null}
```

**alias** — a merge decision: `duplicate` is an exact (whitespace/case-
insensitive text-and-location) match of `canonical`. **Both ids have their
own, full `claim` record in the ledger** — a claim that gets aliased away is
never dropped, only marked as a duplicate of another; this is what lets a
reader recover full corroboration from `claims.jsonl` alone, without needing
the in-memory state a live `afriend run` process held (see "Reading it directly"
below). `source` is `exact` for the deterministic merge every mode performs, or
`orchestrator` for a merge an operator adjudicated under `--merge
orchestrator` — that one carries the `rationale` the operator wrote, and is
the only way two *differently worded* claims ever become linked.

```json
{"type":"alias","canonical":"c-0001@1","duplicate":"c-0002@1",
 "round":1,"source":"exact","rationale":"identical claim text and location"}
```

**resolution** — how a claim was disposed of: `fixed`, `rejected`, or
`accepted-risk`, written by `afriend resolve`. `verified` records what the
runner could actually check about the `evidence` location:
`location-changed`, `location-unchanged`, or `unverifiable`. Read it as an
attestation rather than proof — the runner cannot know a defect is gone, only
whether the named location moved. `unverifiable` means it could not even
check that much. See `modes.md` under **Gate** for the full rule.

## Reading it directly

**Naively counting `claim` records over-counts distinct findings, and
reading any one record's `origin` under-counts corroboration — both errors
point the same way, toward "several independent findings" when the truth is
often "one finding, several friends."** Since an aliased claim keeps its own
full record (see **alias** above),

```bash
jq -c 'select(.type=="claim")' claims.jsonl
```

lists one entry per *record*, not one per distinct finding — a claim that
got merged away is still in there. To count distinct findings, exclude every
id that appears as some alias's `duplicate` (its finding is already counted
via its canonical):

```bash
jq -s '
  ([ .[] | select(.type=="alias") | .duplicate ]) as $dupes
  | .[] | select(.type=="claim")
  | select((.id as $id | $dupes | index($id)) | not)
' claims.jsonl
```

For corroboration, don't trust any single record's `origin` in isolation —
union the canonical's `origin` with the `origin` of every claim whose id is
that canonical's `duplicate` in some `alias` record. `report.md`'s
**"Raised by"** line under each finding is exactly this union, already
computed for you; read it there rather than reconstructing it from raw `jq`
unless you specifically need the ledger-only view.

Every claim in a `report`-mode run has `round: 1`, since there is only ever
one round; a judging mode's ledger spans several, and a `loop` gives each
iteration its own block of round numbers rather than reusing round 1.
`exact_merge` normalizes only whitespace runs and case (plus a bare strip on
`location`) before comparing claim text — it deliberately under-merges
rather than guess at equivalence. Under the default `--merge exact`, two
friends describing the same defect in *different words* produce two `claim`
records with **no `alias` between them at all** — that pair is genuinely two
separate, unlinked findings as far as the ledger is concerned, and
duplicates you notice yourself should be merged in your presentation, not in
the ledger. `--merge orchestrator` is the one path that links them, and the
`alias` it writes carries `source: "orchestrator"` plus the rationale
whoever adjudicated it gave.
Two friends producing the exact *same* text are a different case: still two
full `claim` records (nothing is ever dropped — see **alias** above), but
this time linked by an `alias` record that says so, which is exactly the
distinction the counting/corroboration recipes above exist to recover.

# Adversarial Review of the Staged Hardening Plan

Date: 2026-08-29
Artifact: `docs/superpowers/plans/2026-08-29-staged-hardening.md`
Mode: `report`
Run: `/Users/tim/.local/state/adversarial-friends/runs/run-20260829T191051-5019986e`

## Review coverage

The plan was independently reviewed by:

- `claude:spec-vs-reality`, repo scope, read-only capability, status `ok`;
- `agy:assumptions`, repo scope, read-only capability, status `ok`.

Both friends completed. The run produced five claims. The only downgrade was
the normal environment allowlist notice: 48 variable names were withheld and
no values were exposed. That did not limit either reviewer's repository access.

## Adjudication

### Accepted — tolerant alias replay was lost in the reducer draft

Claude correctly identified a contradiction between Phase 1's explicit
preservation of tolerant alias reconstruction and the first `ReviewState`
draft, which raised on missing alias endpoints. Even though current writers
append claim records before aliases, making a genuinely dangling endpoint a
corrupt or historical shape rather than the ordinary crash fixture, changing
replay tolerance inside the reducer migration would still be an undocumented
policy change.

Revision:

- the design now names dangling and non-topological aliases as a targeted
  compatibility case;
- `ReviewState` records the alias, retires the duplicate as the current
  reconstruction does, and emits `transition_warnings` instead of inventing
  provenance or aborting replay;
- live orchestrator and merge validation remain strict before append;
- Task 7 now runs reducer reconstruction over the partial-merge crash fixture
  and exposes compatibility warnings as run downgrades.

### Rejected as stated — one helper process per HTTP request causes scale collapse

Agy argued that a spawned Python helper per HTTP call could exhaust CPU,
memory, process limits, or wall-clock budget. The general cost exists, but the
claimed severity and proposed remedy do not fit this repository:

- dispatch already runs one OS process per executable friend and bounds fan-out
  through the roster, `--max-friends`, `--max-calls`, and wall-clock ceilings;
- the shipped HTTP path is currently for a small number of model friends whose
  requests take seconds or minutes, so interpreter startup is not the dominant
  cost;
- `aiohttp` and `httpx` would violate the package's explicit zero-runtime-
  dependency contract, while implementing cancellable TLS sockets directly
  would add substantially more risk than a killable helper.

No architectural change was made. The plan does retain bounded joins, explicit
terminate-then-kill escalation, a named helper, and a test proving that no
helper remains alive after cancellation.

### Rejected as a writer-produced scenario; compatibility impact addressed

Agy proposed that reverse-topological alias chains could be emitted in one
round and crash replay. The current orchestrator parser explicitly rejects any
claim id appearing as both canonical and duplicate, directing the operator to
name the final canonical. Exact merge always targets the current live
canonical and does not emit chains either. The stated production path is
therefore infeasible under current write invariants.

Historical or hand-edited ledgers can still contain non-topological aliases.
The accepted compatibility revision above means those now produce a visible
warning rather than the crash in the reviewed draft, without weakening current
writer validation.

### Accepted — lexical containment mishandled symlinked roots

Agy correctly noted that `.absolute().relative_to(...)` compares lexical path
forms. On macOS, `/tmp` and `/private/tmp` can name the same location while
failing that comparison. The plan now resolves the repository root and
candidate before containment, adds a symlink-root regression test, and treats
an in-repository symlink whose target escapes the repository as unverifiable.
The artifact path keeps its separate no-final-symlink-following rule so the
original invocation identity is preserved.

### Accepted — stale source range

Claude correctly observed that `persist_result` begins at
`src/adversarial_friends/rounds.py:311`, not line 330. The Task 9 file map now
uses `rounds.py:311-364` so an implementing worker sees the signature as well
as its returned metadata.

## Additional self-review corrections after the external pass

The external report did not cover every design-to-plan mismatch. The final
revision also:

- changes the report API to consume one `ReviewState`, rather than continuing
  to accept independently reconstructed claim, alias, and verdict lists;
- renders every proposed amendment when conflicting rewrites remain contested;
- adds concrete generated successor records to the incremental/replay property
  test;
- tests short ledger writes plus malformed middle, tail, and typed records;
- records explicit transport metadata at every `persist_result` call site;
- tracks OS confinement at the exact branch where a sandbox wrapper is
  applied; and
- chooses `0.2.0` as the release version for the completed semantic change set.

## Result

Three claims were accepted directly, one was rejected on repository evidence,
and one was rejected as a current-writer scenario while its historical-ledger
risk was covered by the accepted compatibility change. The staged order
survived review: urgent correctness repairs remain Phase 1, the replay reducer
remains Phase 2, and policy/reporting changes remain Phase 3.

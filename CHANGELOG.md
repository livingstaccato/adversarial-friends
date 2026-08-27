# Changelog

## Unreleased

### Doc scope was the unguarded half

Every friend is downgraded to doc scope when the artifact is not inside a git
repository. That path had never been exercised against a real CLI. Two
defects, found by finally running it:

- **codex could not run in doc scope at all.** It refuses to start outside a
  git repository — `Not inside a trusted directory and --skip-git-repo-check
  was not specified` — so it failed before the model saw the prompt, every
  time. Adapters can now declare `doc_argv` for flags a CLI needs in order to
  *start* in a bare directory.
- **Doc scope dropped the CLI's own read-only mode.** The reasoning was "doc
  scope has no repo to protect", which skips that there is still a
  filesystem — and that doc scope is exactly where a read-only-capable CLI
  gets no OS confinement either. Measured, not inferred: real codex in a bare
  directory with no `--sandbox` flag, asked to write outside its working
  directory, did so on the first attempt. Fixing only the first defect would
  have turned "fails to start" into "starts able to write anywhere", so the
  two ship together.

### A crossexam that produced zero verdicts

Pointed `--mode crossexam` at `verdicts.py` with codex, agy, and claude. Two of
three friends failed every round; the failures were worth more than the
verdicts would have been.

- **claude had never produced output under a schema.** `--json-schema` takes
  the JSON itself; every adapter was handed a file path, so claude failed
  before the model saw anything. The third of three native-schema adapters
  found broken the same way, after codex and agy in 0.1.1 — and for the same
  reason: no test ran a real CLI under a schema. Adapters can now declare
  `schema_inline`, and claude's envelope reads `structured_output`.
- **Discard fired on nothing.** Once the repeat tracker disabled both other
  judges, two more rounds ran with nobody dispatched and every claim ended
  `discarded` — "judges looked twice and could not verify" — when no judge
  had looked at all. A judge the tracker withholds now counts as one that
  never reported (§7.2 M12), those claims read `incomplete`, and a round in
  which every judge is withheld ends the run instead of burning the rest.
- **Discard compared non-consecutive rounds.** `unproven` in round 2,
  `contested` in round 3, `unproven` again in round 4 was compared against
  round 2 and discarded — closing a claim with live disagreement on the
  record. codex raised this while reviewing the file; a reachability test
  confirmed it before it was fixed.
- **The first real auth marker.** agy's login had lapsed, and it said so only
  on stderr: `Error: authentication required. Run 'agy' to log in, then
  retry.` §14's marker kinds could not express that, so adapters may now
  declare `stderr_contains` — restricted to a sentence captured verbatim from
  a real failure (recorded as a divergence in the spec's §20). Beside it,
  the near-miss that must not be adopted: `authentication timed out` is what
  agy says when it cannot *reach* the auth endpoint.

### What the second crossexam found

The same three friends, run again on `verdicts.py` after those fixes. All
three succeeded in rounds 2 and 3 -- twenty verdicts, two claims
settled-upheld -- and the round-1 failures, the verdicts, and a two-day-old
process found along the way each turned into a fix.

- **codex's real findings were dropped.** Under `--output-schema` codex emits
  its progress narration as `agent_message` events, each forced into the
  schema's shape: "I'm inspecting the repository..." arrived as a valid
  findings object with `location: null`, before the answer. The normalizer
  keeps the first candidate that ranks best, so the progress line was
  recorded as a claim (and duly discarded) and the answer -- a high-severity
  finding about amendment wording -- was never seen. An NDJSON envelope now
  offers its matches latest-first: in an event stream the final event is
  the answer.
- **The abort handler could deadlock the run it was aborting.** A second
  SIGTERM pending while the handler's first invocation was inside
  `abort_event.set()` ran the handler again, nested, on the same thread; the
  nested `set()` blocked on the lock its own caller held. Found as a process
  from a crossexam two days earlier, still alive with five invocations
  nested on its main thread; reproduced with three back-to-back signals --
  GNU `timeout` alone sends two. The handler is re-entrancy guarded now, and
  a probe forces the interleaving in the suite.
- **`incomplete` was run-level.** The fix above made a withheld judge count
  as one that never reported -- for every below-quorum claim in the run, so
  one unrelated friend's failure marked claims whose own judges had all
  reported `incomplete` and reset their discard signatures. The judges of
  this run raised it. It is per claim now: a claim is `incomplete` when one
  of *its* judges was silent; the run-level flag stays.
- **`discarded` cleared a gate.** The spec says everything but
  `settled-refuted` needs a Resolution; the comment above the set said only
  `settled-refuted` clears; the set also cleared `superseded` and
  `discarded` (settled-upheld by both judges). A discarded claim is one
  nobody could check, and a gate passing on that is the failure the tool
  exists to prevent -- it blocks now. `superseded` is exempt rather than
  clearing: its successor carries the question.
- **The late-amendment note fired for any downgraded amendment**
  (settled-upheld by both judges): the evidence rule rewrites `amended` to
  `unproven` in any round, and the detector could not tell that from the
  final-round rewrite, so a round-2 amendment with unverifiable evidence was
  reported as "in the final round ... counted as upheld", with advice to
  add rounds that would change nothing.
- **agy's own error message was hidden.** `{"status":"ERROR","response":"",
  "error":"timeout waiting for response"}` was reported as "the adapter may
  need an envelope path". A json_path envelope can now name an
  `error_path`, read only after normalizing has failed, so the failure
  leads with what the CLI said. agy then stayed alive until the runner's
  900 s ceiling and left orphans -- its problem, but a fifteen-minute round
  is the cost.

A duplicated block in `cmd_run` also ran the resolve/validate/downgrade
sequence three times over, calling `resolve_friends` three times and
reassigning `specs` *after* confinement had been computed from an earlier
copy. Visible in any real report as the same downgrade printed twice.

## 0.1.1

**Upgrade from 0.1.0 if you use `codex` or `agy`.** Both shipped schemas were
rejected by every schema-enforcing CLI, so cross-examination could not work
with either friend. Found by pointing the tool at its own source with a real
roster — the first thing it did was fail two of its three friends.

- **codex had never produced output under a schema.** OpenAI's strict
  structured-output mode requires `additionalProperties: false` on every
  object and `required` naming every property; neither schema had them, so
  the API rejected the request before the model saw the prompt.
- **agy failed every judging round.** The verdict schema's
  `evidence_assessment` enum contained `null`, which it rejects outright.
- The friend prompt contradicted the fixed schemas, telling friends to send
  one of `findings`/`no_findings` when strict mode requires both.

None of this was caught by 700 tests, because every test used the fake friend
(no schema) or ollama (`schema=False`).

### Confinement

Two holes straight through the middle of the sandbox, from the same review:

- **The environment was not filtered.** A confined friend inherited every
  secret exported in the runner's shell — 61 variables on the machine this
  was found on, four of them API tokens for unrelated services — readable
  without touching a single forbidden path. It now receives an allowlist,
  and the run records how many names were withheld (names only, never
  values).
- **Host-local networking is denied on macOS.** `127.0.0.1` was reachable, so
  a local database or another dev server was one request away.

Still open, and stated rather than implied: SBPL cannot filter numeric IPs, so
cloud metadata stays reachable on macOS; `bwrap` has no selective filtering at
all, so Linux keeps shared networking. Both need an egress proxy, which was
investigated and deliberately not built -- the macOS half is viable
(`localhost:PORT` does parse, and codex and agy both honor `HTTPS_PROXY`), the
Linux half has no stdlib answer, and the whole thing stops lateral movement
rather than exfiltration, since a friend must reach its own model to work.
`sandbox.py` records the measurements so the next attempt starts from them.

- The binary allowlist assumed a CLI's libraries sit beside its executable.
  They do not for any package-manager layout — `opencode` keeps a 61MB
  `node_modules/` beside `bin/`.

## 0.1.0

First release. Dispatches a spec, plan, or review to other agent CLIs as
independent adversarial reviewers, then makes them argue about what they
found.

### Modes

- **`report`** — one round, every friend critiques in parallel, claims merge
  into one ranked report.
- **`crossexam`** — friends then judge the claims they did not write, blind,
  until each settles, deadlocks, or hits a ceiling.
- **`gate`** — every non-advisory claim that did not clear needs an explicit
  resolution. This is the mode that fails a build.
- **`loop`** — repeats until two consecutive rounds surface nothing new.

`afriend resolve` records a resolution and re-reports the gate. `afriend
init` writes a roster from what is installed. `afriend doctor` reports what
each friend can actually enforce, with `--json` and `--gc`.

### Friends

`claude`, `codex`, `agy`, `opencode`, and local models over `ollama`'s HTTP
API. Adapters declare what each CLI can enforce rather than assuming: schema
support, read-only mode, whether its effort level can be verified at all.

### What it refuses to fake

Most of the design work went into *not* claiming more than is true.

- **Blind presentation.** A judge is never told who wrote a claim — not the
  friend, and not the lens either, since round-robin assignment makes a lens
  identify its author just as surely.
- **Deadlocks are reported, never resolved.** Two judges who disagree leave
  the claim `deadlocked` with both sides quoted verbatim. Nothing here is
  entitled to break the tie by majority.
- **A judge that could not check the evidence settles nothing.** A verdict
  carrying `unverifiable` is downgraded before anything counts it, so no
  claim is ever dismissed on the strength of nobody having looked.
- **A resolution is an attestation.** The runner cannot know a defect is
  gone; it checks whether the location you named changed, and says which of
  three things it found. The one thing it refuses is `fixed` at an unchanged
  location.
- **Deduplication is judgment.** `--merge exact` under-merges on purpose;
  `--merge orchestrator` halts with exit 10 and asks.
- **No `--max-spend-usd`.** A dollar cap needs per-CLI cost reporting nobody
  has captured, and a flag that silently never fires is worse than none.
  `--max-calls` is derived from your roster and actually enforced.

### Containment

A CLI with no read-only mode of its own runs under `sandbox-exec` (macOS) or
`bwrap` (Linux), or it is refused. The macOS profile was built by
measurement, not documentation. What it removes is other repositories, SSH
and cloud keys, and the rest of your home directory; what it cannot remove is
network access and the friend's own credentials, which it needs to work at
all — stated plainly rather than implied away.

Rosters supply values only. There is no mechanism for a file to inject a
flag, and a repo-local roster is never loaded automatically: a cloned
repository does not get to choose who reviews it.

### Known gaps

- Only agy declares an auth marker (captured from a real failure; see
  Unreleased). The others stay unclassified until theirs is captured —
  guessing at stderr is what the design rejects. Repeat detection covers the
  cost meanwhile: a friend that fails identically twice stops being
  dispatched.
- A doc-scope friend of a read-only-capable CLI is not OS-confined. Its own
  read-only mode is now engaged there (see Unreleased), so it is no longer
  unrestrained — but OS-level confinement still needs verified credential
  paths for those CLIs.
- `--merge orchestrator` is refused with `--mode loop`.

See `docs/superpowers/specs/` for the design and its recorded divergences.

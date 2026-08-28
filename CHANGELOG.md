# Changelog

## Unreleased

### opencode had never been dispatched, and could not have been

The first crossexam to include `opencode` — the one adapter with no schema,
no read-only mode of its own, and therefore the only one that runs under OS
confinement. It failed in 0.06 seconds, in every round, with `error: An
unknown error occurred (Unexpected)`. It had never been dispatched in a real
run before, so the confinement path shipped in 0.1.0 had never executed
against the CLI it exists for.

Three separate things were wrong, each hidden behind that one message:

- **It could not write its own log.** opencode opens
  `~/.local/share/opencode/log/opencode.log` before it will do anything and
  exits if the open fails. The profile granted that directory read-only.
  Adapters can now declare `[sandbox] write`, which stays empty for every
  adapter that has not earned it.
- **It could not read the temp directory.** It runs on bun, which keeps its
  cache directly in `$TMPDIR` — so granting the friend's own isolation
  directory, which lives there, was not enough. Adapter paths now expand
  `$TMPDIR` as well as `~`, and a declared path is granted alongside its
  real path: `/tmp` is a symlink to `/private/tmp` and `$TMPDIR` sits under
  `/var/folders`, so granting only what was written granted nothing at all.
- **It had no credentials.** `opencode auth list` reports where it actually
  looks, and on the machine this was captured from it named `GEMINI_API_KEY`
  and `HF_TOKEN` — neither in the adapter's allowlist, so a confined
  opencode reached its provider and was refused by it.

With those fixed it starts, runs for a minute rather than dying instantly,
and reaches its provider. It also produced the first auth marker of the kind
§14 actually describes — a CLI naming the failure in its own structured
output, `{"error":{"name":"ProviderAuthError"}}`, rather than the stderr
sentence agy needed. agy's remains a recorded divergence; opencode's is the
shape the spec asked for.

The machinery around the failure behaved correctly throughout, which is worth
recording: the environment was filtered to eight variables, the repeat
tracker disabled opencode after two identical failures, the run reported
itself incomplete, and the shrunken roster was named in the report.

Cross-examining `sandbox.py` immediately afterwards found three defects in
that fix, and the corrections collapse into something better than what it
replaced:

- **The `$TMPDIR` grant exposed every other friend.** Isolation directories
  are created there, so reading it meant reading other friends' trees and
  every other same-user temporary file.
- **One log file was paid for with the whole state directory**, which
  outlives the run: a friend could corrupt saved sessions or auth state that
  a later run depends on.
- **The isolation directory itself was the one path never resolved**, so on
  macOS the profile granted `/tmp/...` while the kernel saw
  `/private/tmp/...` — a friend denied write access to its own working
  directory, by the module that states the resolve-paths rule for
  everything else.

A confined friend now gets a private scratch and state directory *inside*
its isolation directory (`TMPDIR`, `XDG_DATA_HOME` and their neighbours), so
opencode writes its log where the round deletes it and needs no grant
outside the boundary at all. Redirecting where a CLI thinks its state lives
beats widening the sandbox to reach the real one.

### Three friends were inheriting every secret you export

The same crossexam's highest finding, and the only one in this batch that
was not about a change made an hour earlier.

Environment filtering was gated on the same condition as filesystem
confinement — `if not adapter.readonly_argv` — so codex, claude and agy, the
three CLIs that confine themselves, received no environment at all and
inherited the operator's whole shell. A read-only flag stops a CLI writing
files; it does nothing about what it can read out of its own environment,
and an artifact that talks a friend into echoing `env` sends every exported
token to a model provider. The allowlist 0.1.1 introduced — and whose
arrival that release announced as closing this hole — only ever applied to
opencode.

Every exec friend is filtered now. Verified against all three before the
coupling was cut: each authenticates under the allowlist, because their
credentials are files under `HOME` rather than variables.

The run's record follows dispatch again — every exec friend counted, HTTP
friends excluded because there is no child process to deny anything to, and
a host with no sandbox now reports that the filesystem is unconfined *while
the environment is still filtered*. The previous wording understated the
protection as badly as the original overstated it.

### Waiting twelve minutes for an answer already in the pipe

agy, on its error path, writes its JSON and then does not exit until its own
`--print-timeout` elapses. Measured across seven cross-examinations: eleven
successful runs exited about 2.5 seconds after the work they reported, and
all three hangs exited at 906 seconds having reported 163, 372 and 482 — up
to twelve minutes spent waiting for a process that had already answered.

Nothing on this side was wrong: stdin is closed, and §11.3's timeout
reconciliation was doing exactly what it says. So the fix is narrow. A
`json_path` adapter's contract is that stdout **is** one JSON object, so
once it parses, the answer is in hand and the run stops waiting. Restricted
to that envelope kind on purpose: an NDJSON friend streams events and codex
emits a schema-shaped progress message *before* its real findings, so the
same check there would truncate the thing it is waiting for. The run records
`stopped_after_answer`, because a reader comparing a friend's wall clock
against the CLI's own report of itself should not have to guess.

Found while testing it: `cwd=str(cwd)` turned `None` into a literal
directory named `None`, and Popen's `FileNotFoundError` was reported as
**"binary not found"** — sending a reader to hunt for a CLI that is
installed. It now names whichever is actually missing.

### `--merge orchestrator` works with `--mode loop`

It was refused, and the refusal was honest about why: a loop halts once per
iteration and would resume into mid-flight state — a budget, a dry-round
streak, a claim set — that the build had never reconstructed.

All of it was already on disk. States and notes in `run.json`, verdicts in
the ledger, discard signatures derivable from those, iteration and streak in
the metadata. The one thing genuinely missing was that **the halt path
recorded none of it**: the completion path wrote `iterations_run`,
`dry_streak` and `claim_states`, so a resumed loop re-entered knowing only
that it had been interrupted. Both paths build the run's metadata the same
way now — a halted directory a resume cannot read is worse than no halt.

A resumed loop re-enters the iteration it halted in, inherits what earlier
iterations decided, and carries on; the next iteration halts for its own
adjudication.

### A binary in `~/bin` granted the whole home directory

From the same crossexam, and confirmed by running it before it was fixed: a
CLI installed as a real file in `~/bin` or `~/.local/bin` — the normal shape
for a curl-installer or a single-file binary — was granted
`(subpath "/Users/<you>")`, read. That is the exact thing this module's own
docstring says confinement removes.

The rule was "the parent of any directory named `bin`", which is right for a
symlink into an installation (`~/.opencode/bin/opencode` needs the 61MB
`node_modules/` beside it) and wrong for everything else: a real file in
`~/bin` resolves to `~/bin`, whose parent is your home directory. An install
root must now look like one — a `lib`, `libexec`, `share` or `node_modules`
sibling — and the home directory is never one, whatever it happens to
contain.

Two related corrections, both about the boundary describing itself
accurately:

- The generated profile is written into the run directory **to be audited**,
  and its comment claimed link-local and cloud-metadata endpoints were
  denied while emitting only a `localhost` rule. It now says outright that
  SBPL cannot express a numeric address and that `169.254.169.254` stays
  reachable.
- `--ro-bind-try` was documented as used "throughout" when the system set
  and the working directory bind hard, and `read_paths` was documented as
  "the resolved path of the binary" when it carries up to three directories.

The rest of the fifth crossexam's findings -- the eight the 0.1.2 batch left
open -- plus two defects that surfaced while testing them.

- **Friends reviewed the live file, not the frozen copy.** §4.1 lists "the
  frozen artifact" among a friend's three inputs and `artifact_hash`
  attests to those bytes, but every dispatch re-read the live path, which
  made the frozen read dead code. An artifact edited between a halt and a
  resume was judged while run.json still reported the original hash, and
  `afriend resolve` compared locations against a copy nobody had reviewed.
  A loop re-freezes per iteration instead, so the copy, the hash and what
  friends read are the same bytes.
- **A `fixed` resolution could be accepted for a file nobody touched.** The
  snapshot was taken only for repo-scope friends or `gate`, but `afriend
  resolve` accepts any run directory and never reads the mode -- so a
  doc-scope crossexam recorded no snapshot, every location verified as
  `unverifiable`, and `unverifiable` does not refuse `fixed`. The snapshot
  is taken whenever there is a repository; it is a commit object built from
  the index, with no worktree and no checkout.
- **A symlinked artifact reviewed the wrong repository.** Repository
  selection resolved the artifact before asking git which repo enclosed it,
  so `repo-A/docs/spec.md -> repo-B/spec.md` snapshotted repo-B. The path
  the operator names picks the context; the link's target supplies only the
  bytes.
- **Two concurrent resumes shared one run directory.** A fresh run is
  protected by the "already exists" refusal, but a resume deliberately
  reopens a directory that has one, so two CI workers could dispatch the
  same round twice, append duplicate records to one ledger, and overwrite
  each other's run.json. An advisory lock now refuses the second with an
  explanation.
- **The fan-out was unbounded**: one thread, one process and one worktree
  per friend, all at once. A large generated roster could exhaust file
  descriptors or a provider's rate limit before repeat detection saw a
  single failure. Bounded at eight, which a hand-written roster never
  reaches.
- **The wall-clock ceiling bounded the gaps between rounds, not the run.** A
  friend dispatched a second before it expired ran its own full timeout
  past it -- 900 seconds by default -- and a run that finished in that
  round reported no ceiling hit at all. Every friend's timeout is capped at
  what is left.
- **Nothing had ever executed the wall-clock branch.** `Budget.out_of_time`
  had a unit test; the code that calls it did not, because an end-to-end
  run cannot wait two hours and the check read the clock directly.
  `AF_CLOCK_OFFSET_S` moves the clock the run reads, so the same arithmetic
  runs against a clock a test can advance.

Two more, both found by writing that test:

- The first clock injection cancelled itself out -- the offset was added to
  the run's start as well as to each reading -- so the ceiling could never
  be reached however far the clock was moved.
- A ceiling hit in the iteration loop exited **1**, not 11: `ceiling_hit`
  was only ever read off the crossexam outcome, and a budget exhausted
  before any crossexam existed left the operator with a plain failure and
  no mention of the ceiling they had set.

Also: dead comments and a dead type alias left by the `commands/` split are
gone, `JUDGING_MODES` is defined once rather than twice, `cmd_run` crossed
the line cap again and the revision an iteration reviews now lives in one
function (`environment.freeze_revision`), and the README installs from PyPI.

## 0.1.3

**The first release published to PyPI.** 0.1.2 was tagged but never
published, because building it turned up the reason to look: `afriend
--version` printed `0.1.0` from a 0.1.2 wheel.

- **The reported version had drifted two releases.** `__version__` was a
  literal in `__init__.py`, and nothing compared it to `VERSION` -- the
  file that drives the build, the plugin manifests and the wheel metadata
  all agreed with each other while the string a user actually sees did not.
  It is derived now: from `VERSION` in a checkout, from distribution
  metadata when installed. `scripts/check_version_sync.py` checks it
  alongside the manifests, so the next spelling that reintroduces a literal
  fails the gate.
- **The test for it passed by construction.** `test_af_reports_version`
  asserted the output started with `"afriend "` and never looked at the
  number, which is exactly how an installed 0.1.2 could print 0.1.0 with
  the suite green. It compares the number now.

## 0.1.2

**Upgrade from 0.1.1 if you use `--mode gate`, `--mode loop`, or run friends
confined.** A gate could clear without checking anything, a run's record of
withheld secrets could describe a filter that never ran, and a loop could
judge one revision's wording against another revision's code.

Everything below was found by pointing the tool at its own source: five
cross-examinations, each one reviewing the file the previous one's fixes had
just changed. Every round found defects in the round before it.

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

### What the third crossexam found

Run again after those fixes, same roster, same file. Every friend succeeded
in every round; nine claims, twenty verdicts, seven settled-upheld, none
garbage, none discarded. Two of the upheld claims changed rules that were in
the spec.

- **A final-round amendment was rewritten to `upheld`.** The rule existed so
  no successor could be created with no round left to judge it. On this run
  both judges of one claim said its headline was false, amended it in the
  final round, and the rule turned their rewrites into `settled-upheld` --
  "judges unanimously agreed the claim stands". It was also wrong in loop
  mode (claims carry into the next iteration) and for a lone judge (whose
  amendment could never produce a successor). Gone: an amendment is a
  rewrite in any round, a lone judge's included; a successor created by the
  last round stays `incomplete`, is named in the report, and blocks a gate.
- **The ledger identity dropped the model.** `codex:ops:gpt-5` and
  `codex:ops:gpt-5-mini` shared `codex/ops`: quorum counted two judges,
  one verdict survived, and which one depended on `--friend` flag order --
  flag order could clear a gate. The identity is the roster unit now
  (`cli/lens`, then `@model` and `+effort` when set; existing ledgers are
  unchanged), a repeated entry is refused up front in any mode that judges,
  and `judges_for` counts each identity once.
- **A claim nobody could judge was `discarded` after two rounds**, because
  its verdict signature was `()` both times and `() == ()`. "Judges looked
  twice and could not verify" was being said of a claim no judge was shown.
  An empty signature never discards.
- `gate_blocked` and `summarize` had no callers -- the gate is
  `resolutions.blocking_claims`, whose docstring still said `discarded`
  clears. Both deleted, docstring fixed. `loop_should_terminate` now states
  the precondition its caller meets, and the filter that meets it has a
  test. The judge prompt says an amendment must leave the claim's evidence
  standing, since a successor inherits it.

### What a review of that batch found

The amendment and identity changes above were right about `crossexam` and
wrong about `loop`, where claims carry from one iteration to the next.
Reproduced by running the tool, all three:

- **A superseded claim was re-judged every iteration.** Each iteration
  re-seeded every claim `contested`, so a claim an earlier iteration had
  already settled was judged again -- and an amended one produced a
  successor under the same id each time, since claim ids count versions
  rather than records. A three-iteration loop wrote `c-0002@2` into the
  ledger three times. Terminal is terminal across iterations now.
- **A claim no friend could judge held the loop open forever.** An amended
  claim's successor inherits both the author's and the amenders' origins,
  which on a two-friend roster is the whole roster: no independent judge,
  `unproven` for good. Since the empty-signature fix above (correctly)
  stopped discarding it, the loop waited for it and ran to its iteration
  ceiling -- twelve judging rounds where three would do. A loop no longer
  waits on what no further iteration could change; the claim is still
  reported and still blocks a gate.
- **"No round was left to judge it" was told to the operator once per
  iteration**, for a successor the next iteration went on to carry. That
  ceiling is per iteration; the message now waits for the last one.

Three more from the same review, none loop-specific:

- The run-level `incomplete` flag was being set for an unjudgeable
  successor. It means "a required friend failed" (§7.2 M12) and the report
  says so in those words; no friend had failed.
- The duplicate-identity guard ran before the preset filled efforts and
  before `--model`/`--effort` (§10.1 layer 4), so it missed the collisions
  those create -- `--friend codex:ops:gpt-5 --friend codex:ops --model
  gpt-5` resolves to two friends with one identity -- and it refused
  rosters whose duplicate entry `--max-friends` would have dropped. It runs
  last now, on the roster the run will actually use.
- **`--model` and `--effort` were not restored on resume.** Now that they
  are part of the ledger identity, a run resumed without them re-resolved
  its friends under identities the ledger did not hold, and a claim whose
  author no longer matched its origin was handed its own claim to judge.

Also: an unsettled claim with no verdicts says why instead of sitting under
a heading promising both sides quoted, and `Rounds run:` counts the highest
round the run reached rather than the last iteration's -- which, once a
final iteration could legitimately run no judging round, read "1" for a run
that had just spent eight.

### What the fourth crossexam found

Pointed at `commands/crossexam.py` -- the file the last two commits
changed, and the first target that was not `verdicts.py`. Nine claims,
eight settled-upheld, one settled-refuted. Almost all of them were about
the loop carry-over those commits had just introduced.

**A loop block carried states and nothing else**, and four claims followed
from that one omission. Each iteration built a fresh outcome, so a claim
deadlocked in iteration 1 was printed under "Unsettled" with "No verdict
was cast on this claim" -- the line added one commit earlier -- while both
judges' reasoning sat in the ledger; later blocks' judges never saw earlier
arguments; a required friend's failure in an earlier iteration was
forgotten, so a run that lost a judge reported itself complete; and the
discard rule, which needs two consecutive rounds, could never fire in a
loop whose blocks hold one judging round each. A block now inherits the
previous one's verdicts, notes, discard signatures and `incomplete` flag.

**Carried states were not tied to the artifact.** A loop re-reads the
artifact precisely to pick up a revision, and a claim settled against the
old text is not settled against the new one -- carried across an edit, the
report goes on naming a defect the edit may have removed. The carry now
stops at any change to the artifact, and the run says it re-opened those
claims.

**A friend the repeat tracker disabled was still counted as a judge.**
`RepeatTracker` never clears a disabled friend, so it was recorded as
"missing" every round for the rest of the run: its claims were pinned at
`incomplete`, never `unproven`, so never discardable, so a loop could not
converge on them. It leaves the judging roster now -- quorum counts who can
still vote -- and the shrunken roster is reported rather than implied. The
judges refuted the claim's headline (it does not pin *every* claim, and
disabled friends were never re-dispatched) and upheld the narrower defect.

Three smaller ones from the same run:

- `_prior_verdicts_by_claim` never reduced to one verdict per judge, so
  from round 4 a judge's prompt showed one judge's round-2 and round-3
  verdicts as two anonymous reviewers -- §5.1 strips the judge and carries
  no round. A manufactured consensus, in the prompt the next judge reads.
  The file's own comment listed three sites where this accumulation bug had
  been fixed; this was the fourth.
- The call-budget precheck counted judges the repeat tracker would drop, so
  a run could stop `budget-exhausted` with room for the one judge it would
  actually have dispatched.
- A successor created at the last round of a *non-final* loop block was
  left `contested` with no note, on the assumption the next iteration would
  judge it. The loop can stop first, and such a successor cannot even hold
  it open, so the report said "judges disagreed" about a rewrite no judge
  had seen.

### What the fifth crossexam found

Pointed at `commands/run.py`. Thirteen claims, twelve settled-upheld. Two
of them are the worst kind this tool can find: a run record that asserts a
protection that did not happen, and a gate that cannot gate exiting 0.

- **`env_withheld` described a filter that had not run.** It is the run's
  record that secrets were kept from confined friends, and it was computed
  by passing `--pass-env` into `childenv.withheld`'s *adapter* slot -- so
  the adapter's own pass list was never consulted. opencode declares six
  API keys in its `pass` list, dispatch hands all six to the child, and all
  six were reported as withheld. Nothing checked whether a confinement
  mechanism existed either, so an unsandboxed run that filtered nothing
  still produced a full withheld list. Both fixed: the list is computed per
  friend from the same inputs dispatch uses, only when a mechanism exists,
  and a name counts as withheld only if no confined friend received it. A
  run with no mechanism now says the environment was NOT filtered.
- **§8.3 was a comment, not a rule.** One friend plus `crossexam`, `gate`
  or `loop` must hard-error (exit 3); the code appended a downgrade and
  ran. With one friend no judge is independent of any claim, so a `gate`
  run settles nothing, blocks on nothing, and exits 0 -- CI reads "gate
  clear" from a run that structurally could not check anything. The
  `DEGRADED_MODES` constant that encodes the rule existed and was wired to
  nothing.
- **A loop could review two revisions at once.** The snapshot was taken
  once before the loop, so re-reading the artifact each iteration asked
  friends to judge new wording while repo-scope friends were checked out at
  the old commit: claim and evidence from different revisions, in one
  verdict. The repository is re-snapshotted when the artifact changes.
- **Resume re-resolved the roster.** "A resumed run rebuilds its whole
  configuration from run.json" was not true: `resolve_friends` ran
  unconditionally, the recorded roster was never consumed, and
  `max_friends`, `pass_env`, `unsafe_extra_args`, `i_accept_unsandboxed`
  and `keep` were not restored either. A roster file edited between halt
  and resume, or a CLI installed in the meantime, could change quorum.
- **A test passed by construction**: `test_a_friend_that_recovers_is_not_
  disabled` used two friends that never failed, so the tracker it meant to
  exercise was never engaged.

Two existing tests had encoded the pre-§8.3 behaviour -- a single-friend
gate exiting 1, and the preset test running one friend -- and were changed
to two friends. `cmd_run` crossed the 500-line cap with the refusal in it,
so which friends a run dispatches now lives in one place,
`friends.roster_for_run`, including both rules that can stop a run before
anything is spent.

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

- Three of five adapters declare no auth marker. agy and opencode do, both
  captured from real failures; claude, codex and ollama stay unclassified
  until theirs are captured —
  guessing at stderr is what the design rejects. Repeat detection covers the
  cost meanwhile: a friend that fails identically twice stops being
  dispatched.
- A doc-scope friend of a read-only-capable CLI is not OS-confined, and the
  reason is now measured rather than assumed. codex runs confined given read
  and write on `~/.codex`. claude reports `Not logged in` under any profile
  that does not grant `~/Library/Keychains` — its credentials are in the
  macOS Keychain, so confining it would hand a friend every credential the
  user has, which is worse than the gap it closes. agy is untested, because
  provoking its re-authentication has already cost one login. Its own
  read-only mode is engaged in doc scope (see 0.1.2), so no friend is
  unrestrained; what is missing is read protection, for one CLI, at a price
  that is not worth paying.
- `quorum_partial` (spec §7.2) is not emitted, and will not be: the
  per-claim state and the run-level `incomplete` flag already say it, and a
  third spelling is one more thing to keep true.

See `docs/superpowers/specs/` for the design and its recorded divergences.

# Contract-First Hardening Design

**Date:** 2026-08-30  
**Target:** adversarial-friends 0.2.1  
**Status:** Approved design; implementation not started

## Purpose

Version 0.2.1 closes all ten defects verified during the 0.2.0 dogfood run.
The release will make readiness, provider selection, external authority,
snapshot identity, convergence, and terminal state explicit contracts rather
than conclusions reconstructed independently by each command or renderer.

This is a focused hardening release. It does not replace the ledger, redesign
the four modes, or introduce a general event-sourced runtime.

The repository-wide Python source limit is raised from 500 to 777 lines per
file. Enforcement, current documentation, and tests use 777 consistently;
unrelated values such as HTTP status 500 remain unchanged.

## Verified Problems

The design addresses these observed failures and limitations:

1. Natural `--max-loop-iterations` exhaustion does not set a ceiling or exit
   with code 11.
2. A reachable, model-less Ollama endpoint is counted as usable and can evict a
   dispatchable friend before failing transport validation.
3. Reports describe OS confinement but omit authenticated backend tool and
   connector authority.
4. Resume creates a new snapshot commit instead of preserving the snapshot at
   which the run halted.
5. Gate Markdown omits the explicit decision and blocker claim IDs.
6. Terminal `run.json` omits lifecycle and exit fields and loses checkpoint
   spending and repeat-tracker state.
7. Exact claim merging can treat high-confidence wording variants as novel on
   every loop iteration.
8. Successful reviewers' nonfatal stderr diagnostics are hidden.
9. Markdown escaping is imperfect, and exposed-resource names repeat per
   round.
10. A reviewer disabled after repeated failure can receive a prompt artifact
    without corresponding dispatch metadata.

The provider-selection design also fixes a dogfood orchestration error:
current Codex sessions expose `CODEX_SESSION_ID` and `CODEX_THREAD_ID`, but the
existing host detector does not recognize them. As a result, a Codex-hosted
run can dispatch Codex as though it were an independent friend.

## Design Principles

- One domain decision has one source of truth.
- Reachability is not readiness.
- The host agent orchestrates; it is not an independent reviewer by default.
- Authenticated external authority is denied by default and never granted by
  repository-controlled configuration.
- Resume preserves identity rather than approximating it.
- Exact claim identity and loop novelty are separate concerns.
- Persisted metadata, Markdown, console output, and process exit status are
  projections of the same terminal outcome.
- New configuration is user-owned, narrow, inspectable, and stdlib-only.

## Core Contracts

### FriendReadiness

`FriendReadiness` is the shared result of evaluating a provider before roster
capacity is applied. It records:

- provider and resolved friend identity;
- state: `ready`, `reachable-unconfigured`, `unavailable`,
  `disabled`, `host-excluded`, or `policy-blocked`;
- a bounded human-readable reason;
- the resolved executable or endpoint;
- the resolved model when the transport requires one; and
- the external-tool enforcement strategy that will be applied.

Discovery, explicit-roster validation, `doctor`, automatic lens assignment,
and `--max-friends` consume this same assessment. Only `ready` friends count
toward the cap or receive a lens. This prevents an unusable endpoint from
displacing a runnable reviewer.

An HTTP provider that requires a model is `reachable-unconfigured` until a
model is supplied explicitly or in user configuration. Endpoint reachability
alone is never sufficient. For Ollama, an explicit
`--friend ollama:<lens>:<model>` remains valid. A persistent default model can
be set with `afriend providers set-model ollama <model>`.

### ProviderPolicy

Provider defaults are controlled by a user-owned JSON file at:

- `$XDG_CONFIG_HOME/adversarial-friends/config.json` when
  `XDG_CONFIG_HOME` is set; otherwise
- `~/.config/adversarial-friends/config.json`.

The project never loads provider policy from the reviewed repository. The
file contains a versioned object with a `providers` mapping. Each known
provider may have an `enabled` boolean and a `model` string. Unknown keys or
invalid values produce a configuration error that identifies the file and
field.

The supported management interface is:

```text
afriend providers list
afriend providers enable <name>
afriend providers disable <name>
afriend providers set-model <name> <model>
afriend providers clear-model <name>
```

Writes are atomic and preserve a last-known-valid file if replacement fails.
A sibling lock serializes each complete read-modify-write transaction, so
concurrent provider commands cannot discard one another's changes. Temporary
files are unique, file and directory metadata are flushed best-effort, and
the package remains runtime-dependency-free.

Roster precedence is deterministic:

1. One or more explicit `--friend` flags replace automatic selection.
2. Per-run `--enable-provider` and `--disable-provider` flags override the
   persistent provider setting for automatic selection.
3. Persistent settings override built-in defaults.
4. Readiness filtering removes unavailable, unconfigured, and policy-blocked
   providers.
5. Host exclusion removes the detected host unless `--include-self` was
   passed.
6. `--max-friends` is applied to the remaining ready providers.

An explicit `--friend` can name a disabled or host provider because it is an
intentional roster. `--include-self` affects automatic selection. Disabled
providers are not probed during automatic selection. `doctor` lists every
known provider with its effective state and the policy layer responsible for
that state.

Host detection recognizes current and legacy markers, including
`CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SANDBOX`, and
`CODEX_COMPANION_SESSION_ID` for Codex. An explicit host override remains
available for wrappers and ambiguous nested-agent environments. A Codex host
therefore performs orchestration and synthesis but is not dispatched as an
independent Codex friend by default.

### ExternalToolPolicy

External tool and connector authority is distinct from filesystem/process
confinement. New runs use `deny` by default. The only opt-in is the per-run
`--allow-external-tools` flag; persistent or repository configuration cannot
grant this authority.

Every adapter, regardless of transport, declares whether and how it can
neutralize inherited plugins, apps, MCP servers, tools, or equivalent
provider-managed integrations. Missing declarations mean `unknown`, never
`denied`. The preflight assessment requires a supported denial strategy. If
an installed provider cannot enforce the declared strategy, it is
`policy-blocked` and is not launched.

For Codex, the denial strategy starts without user configuration and disables
app/plugin capability using the supported Codex CLI flags. That removes
user-configured MCP servers and connectors while retaining the model process
and the local scope already governed by the adapter's read-only and OS
confinement policy. Other shipped adapters receive an adapter-specific denial
strategy when their CLI supports one; otherwise they fail closed under the
default policy and `doctor` explains the limitation.

When `--allow-external-tools` is present, the adapter may inherit its normal
provider configuration. The run records the explicit opt-in, the adapter's
known authority sources, and the fact that provider-managed integrations may
exist. It does not claim a complete inventory when the provider cannot supply
one.

Security grants are invocation-local. Resume never restores external-tool
access, unsandboxed execution, arbitrary extra arguments, or passed
environment variables from `run.json`. If continuity requires a prior grant,
the operator must repeat the exact grant on the resume command line; saved
metadata can require re-acknowledgment but cannot grant authority. All
non-grant values restored from metadata are schema- and type-validated before
use.

### SnapshotIdentity

A fresh run creates one immutable input snapshot and records its commit, tree,
artifact path, and artifact hash as `SnapshotIdentity`. Resume loads this
identity from the run and verifies that the saved commit and artifact are
still available and consistent before dispatch.

Resume never creates a replacement snapshot merely because a new process was
started. If verification fails, resume exits without changing the existing
run. Loop-mode artifact revision remains an explicit transition: the new
snapshot points back to its predecessor and the transition is appended to
snapshot history.

Version 0.2.0 runs with a recorded snapshot use that snapshot. Runs too old or
damaged to identify one fail with actionable recovery guidance rather than
silently reviewing a different input.

Legacy snapshots that recorded a commit but not its tree derive the tree from
the validated full-length hexadecimal commit ID, persist the derived value,
and then use the complete identity. Commit-like strings from metadata are
validated before reaching Git argument parsing.

### RunOutcome

`RunOutcome` is constructed before terminal files are rendered. It contains:

- start and finish timestamps plus duration;
- convergence and gate decision;
- ordered blocker claim IDs;
- stop reason;
- ceiling type when a ceiling was reached;
- process exit code;
- attempted and spent calls;
- completed loop iterations and dry-streak state;
- repeat-failure tracker state; and
- partial-failure and downgrade summaries.

Supported stop reasons distinguish at least `completed`, `gate-blocked`,
`max-loop-iterations`, `max-calls`, `max-wall-clock`, `auth-abort`,
`interrupted`, and `runtime-error`. Natural exhaustion of the loop range
becomes `max-loop-iterations`, marks the iteration ceiling, and exits 11 unless
convergence was already achieved.

`run.json`, checkpoint state, Markdown, console output, and the returned exit
code are derived from this object. Exit selection therefore happens before
terminal metadata is written. The terminal document includes a metadata
schema version so future readers can migrate deliberately.

## Runtime Flow

1. Parse CLI arguments and load user provider policy.
2. Detect the host and resolve explicit or automatic candidates.
3. Assess candidates into `FriendReadiness` values without probing providers
   disabled by effective policy.
4. Apply readiness, host exclusion, and capacity rules in the defined order.
5. Refuse to create a run when no dispatch-ready friend remains.
6. Create or verify `SnapshotIdentity`.
7. Dispatch ready friends with the selected external-tool policy.
8. Persist every dispatch or explicit skip as one coherent audit event.
9. Update the exact claim ledger and the separate novelty tracker.
10. Construct `RunOutcome` at convergence, gate completion, ceiling reach,
    abort, interruption, or runtime failure.
11. Render all terminal outputs from that outcome and return its exit code.

## Loop Novelty Without Destructive Merging

Exact canonical IDs remain the durable ledger identity. The tool does not
silently merge semantically distinct claims.

Loop convergence instead uses a conservative `theme_signature`. Claims are
eligible to share a theme only when they have the same normalized source
anchor (path, symbol, section, or other structured location) and high token
similarity across the failure mechanism and consequence. Claims without a
shared source anchor fall back to exact normalized identity. Similarity groups
and their member claim IDs are persisted as advisory duplicate proposals.

A repeated wording variant in an existing high-confidence theme does not
reset the dry streak. The original claims remain visible, independently
addressable, and available for later human or orchestrator merge. Tests pin
both sides of the boundary: obvious wording variants share a theme, while
different failure mechanisms at the same location remain novel.

## Diagnostics and Audit Artifacts

A successful subprocess result may contain meaningful stderr. The round
record therefore stores one bounded, sanitized diagnostic summary and a path
to the full captured stderr even when the friend succeeded. Raw stderr stays
only in the `.err` artifact. Reports surface the safe summary without treating
it as a failure.

Repeat-failure filtering occurs before prompt construction. A reviewer
disabled for the next round receives an explicit `skipped` audit record with
the repeat-failure reason but no prompt-only artifact. Every prompt artifact
that exists therefore corresponds to a dispatch record.

Exposed-resource names are deduplicated in stable first-seen order across all
rounds. Markdown escaping is fixed for inline-code and other affected report
contexts, with golden tests for adversarial names and content.

## Gate and Report Output

Gate reports include a dedicated section with:

- the explicit gate decision;
- ordered blocker claim IDs;
- the outcome and stop reason; and
- any ceiling or partial-evidence caveat.

All modes report the external-tool policy separately from local confinement.
The report states whether external tools were denied, explicitly allowed, or
unknown for a legacy capture. It does not generalize OS-level confinement into
a claim about remote connector authority.

## Errors and Compatibility

- No ready friends: fail during preflight before a run directory is created.
- Missing required model: classify the provider as
  `reachable-unconfigured`; do not consume roster capacity.
- Unenforceable external-tool denial: classify the provider as
  `policy-blocked`; do not launch it.
- Invalid provider config: report the exact file, field, and invalid value.
- Missing or mismatched resume snapshot: refuse resume and preserve the run.
- Natural iteration or budget exhaustion: persist the ceiling outcome and
  exit 11.
- Partial friend failures: continue when the mode's existing evidence
  requirements are met, while recording the failures and diagnostics.

Version 0.2.0 run directories remain readable. Missing lifecycle fields are
reconstructed only when the recorded evidence supports an unambiguous value;
otherwise they remain explicitly unknown. Historical external authority is
reported as `legacy-unknown`. Any new dispatch made while resuming an old run
uses deny-by-default and records that policy transition. Existing explicit
`--friend` syntax and the four modes retain their public behavior.

## Verification Strategy

Development is test-driven. Coverage includes:

- unit tests for readiness, provider precedence, host detection, external-tool
  enforcement, snapshot identity, theme grouping, and terminal outcomes;
- end-to-end tests for iteration exhaustion, model-less Ollama, disabled
  providers, Codex host exclusion, resume preservation, gate reporting,
  successful stderr diagnostics, and repeat-disabled reviewers;
- migration fixtures representing 0.2.0 terminal and checkpoint metadata;
- adapter command tests proving deny-mode construction and fail-closed
  behavior;
- report golden tests for decisions, blockers, authority language,
  deduplication, diagnostics, and Markdown escaping; and
- regression coverage tied to each of the ten dogfood defects.

Release acceptance requires:

- every verified defect has a passing regression test;
- existing explicit-roster behavior remains compatible;
- Codex is excluded automatically when Codex hosts a run;
- disabled providers are neither probed nor auto-dispatched;
- metadata, report, console output, and exit status agree;
- `make quality`, the full test suite, wheel installation, version checks, and
  plugin payload synchronization pass; and
- a bounded live dogfood run dispatches only enabled, non-host providers.

## Implementation Workflow

After this spec is approved, a detailed implementation plan will be written
and challenged with Adversarial Friends before code execution. Accepted
findings will be incorporated into the plan.

Implementation will use subagent-driven development in an isolated worktree.
Each plan task receives a fresh implementer that follows test-driven
development and commits its work. A separate reviewer checks spec compliance
first; only after that passes does another reviewer evaluate code quality.
Open findings return to the implementer and are re-reviewed before the next
task begins. A final holistic review and full verification run follow all
tasks.

## Out of Scope

- Replacing the current ledger with an event-sourced architecture.
- Automatically collapsing the durable identities of semantically similar
  claims.
- Claiming complete external-tool inventories from CLIs that cannot expose
  them.
- Loading security or provider policy from the repository under review.
- Redesigning the four review modes or their core purpose.

# Adversarial Friends Staged Hardening Design

Date: 2026-08-29
Status: Approach approved; written design pending user review

## 1. Purpose

Harden the shipped runner around interruption, replay, provenance, and gate
integrity without mixing urgent correctness repairs into a broad state-machine
rewrite. The work is staged so each phase is independently releasable and the
reducer introduced in Phase 2 is constrained by behavioral contracts added in
Phase 1.

The existing product design remains authoritative except where this document
explicitly changes behavior.

## 2. Success criteria

The hardening is complete when all of the following are true:

1. A resumed run reconstructs the same canonical claims, origins, verdict
   states, successors, and resolutions as uninterrupted execution.
2. A friend that contributed to any alias in a canonical claim's transitive
   provenance can never judge that canonical claim after replay.
3. Resolution evidence is interpreted against the run's recorded context,
   independent of the directory from which `afriend resolve` is invoked.
4. Cancelling an HTTP friend returns within a bounded grace period and leaves
   no worker capable of delaying process exit.
5. Invalid numeric limits and global model names fail before dispatch with a
   usage error and exit code 2.
6. An acknowledged ledger append is durable according to a documented POSIX
   contract, and a corrupt tail produces a targeted diagnostic rather than an
   unhandled JSON exception.
7. Live execution and resume derive ledger-backed review state through one
   reducer.
8. Reports distinguish write protection, repository/document read scope, and
   OS-enforced filesystem confinement.
9. Local quality documentation accurately describes any platform-specific
   difference from CI.

## 3. Delivery strategy

### Phase 1: correctness contracts and targeted repairs

Phase 1 fixes observable defects without reorganizing the state machine. Every
repair begins with a failing regression test and lands as a small independent
commit.

#### 3.1 Transitive provenance

`canonical_claims` must fold the accumulated origin set of the duplicate into
the canonical claim, not only the duplicate claim's original authors. Alias
records are replayed in ledger order. A dangling alias remains recorded and is
not silently repaired.

Tests cover:

- one alias;
- a three-claim alias chain;
- a branch where two accumulated claims merge into one canonical claim;
- mixed exact and orchestrator aliases;
- judge exclusion after reconstruction; and
- equivalence between live merge output and ledger reconstruction.

#### 3.2 Stable resolution paths

Evidence locations are classified before reading:

- an absolute path is used as written and must fall within a reconstructible
  context;
- a relative repository location is anchored at recorded `repo_root`;
- a relative artifact location is anchored at the original artifact path and
  compared with the frozen artifact; and
- a location outside those contexts is `unverifiable`.

Invocation cwd never participates in resolution. A `fixed` disposition whose
evidence is `unverifiable` is refused; the operator can use `accepted-risk`
when verification is intentionally unavailable. This deliberately strengthens
the existing resolution design.

Tests run the same resolution from the repository, a sibling directory, and a
temporary directory and require identical results.

#### 3.3 Cancellable HTTP dispatch

The urllib request runs in a spawned helper process, not a thread owned by a
`ThreadPoolExecutor`. The parent sends only serializable request inputs and
receives a bounded response record over a one-way pipe. On cancellation it
terminates the helper, waits for a short grace period, escalates to kill when
available, closes the pipe, and returns `failure_reason="aborted"`.

The helper retains the existing timeout, response-size ceiling, header
construction, status handling, and redacted argv semantics. Process startup
overhead is acceptable because HTTP friend calls normally take seconds or
minutes. The stdlib-only runtime constraint is preserved.

Tests use a local HTTP server that withholds its response and assert bounded
abort latency and no live helper. Existing timeout, malformed-body, HTTP-error,
and response-ceiling tests remain unchanged in meaning.

#### 3.4 Central invocation validation

`validate_run_args` becomes the single pre-dispatch validation boundary for
global invocation values. The following must be positive integers when set:

- `timeout`;
- `max_friends`;
- `max_calls`;
- `max_wall_clock`;
- `max_loop_iterations`; and
- `require_friends`.

`max_rounds` must be at least one for `report` and at least two for judging
modes. The global model override must satisfy the same `MODEL_RE` as roster and
per-friend models. Invalid values raise `UsageError`, dispatch no friends, and
exit 2. Final argv denylist validation remains defense in depth.

#### 3.5 Ledger durability and corruption diagnostics

The existing run lock remains the single-writer boundary. Each JSONL record is
encoded completely before opening the ledger, written as one append, flushed,
and `fsync`ed before `append` returns. Creation of a new ledger also synchronizes
its parent directory entry. The contract is POSIX local-filesystem durability
after a successful return; network filesystems and storage hardware that lies
about `fsync` are outside the guarantee.

On read, malformed JSON or malformed records identify the ledger path and line
number through `UsageError`. A malformed final line is not silently discarded:
doing so could erase an accepted verdict or resolution and falsely clear a
gate. Recovery remains an explicit operator action against the preserved run
directory.

Tests inject short writes, invalid middle and final records, and an `fsync`
failure. An append is not reported successful when synchronization fails.

### Phase 2: one ledger-backed review-state reducer

Phase 2 introduces `ReviewState`, a pure in-memory representation derived from
ledger records. It owns only ledger-backed review semantics:

- claims and version relationships;
- alias canonicalization and transitive origins;
- latest verdict per independent judge;
- claim settlement states;
- successor relationships; and
- resolutions.

Process execution, budgets, wall-clock tracking, environment filtering, and
run-directory I/O remain outside the reducer.

The reducer exposes two equivalent operations:

```python
ReviewState.replay(records: Iterable[Record]) -> ReviewState
ReviewState.apply(record: Record) -> None
```

For every valid record sequence, replaying the complete sequence must equal
incrementally applying the same sequence. Commands append a record and apply
that record to the current state. Resume reads the ledger and calls `replay`.
Report generation consumes `ReviewState` instead of independently rebuilding
canonical claims or verdict state.

Migration occurs consumer by consumer behind equivalence tests:

1. canonical claims and origins;
2. verdict reduction and settlement;
3. successors and supersession;
4. resolutions;
5. report and resume consumers; and
6. deletion of superseded reconstruction helpers.

The reducer rejects impossible transitions—duplicate claim ids with different
content, aliases to unknown canonical claims after replay completes, verdicts
for unknown claim versions, and successor cycles—with a diagnostic identifying
the offending record.

Property tests generate valid claim, alias, verdict, successor, and resolution
sequences and assert incremental/replay equality after every prefix. Saved
fixtures cover interrupted critique, merge adjudication, cross-examination,
and resolution boundaries.

### Phase 3: policy and release hardening

#### 3.6 Amendment consensus

`amended` is unanimous only when every dispositive judge proposes identical
replacement text after newline normalization and surrounding-whitespace trim.
Different proposed rewrites leave the claim `contested` (or `deadlocked` at the
round ceiling), with all alternatives reported. The runner no longer chooses a
replacement merely by judge sort order.

#### 3.7 Discard equivalence

The consecutive-round discard signature includes judge, verdict,
`evidence_assessment`, normalized `counter_evidence`, and normalized
`amended_claim`. Free-form reasoning and confidence remain outside the
signature: changes to rhetoric or confidence alone do not justify another
full fan-out, while new evidence or a different amendment does.

#### 3.8 Confinement reporting

Run metadata and reports expose three separate properties per friend:

- `write_protected`: the adapter emitted a verified read-only mode;
- `declared_scope`: `doc` or `repo`, describing the prompt/input contract; and
- `os_confined`: the operating system enforced the readable/writable paths.

A friend with read-only argv but no OS confinement is explicitly reported as
having unrestricted same-user filesystem read access. Existing refusals for
write-capable, unconfined friends remain fail-closed.

#### 3.9 Platform and quality accuracy

Package metadata stops claiming OS independence while `fcntl`, POSIX process
groups, and Unix signals are required. Supported platforms are documented as
macOS and Linux.

`make quality` adds the portable wheel-asset and isolated-wheel-install gates.
The Linux bubblewrap assertion remains a named platform-specific CI gate, and
repository guidance states that distinction instead of claiming exact local/CI
parity on macOS.

## 4. Compatibility and migration

Existing valid ledgers remain readable. No record schema changes are required
for Phases 1 or 2. Phase 3 changes settlement outcomes for conflicting
amendments and discard timing; those changes apply when a run is resumed under
the new version and are recorded as behavior changes in the changelog.

The reducer must not rewrite historical ledgers. It reports invalid historical
transitions rather than repairing them invisibly. If compatibility fixtures
expose a historical pattern that was previously accepted, the implementation
must either model it explicitly or provide a targeted migration command; it
must not silently reinterpret evidence or provenance.

## 5. Error handling

- Configuration failures occur before a run directory or friend dispatch when
  possible and exit 2.
- Cancellation returns the existing aborted result shape and signal-derived
  process exit behavior.
- Ledger synchronization failures stop the run and preserve all run artifacts.
- Ledger corruption reports the exact record location and never clears a gate.
- Reducer transition errors identify the record type, claim id, and ledger line
  when known.
- Resolution paths outside reconstructible context remain `unverifiable`, but
  cannot support a `fixed` disposition.

## 6. Test and release gates

Each phase must pass focused tests before the full suite. Phase completion
requires:

```bash
make quality
bash ci/verify_wheel_assets.sh
bash ci/verify_wheel_install.sh
```

Linux CI must additionally execute the real bubblewrap tests and
`ci/assert_sandbox_tested.sh`.

Phase 2 cannot land until incremental/replay equivalence holds for generated
record sequences and every existing resume fixture. Phase 3 semantic changes
must include report snapshots that make disagreement and filesystem-read risk
visible to operators.

After changes under `src/adversarial_friends/assets/`, the canonical payload is
re-synced to `plugins/adversarial-friends/` before the quality gate. Any release
version bump updates `VERSION` and all plugin manifests together.

## 7. Rollout and commit boundaries

The implementation uses small, reversible commits in this order:

1. transitive provenance repair;
2. cwd-independent resolution verification;
3. cancellable HTTP helper process;
4. invocation validation;
5. ledger durability and diagnostics;
6. reducer plus consumer-by-consumer migration;
7. amendment and discard semantics;
8. confinement reporting; and
9. platform metadata, local quality gates, and documentation.

No release combines an unverified reducer migration with unrelated policy
changes. Phase 1 may ship before Phase 2, and Phase 2 may ship before Phase 3,
but all three phases are part of this approved hardening scope.

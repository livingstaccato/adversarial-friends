# Contract-First Hardening Plan — Adversarial Review

**Date:** 2026-08-30  
**Artifact:** `docs/superpowers/plans/2026-08-30-contract-first-hardening.md`  
**Run:** `/Users/tim/.local/state/adversarial-friends/runs/run-20260830T164837-5700fca0`  
**Mode:** bounded crossexam, two rounds  
**Reviewers:** Claude/security and Agy/ops  
**Host excluded:** Codex  
**Other providers not selected or probed:** OpenCode and Ollama

## Run Quality

Both reviewers completed both rounds. Round 1 produced nine claims; round 2
produced nine reciprocal verdicts. Seven original claims were settled-upheld,
one was amended, and one technically valid repository finding was judged
outside the plan's scope. The run exited 1 because the amended claim was
created in the final allowed round and could not receive another judgment;
there was no reviewer, transport, or authentication failure.

The generated report also repeated each friend once per round and described
only OS confinement. Those are known 0.2.0 dogfood defects already covered by
the plan, not new plan findings.

## Accepted Findings

### 1. High — Resume could restore security grants from attacker-controlled metadata

The draft added `allow_external_tools` to the existing unvalidated resume
channel. That channel already restores `allow_unsandboxed_friend`,
`unsafe_extra_args`, `i_accept_unsandboxed`, and `pass_env` from `run.json`,
including from a caller-supplied directory. A metadata file could therefore
satisfy its own privilege acknowledgments.

**Plan correction:** Security grants are no longer restorable settings. A
resume may restore only schema-validated deterministic configuration. Every
grant must be repeated exactly on the resume command line; saved metadata can
require re-acknowledgment but can never grant authority. Restored invocation
and roster values are type- and trust-validated before use.

### 2. High — Provider updates were atomic writes but not atomic transactions

The draft used one static temporary path and performed unlocked
read-modify-write cycles. `Path.replace` prevents partial target files but
does not prevent concurrent commands from losing one another's changes.

**Plan correction:** A sibling `flock` covers the entire read-modify-write
transaction. Each write uses a unique temporary file, flushes the file and
directory best-effort, and cleans up the temporary path. A deterministic
process-blocking test proves that the later updater re-reads state after it
acquires the lock. This consolidates the overlapping Claude and Agy findings.

### 3. Medium — Unknown capabilities would have been mislabeled denied

Defaulting `Capability.external_tools` to `denied` would cause crashed,
synthetic, and undeclared adapters to report enforcement that never occurred.

**Plan correction:** Missing adapter and capability declarations default to
`unknown`. Fake dispatch is `not-applicable`; dispatch that failed before a
decision is `unknown`; only a successful authority decision may produce
`denied`.

### 4. Medium — HTTP transport was incorrectly trusted by transport type

The draft returned `denied` for every HTTP adapter before consulting its
authority declaration. A future tool-capable HTTP endpoint would therefore
silently bypass the fail-closed policy.

**Plan correction:** Every adapter and transport must declare its authority
shape. Ollama's current `/api/generate` adapter is explicitly `none` because
the request contains no tool field. Undeclared or uncontrolled HTTP adapters
are policy-blocked like executable adapters.

### 5. Medium — Denial-flag ordering test could not test Codex and missed the risky adapters

Codex sends its prompt on stdin, so the draft assertion that a denial flag
preceded an argv `-` token raised `ValueError`. Claude and Agy are the adapters
where trailing/flag-value prompt placement can silently swallow a late flag.

**Plan correction:** Parameterized tests cover every executable adapter and
assert ordering according to `prompt_mode`. Denial argv is emitted before any
prompt placement.

### 6. Medium — Snapshot tests and legacy tree handling were inconsistent

The tests called `verify()` without its required frozen artifact. Legacy
metadata set `tree=None`, and the draft did not explicitly persist the tree
derived from the saved commit.

**Plan correction:** Tests pass the frozen artifact. Commit IDs must be forty
hexadecimal characters before reaching Git. A legacy saved commit is verified,
its tree is derived and persisted, and no new commit is created as repair.

### 7. Medium — The successful-stderr test did not distinguish raw from sanitized output

The benign test string was identical before and after sanitization, so an
implementation could store raw, unbounded stderr and still pass.

**Plan correction:** The test uses hostile Markdown, an autolink, and a long
tail. It requires `diagnostics` to equal the bounded `_stderr_tail`, rejects
active link schemes, and keeps raw text only in the `.err` artifact.

## Valid but Deferred Finding

### High — HTTP response streaming lacks a hard wall-clock deadline

Agy found that `http_transport._run_worker` relies on urllib's socket
inactivity timeout. A server that continuously trickles bytes can keep the
worker alive beyond the run's nominal wall-clock ceiling. Claude confirmed
the code evidence but correctly judged it outside this plan: the approved
scope closes the ten verified 0.2.0 dogfood defects plus provider/host policy,
and does not modify HTTP streaming deadlines.

This should become a separate spec and fix. It must not be silently mixed
into 0.2.1 contract-first hardening because doing so expands a reviewed plan
after approval.

## Disposition

The plan is materially stronger after review. All seven in-scope findings
were incorporated into the design and implementation plan. The single
out-of-scope finding is preserved here with evidence for a follow-up rather
than discarded or quietly implemented.

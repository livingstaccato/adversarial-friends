# Adversarial Review: Afriend Activation, Host Friends, and Harness Authority

**Date:** 2026-09-01

**Artifact:**
`docs/superpowers/specs/2026-09-01-afriend-activation-host-friends-harness-authority-design.md`

**Run:** `run-20260901T102805-1c07ea0e`

**Reviewers:** Claude/security and Codex/spec-vs-reality

**Mode:** report, two required friends, two successful responses

## Outcome

The review raised seven findings: two high and five medium. All seven identify
real ambiguities or unsafe defaults. The design was amended before planning.

## Ranked findings and dispositions

### 1. Staged target symlinks can escape the isolation root — accepted, high

The original design rejected source symlinks but did not explicitly require a
descriptor-rooted, no-follow write for the target. A malicious snapshot could
pre-create `.agents` or a descendant as a symlink. The design now requires the
existing secure I/O descriptor walk, `O_NOFOLLOW` on every component,
non-regular-leaf refusal, root-bounded directory creation, and a committed
symlink regression test.

### 2. Help-marker probes cannot prove Agy tools are denied — accepted, high

The original design could have promoted Agy to `denied` after checking only
that CLI flags existed plus a development smoke test. That would not prove
the installed run-time agent omitted inherited tools. The revised design
keeps Agy `uncontrolled` in this release. Its staged agent is best-effort
hardening and the generic scoped grant stamps it `explicitly-allowed`. A
future denied label requires a per-dispatch semantic handshake or equivalent
invocation-time proof.

### 3. Scoped tool authority does not imply filesystem confinement — accepted,
medium

The design now requires Agy's `--sandbox` in both controlled and fallback
paths, keeps generic OS-confinement decisions independent of tool authority,
and requires reports to show both facts.

### 4. Run-wide policy checks could survive the scoped-policy refactor —
accepted, medium

The revised contract explicitly names readiness capability probes and both
extra-argument guards. Every provider lookup uses `policy.for_provider(name)`;
global unsafe extra arguments require the explicit `*` grant at both the
early and dispatch boundaries.

### 5. A valueless compatibility spelling fails open — accepted, medium

The revised CLI requires `--allow-external-tools=<provider>` or explicit `=*`.
The old valueless form is an error with remediation rather than a global
grant.

### 6. Host self-review could satisfy gate quorum — accepted, medium

Codex remains a default friend, but its friend spec is marked non-independent.
It contributes claims and advisory verdicts only when a judging run already
has two non-host friends. It cannot satisfy the judging minimum, quorum, or
gate clearance.

### 7. The design named the plugin mirror as canonical — accepted, medium

The canonical file is now correctly identified as
`src/adversarial_friends/assets/SKILL.md`; the plugin path is explicitly a
generated mirror.

## Residual risks

- Agy plugin isolation is intentionally best-effort until its CLI exposes or
  the adapter implements a semantic pre-prompt tool-list handshake.
- Provider-scoped grants still authorize the named provider's external tools.
  The benefit is containment of the exception, not removal of that risk.
- A host self-review shares a provider/model family with the orchestrator and
  may be correlated. Marking it non-independent prevents that correlation
  from clearing a gate but does not remove it from report-mode output.

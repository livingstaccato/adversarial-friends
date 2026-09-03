# Multi-skill interface design

**Date:** 2026-09-02

## Purpose

Adversarial Friends presents a focused, selectable skill for each operational
job: starting a review, checking readiness or run health, configuring the
provider roster, and resolving an existing run.  The interface is concise at
selection time, loads only task-relevant guidance, and remains accurate about
the existing `afriend` command-line contract.

The package continues to expose its original `adversarial-friends` skill as a
compatibility router.  Existing command-like requests therefore continue to
work while users can select a narrowly scoped skill directly.

## Public skill surface

The plugin provides these selectable skills under the
`adversarial-friends:` namespace:

| Skill | Job | Activation boundary |
| --- | --- | --- |
| `adversarial-friends` | Route explicit Adversarial Friends requests to the appropriate job. | Requests beginning with `afriend`, requests that name Adversarial Friends, or direct selection. |
| `review` | Start and interpret a review run. | Direct selection, or review-oriented `afriend` routing. Generic review language does not activate it. |
| `status` | Diagnose provider readiness and an optional existing run. | Direct selection or an explicit `afriend status` request. |
| `configure` | Inspect and intentionally change persistent provider defaults. | Direct selection or an explicit `afriend configure` request. |
| `resolve` | Inspect and resolve claims in an existing run. | Direct selection or explicit `afriend resolve` or `afriend resume` routing. |

The router recognizes conversational operation words, not new executable
subcommands.  It maps them to the established command surface:

| Conversational operation | Command invoked |
| --- | --- |
| review | `afriend run` |
| status | `afriend doctor`, plus read-only run inspection when a run is named |
| configure | `afriend providers ...` |
| resolve | `afriend resolve` |

There are no new `afriend status`, `afriend review`, or similar CLI aliases.

## Skill layout and shared guidance

The canonical package data keeps runtime adapter, lens, and harness files at
the asset root.  Selectable skills and their shared operator references are
under `assets/skills/`:

```text
src/adversarial_friends/assets/
  adapters/
  harnesses/
  lenses/
  skills/
    adversarial-friends/SKILL.md
    review/SKILL.md
    status/SKILL.md
    configure/SKILL.md
    resolve/SKILL.md
    _shared/
      review-operations.md
      provider-policy.md
      result-interpretation.md
      troubleshooting.md
```

`_shared` is reference material and has no `SKILL.md`; it is not selectable.
Each selectable skill links only to the references that affect its decision.
This maintains one authoritative explanation of provider policy, mode
semantics, result interpretation, and troubleshooting without loading all of
it for every task.

The plugin mirror has the same skill tree directly below its `skills/`
directory, so every directory that contains `SKILL.md` is discoverable by the
plugin loader:

```text
plugins/adversarial-friends/skills/
  adversarial-friends/
  review/
  status/
  configure/
  resolve/
  _shared/
```

The sync checker and copy target compare/copy this tree.  Wheel package-data
includes all skill and shared-reference files.

## Operational behavior

`review` chooses `report` unless the user names a different mode or clearly
requests its semantics.  It confirms the exact artifact, runs the existing
CLI, reads the run report, and reports failures, quorum refusal, scope
warnings, and recorded downgrades as part of the result.

`status` is read-only.  It reports the effective provider state with
`afriend doctor`; when a run identifier or directory is supplied, it also
interprets that run's recorded outcome and directs the user to the next
action.  It never dispatches friends or changes defaults.

`configure` first presents the effective roster and clearly distinguishes
persistent provider defaults, per-run enable/disable overrides, and
external-tool authority.  It changes persistent defaults only for an explicit
user request.

`resolve` reads the named run, identifies unresolved claims and existing
evidence requirements, and records a disposition only when the user supplies
one.  It never invents a resolution or represents an attestation as proof the
underlying defect is fixed.

The host remains the orchestrator.  In Codex, its self-review is advisory;
independent-friend and judging-mode requirements remain unchanged.  Providers
remain deny-by-default and external tools remain denied unless explicitly
authorized.

## Documentation and diagrams

README, operator documentation, plugin metadata/default prompts, and every
architecture diagram describe the multi-skill interface as the current
product.  They present the selectable skills, their responsibilities, their
handoffs, and the distinction between conversational routing and the stable
CLI.  No user-facing document frames this interface as a migration or retains
the former single-skill architecture as current behavior.

## Validation

The implementation proves:

1. Each skill has valid, discriminating metadata and a narrow activation
   boundary.
2. The router preserves established `afriend` and direct-selector behavior,
   and hands explicit operations to the correct focused skill.
3. Generic review, challenge, second-opinion, and architecture language does
   not invoke Adversarial Friends without an explicit trigger.
4. Every focused skill references available shared guidance and maps to an
   existing CLI command without inventing aliases.
5. The canonical skill tree and plugin mirror are identical, including
   deletions.
6. Built wheels contain all selectable skills and shared references.
7. Public documentation and rendered diagrams describe only the current
   multi-skill interface and remain internally consistent with the code.

## Non-goals

- Adding CLI subcommands that duplicate `run`, `doctor`, `providers`, or
  `resolve`.
- Relaxing narrow automatic activation for ordinary review requests.
- Altering provider authority, sandboxing, host authority, or run-mode
  semantics.

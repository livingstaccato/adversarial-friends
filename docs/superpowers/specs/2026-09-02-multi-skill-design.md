# Multi-skill interface design

**Date:** 2026-09-02

## Purpose

Adversarial Friends presents a focused, selectable skill for each operational
job: starting a review, checking readiness or run health, configuring the
provider roster, and resolving an existing run.  The interface is concise at
selection time, loads only task-relevant guidance, and remains accurate about
the existing `afriend` command-line contract.

`/afriend` is the sole router. It serves command-like requests and directs
explicit product-name requests to the same focused operation surface.

## Public skill surface

The plugin provides these selectable skills under the
`adversarial-friends:` namespace:

| Skill | Job | Activation boundary |
| --- | --- | --- |
| `afriend` | Router and `/afriend` slash entry point. | Direct selection, command-like `afriend` requests, an explicit “a friend” request, or a request that names Adversarial Friends. |
| `review` | Start and interpret a review run. | Direct selection, or review-oriented `afriend` routing. Generic review language does not activate it. |
| `status` | Diagnose provider readiness and an optional existing run. | Direct selection or an explicit `afriend status` request. |
| `configure` | Inspect and intentionally change persistent provider defaults. | Direct selection or an explicit `afriend configure` request. |
| `resolve` | Inspect and resolve claims in an existing run. | Direct selection or explicit `afriend resolve` or `afriend resume` routing. |

The router recognizes conversational operation words, not new executable
subcommands. It maps them to the established command surface:

| Conversational operation | Command invoked |
| --- | --- |
| review | `afriend run` |
| status | `afriend doctor`, plus read-only run inspection when a run is named |
| configure | `afriend providers ...` |
| resolve | `afriend resolve` |

There are no new `afriend status`, `afriend review`, or similar CLI aliases.

## Skill layout and packaged payload

The canonical package data keeps runtime adapter, harness, and lens files at
the asset root. Every selectable skill lives below `entrypoints/`; the router
keeps its detailed operator references in its own folder, and focused skills
are self-contained so a plugin loader can load one without depending on
sibling-directory traversal:

```text
src/adversarial_friends/assets/
  adapters/
  harnesses/
  lenses/
  entrypoints/
    afriend/SKILL.md
    afriend/references/
    review/SKILL.md
    status/SKILL.md
    configure/SKILL.md
    resolve/SKILL.md
```

The router links to its co-located detailed references when an operation
needs them. Focused skills state the safety rules required for their own
operation rather than relying on a sibling reference directory.

The plugin mirrors the runtime payload into the router directory, so
`/afriend` includes the controlled Antigravity harness, adapters, lenses, and
operator references. The focused entrypoints are copied directly below
`skills/`, where the plugin loader discovers slash commands:

```text
plugins/adversarial-friends/skills/
  afriend/
    SKILL.md
    adapters/
    harnesses/
    lenses/
    references/
  review/
  status/
  configure/
  resolve/
```

The sync checker verifies this composite projection: root runtime assets map
to `skills/afriend/`; router references and all `assets/entrypoints/` skills
map to their direct skill folders. Wheel package-data includes every runtime
asset and every entrypoint skill.

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
2. `/afriend` handles command-like and explicit product-name requests and
   hands explicit operations to the correct focused skill.
3. Generic review, challenge, second-opinion, and architecture language does
   not invoke Adversarial Friends without an explicit trigger.
4. Every focused skill is self-contained and maps to an existing CLI command
   without inventing aliases.
5. The plugin mirror preserves every runtime asset and its direct entrypoint
   projection, including deletions.
6. Built wheels contain all runtime assets and selectable skills.
7. Public documentation and rendered diagrams describe only the current
   multi-skill interface and remain internally consistent with the code.

## Non-goals

- Adding CLI subcommands that duplicate `run`, `doctor`, `providers`, or
  `resolve`.
- Relaxing narrow automatic activation for ordinary review requests.
- Altering provider authority, sandboxing, host authority, or run-mode
  semantics.

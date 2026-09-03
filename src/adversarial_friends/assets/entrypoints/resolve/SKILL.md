---
name: resolve
description: Use only through direct qualified selection ($adversarial-friends:resolve) or explicit /afriend routing to inspect unresolved claims or record a disposition for a named run claim. Require user-supplied disposition and evidence before writing.
---

# Adversarial Friends resolution

`afriend resume <run-id>` is not a resolution request. Route it to the router
and run `afriend run --resume <run-id>`; it does not require a disposition or
evidence.

Discover unresolved claims from a named run before recording anything:

```bash
afriend resolve <run-id> --list
afriend resolve <run-id> --next
```

`--list` is read-only and shows stable IDs, severity, summary, location, and
recorded evidence. `--next` is read-only and selects a claim only when the
highest-priority choice is unique; otherwise it prints the choices and records
nothing.

Resolve only a named run and claim, with a user-supplied disposition and
concrete evidence location:

```bash
afriend resolve <run-id> --claim c-0001@1 \
  --disposition fixed|rejected|accepted-risk --evidence <location>
```

Before resolving a supplied run ID, read its default directory
`${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/<run-id>`: inspect
`report.md`, `run.json`, and `claims.jsonl`. If the run used `--out`, ask for
its run directory instead. Ask for any missing run, claim identifier (for
example `c-0001@1`), disposition, or evidence; never invent any of them. A recorded resolution is
an attestation, not proof that a defect is fixed, so report the recorded
location-changed, location-unchanged, or unverifiable outcome honestly.

This skill does not dispatch a new review or change provider defaults. Codex
remains advisory, provider selection follows effective configured defaults, and external-tool authority
must be explicit for any separate run.

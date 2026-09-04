---
name: status
description: Use only through direct qualified selection ($afriend:status) or explicit /afriend routing to inspect provider readiness or a named existing run. This is read-only.
---

# afriend status

Check provider readiness with the stable command:

```bash
afriend doctor
```

Explain effective provider states such as `ready`, `reachable-unconfigured`,
`unavailable`, `disabled`, and `policy-blocked`. Disabled providers are not
probed. `afriend doctor` is readiness only, not run inspection; doctor is
readiness only. When the user supplies a named run ID or path, inspect it with:

```bash
afriend status <run-id-or-path>
afriend status <run-id-or-path> --watch
afriend status <run-id-or-path> --json
```

The default run root is
`${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/<run-id>`; use
`--out` for a run written elsewhere. Status reports lifecycle state, mode,
scope, profile, per-friend completion/failure, current round, claims,
downgrades, and a next action. `--watch` renders only new lifecycle events
until the terminal event and tolerates a partially written final event. It
uses `run.json` and `claims.jsonl` for the available legacy summary when a
run has no events; it never invents a run identity or reads a report as
lifecycle evidence.

This skill is read-only: it never dispatches friends and never changes
configuration. Codex remains an advisory host orchestrator. A past run's
authority record is descriptive only: external tools are denied for a new
`afriend run --resume` invocation unless the same normalized grant is supplied
again on that command line.

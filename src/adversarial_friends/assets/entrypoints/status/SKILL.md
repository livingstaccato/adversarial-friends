---
name: status
description: Use only through direct qualified selection ($adversarial-friends:status) or explicit /afriend routing to inspect provider readiness or a named existing run. This is read-only.
---

# Adversarial Friends status

Check readiness with the stable command:

```bash
afriend doctor
```

Explain effective provider states such as `ready`, `reachable-unconfigured`,
`unavailable`, `disabled`, and `policy-blocked`. Disabled providers are not
probed. `afriend doctor` is readiness only, not run inspection; doctor is readiness only. When the user
supplies a run ID, inspect `${XDG_STATE_HOME:-~/.local/state}/adversarial-friends/runs/<run-id>`.
If it was created with `--out`, ask for its run directory instead. Read
`report.md`, `run.json`, and `claims.jsonl`, plus per-friend metadata/error as
needed, then recommend the next action for the named run; never invent a run identity.

This skill is read-only: it never dispatches friends and never changes
configuration. Codex remains an advisory host orchestrator. A past run's
authority record is descriptive only: external tools are denied for a new
`afriend run --resume` invocation unless the same normalized grant is supplied
again on that command line.

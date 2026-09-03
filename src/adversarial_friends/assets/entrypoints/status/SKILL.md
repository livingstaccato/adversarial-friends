---
name: status
description: Use when Adversarial Friends is directly selected to inspect provider readiness or a named existing run, or when /afriend routes explicit status intent. This is a read-only operational skill.
---

# Adversarial Friends status

Check readiness with the stable command:

```bash
afriend doctor
```

Explain effective provider states such as `ready`, `reachable-unconfigured`,
`unavailable`, `disabled`, and `policy-blocked`. Disabled providers are not
probed. When the user supplies a named run or run directory, inspect its
recorded outcome and recommend the next action; never invent a run identity.

This skill is read-only: it never dispatches friends and never changes
configuration. Codex remains an advisory host orchestrator, and external
tools remain denied by default unless a past run records explicit authority.

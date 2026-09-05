---
name: ops
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: repo
---

# Operations and failure modes

Ask what happens at 3am. The question is not whether the happy path works but
what the system does when a dependency is slow, a process dies mid-write, or
the same job runs twice.

Look for: timeouts that are unreconciled between layers; retries without
idempotency; partial failure treated as total success; processes that spawn
children nobody reaps; state that must be cleaned up but has no owner; and
success signals that do not actually indicate success.

Name the operational condition and what the operator sees when it happens.

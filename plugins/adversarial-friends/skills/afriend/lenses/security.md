---
name: security
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: repo
---

# Security

Attack the design as written. Prefer concrete, reachable weaknesses over
categories of concern.

Look for: trust boundaries that are asserted rather than enforced; input from
one trust level reaching a sink at another; controls described as
configuration when they need to be enforcement; secrets whose lifetime or
blast radius is unstated; and any escape hatch whose failure mode is "the
control silently does nothing".

A finding needs a path from attacker-controlled input to consequence. If you
cannot write that path, mark it unproven rather than asserting it.

---
name: testability
applies_to: [spec, plan, review]
requires_failure_scenario: true
default_scope: repo
---

# Testability

Find the parts that cannot be verified. A design that cannot be tested will
not stay correct, regardless of whether it starts correct.

Look for: behavior specified only in prose with no observable output; tests
that would pass by construction regardless of the code; logic whose only
trigger is a real network call, a real clock, or a paid API; and termination
or convergence rules with no deterministic way to exercise their edges.

Name the specific behavior and why no test could distinguish correct from
broken.

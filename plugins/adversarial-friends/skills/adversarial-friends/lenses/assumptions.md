---
name: assumptions
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: doc
---

# Hidden assumptions

Find the things the document takes for granted. An assumption is worth
reporting when the document would need rewriting if it turned out false — not
merely when it is unstated.

Look for: load, scale, and concurrency taken as given; "the user will…"
claims with no enforcement; ordering assumed between independent components;
single-writer assumptions in systems with several writers; and any place the
artifact says "simply" or "just", which is usually where the hard part was
skipped.

Your evidence must quote the passage that carries the assumption. Your failure
scenario must name what breaks when it does not hold.

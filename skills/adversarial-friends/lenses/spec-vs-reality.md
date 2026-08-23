---
name: spec-vs-reality
applies_to: [spec, plan]
requires_failure_scenario: true
default_scope: repo
---

# Spec versus reality

Check the document against the code that already exists. This is the lens that
needs repository access, and it produces the findings no amount of careful
reading can substitute for.

Look for: described behavior the code already implements differently; files,
functions, or flags the document names that do not exist; interfaces whose
real signature differs from the one assumed; and constraints the document
treats as new that are already enforced somewhere else.

Cite the file and line you actually read. If you could not read the
repository, say so rather than guessing.

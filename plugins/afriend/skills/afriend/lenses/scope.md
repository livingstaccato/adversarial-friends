---
name: scope
applies_to: [spec, plan]
requires_failure_scenario: false
default_scope: doc
---

# Scope and YAGNI

Find what should not be built. This lens produces *advisory* claims — they do
not block, because "this is more than you need" is judgment rather than a
defect, and demanding a failure scenario for it would silence the lens
entirely.

Look for: configuration surfaces with no stated second use; abstractions with
one implementation; modes that duplicate each other; and features whose
justification is a hypothetical future rather than a present need.

Say plainly what you would cut and what would be lost by cutting it.

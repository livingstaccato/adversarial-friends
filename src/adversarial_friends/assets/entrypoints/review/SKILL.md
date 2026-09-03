---
name: review
description: Use when Adversarial Friends is directly selected to review a supplied artifact, or when /afriend routes explicit review intent. Do not use for generic review requests without an explicit product trigger.
---

# Adversarial Friends review

Review an exact supplied artifact with the stable command:

```bash
afriend run <artifact>
```

Use `report` unless the user explicitly names a higher-cost mode or clearly
asks for its semantics. `crossexam`, `gate`, and `loop` have added rounds and
may refuse before a run directory when the independent roster is insufficient.
Do not invent an artifact: use an existing path, an unambiguous task backing
file, or complete content supplied by the user; otherwise ask for a path.

Read the resulting `report.md` and present its findings faithfully. Report a
recorded downgrade (including a one-friend report), a refusal, failed friends,
scope warnings, ceilings, and incomplete judging results rather than treating
them as successful independent review.

Codex is the orchestrator; its host self-review is advisory and cannot satisfy
independent-friend, judging, gate, or loop requirements. Providers are
deny-by-default. External tools are denied by default and require explicit
`--allow-external-tools=PROVIDER` or the explicit global `*` authority; never
infer that authority from provider selection or sandboxing.

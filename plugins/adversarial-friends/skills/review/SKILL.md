---
name: review
description: Use only through direct qualified selection ($adversarial-friends:review) or explicit /afriend routing for a supplied artifact. Do not use for generic review requests.
---

# afriend review

Review an exact supplied artifact with the stable command:

```bash
afriend run <artifact>
```

Use the effective `quick` profile unless the user explicitly selects a
task-only profile, a higher-cost mode, or clearly asks for its semantics.
`crossexam`, `gate`, and `loop` have added rounds and may refuse before a run
directory when the independent roster is insufficient. `afriend run <artifact>
--profile NAME` is a per-run selection; an explicit `--mode` wins over the
profile's mode.
Do not invent an artifact: use an existing path, an unambiguous task backing
file, or complete content supplied by the user; otherwise ask for a path.

Read the resulting `report.md` and present its findings faithfully. Report a
recorded downgrade (including a one-friend report), a refusal, failed friends,
scope warnings, ceilings, and incomplete judging results rather than treating
them as successful independent review.

Codex is the orchestrator; its host self-review is advisory and cannot satisfy
independent-friend, judging, gate, or loop requirements. Provider selection
follows effective configured defaults. External tools are denied by default and require explicit
`--allow-external-tools=PROVIDER` or the explicit global `*` authority; never
infer that authority from provider selection or sandboxing.

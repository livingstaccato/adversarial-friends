---
name: resolve
description: Use only through direct qualified selection ($adversarial-friends:resolve) or explicit /afriend routing to record a disposition for a named run claim. Require user-supplied run, disposition, and evidence.
---

# Adversarial Friends resolution

`afriend resume <run-id>` is not a resolution request. Route it to the router
and run `afriend run --resume <run-id>`; it does not require a disposition or
evidence.

Resolve only a named run and claim, with a user-supplied disposition and
concrete evidence location:

```bash
afriend resolve <run-id> --claim <claim-id> \
  --disposition fixed|rejected|accepted-risk --evidence <location>
```

Inspect the named run first when supplied. Ask for any missing run, claim,
disposition, or evidence; never invent any of them. A recorded resolution is
an attestation, not proof that a defect is fixed, so report the recorded
location-changed, location-unchanged, or unverifiable outcome honestly.

This skill does not dispatch a new review or change provider defaults. Codex
remains advisory, provider selection follows effective configured defaults, and external-tool authority
must be explicit for any separate run.

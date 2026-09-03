---
name: configure
description: Use only through direct qualified selection ($adversarial-friends:configure) or explicit /afriend routing to inspect or explicitly change provider defaults. Do not change settings without an exact requested change.
---

# Adversarial Friends configuration

Inspect persistent defaults with:

```bash
afriend providers list
```

`afriend providers list` reports persistent defaults; `afriend doctor` reports
effective readiness. Persistent provider defaults are user-owned configuration, changed only for an
exact user-requested change with `afriend providers enable`, `disable`,
`set-model`, or `clear-model`. Do not turn an observation or recommendation
into a persistent change.

Distinguish persistent defaults from per-run `--enable-provider` and
`--disable-provider` overrides. External-tool authority is a third, separate
layer: provider selection follows effective configured defaults and external tools remain denied by
default unless the user explicitly supplies `--allow-external-tools=PROVIDER`
or global `--allow-external-tools=*`. That authority neither changes defaults
nor follows from provider enablement. Codex's advisory host role does not
alter these boundaries.

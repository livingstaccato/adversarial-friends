---
name: configure
description: Use when Adversarial Friends is directly selected to inspect or explicitly change provider defaults, or when /afriend routes explicit configuration intent. Do not change settings without an exact requested change.
---

# Adversarial Friends configuration

Begin by showing the effective roster:

```bash
afriend providers list
```

Persistent provider defaults are user-owned configuration, changed only for an
exact user-requested change with `afriend providers enable`, `disable`,
`set-model`, or `clear-model`. Do not turn an observation or recommendation
into a persistent change.

Distinguish persistent defaults from per-run `--enable-provider` and
`--disable-provider` overrides. External-tool authority is a third, separate
layer: providers are deny-by-default and external tools remain denied by
default unless the user explicitly supplies `--allow-external-tools=PROVIDER`
or global `--allow-external-tools=*`. That authority neither changes defaults
nor follows from provider enablement. Codex's advisory host role does not
alter these boundaries.

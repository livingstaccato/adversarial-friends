---
name: configure
description: Use only through direct qualified selection ($adversarial-friends:configure) or explicit /afriend routing to inspect or explicitly change guided setup, review profiles, or provider defaults. Do not change settings without an exact requested change.
---

# afriend configure

Inspect persistent provider defaults with:

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

For first-session setup, preview exact local changes without writing:

```bash
afriend init --guided
afriend init --guided --default-profile balanced --enable-provider claude
afriend init --guided --apply --default-profile balanced --enable-provider claude
```

The preview reports built-in profiles, discovered provider readiness, the host
role, and the continuing external-tool denial. `--apply` writes only the
listed provider defaults, optional Ollama model, selected default profile, and
generated roster; it never dispatches friends or enables external tools.
Plain `afriend init` remains the direct roster-generation command.

Profiles are a separate persistent layer:

```bash
afriend profiles list
afriend profiles show quick
afriend profiles create focused --base quick --timeout 300
afriend profiles set-default focused
```

Custom profiles inherit a built-in or custom base and can hold only review-safe
mode, preset, lenses, `max_friends`, `require_friends`, timeout, and
round/iteration ceilings. They cannot encode a provider, `--friend`, model,
credential, environment forwarding, external-tool authority, unsafe arguments,
or sandbox exception. Make a persistent change only for the exact
user-requested selection; use `--profile NAME` for a per-run choice.

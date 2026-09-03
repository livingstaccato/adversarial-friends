# DNS and Failure-Visibility Hardening Design

## Goal

Make a run that cannot obtain any friend response unambiguously incomplete,
verify the Linux resolver bind against the layouts it claims to support, and
make the unsafe fallback's consequence clear without silently weakening the
security boundary.

## Decisions

### Resolver confinement

Linux confinement continues to expose only the resolved regular-file target of
`/etc/resolv.conf` when that file is a symlink. The target is not assumed to
live under systemd-resolved: a systemd-resolved, resolvconf, or NetworkManager
layout must produce the same destination bind. A Linux-only integration test
uses real `bwrap` to prove the generated namespace can read the resolver
through its original `/etc/resolv.conf` path. Unit tests cover target discovery
for each layout; the integration test is required in Linux CI and skipped only
where `bwrap` is unavailable locally.

The helper remains fail-closed with respect to authority: a missing,
unreadable, broken, directory, or outside-root target receives no additional
bind. It must not broaden the `/run` tree merely to make DNS work.

### Incomplete-review signal

The run result derives one stable review-completeness summary from persisted
friend outcomes. When zero independent dispatched friends answer, the result
is `incomplete`, includes `0/N friends answered`, and includes the compact
classified reasons already stored for each failed or skipped friend. It is
written into the report and named-run status payload; it never interprets zero
claims as a clean review.

The CLI prints this summary to stderr by default at terminal completion.
Configuration exposes a review-safe output setting with two values:

- `terminal` (default): print the incomplete summary to stderr.
- `report-only`: persist the same summary but suppress the terminal line for
  automation that intentionally owns presentation.

The exit-code contract remains unchanged: a run in which every dispatched
friend fails exits `1`. A partial run remains visibly partial but retains its
existing mode-specific exit behavior.

### Pre-dispatch reachability

Preflight remains capability-based, not a universal subprocess probe. HTTP
adapters use their declared bounded endpoint probe and fail before a run
directory is created when unreachable. The adapter contract gains an optional,
bounded readiness probe only when a provider can prove it is read-only,
non-billable, and has no side effects. CLI adapters without such a declaration
are started normally and their exact terminal failure is classified and
reported; the harness will not invent a command or consume a model call just
to guess health.

### Unsafe fallback contract

`--allow-unsandboxed-friend` remains an explicit override for a provider with
no verified self-confinement when the host lacks an OS sandbox. It does not
apply to a provider that supplies and uses its own read-only mode. Every
affected result says that the friend ran without OS confinement and may read
with the invoking user's authority. Help text, report wording, and the skill
recommend installing `bwrap`/`sandbox-exec` or choosing a self-confining
provider for untrusted artifacts; they do not present the override as a normal
repair.

## Data flow

```text
adapter declaration -> bounded readiness assessment -> dispatch
                                           |                |
                                     unavailable reason   friend outcomes
                                           \                /
                                            review completeness
                                             |        |       |
                                        report.md  status   stderr (policy)
```

The resolver path is separate from provider readiness:

```text
/etc/resolv.conf symlink -> resolve safe regular target -> one read-only bind
                                                     -> real bwrap DNS/read test
```

## Error handling

- Resolver discovery failures add no bind and cannot create filesystem access.
- A preflight probe failure names the provider and endpoint without printing
  credentials or raw response bodies.
- The terminal summary uses the existing bounded, sanitized diagnostic
  summaries; detailed stderr remains in the run directory.
- A quiet-output setting changes only presentation, not metadata, reports, or
  exit status.

## Verification

Tests prove each resolver layout maps to its real target, the Linux namespace
can consume that target, zero-response runs emit and persist the incomplete
summary by default, `report-only` suppresses only stderr, and HTTP readiness
still refuses unavailable providers before dispatch. Documentation and the
shipped skill explain the exact unsafe-fallback authority. The package and its
plugin mirror remain synchronized.

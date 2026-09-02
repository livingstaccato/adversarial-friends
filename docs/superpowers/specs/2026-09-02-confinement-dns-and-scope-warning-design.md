# Confinement DNS and Scope Warning Design

## Goal

Make confined Linux friends able to use the host resolver on systemd-resolved
hosts, and make an automatic repo-to-doc scope downgrade visible before any
provider is dispatched.

## Scope

This change addresses GitHub issues #2 and the immediate, safe portion of #3.
It does not introduce an explicit repository override. An artifact outside a
repository continues to be doc scope by default.

## DNS confinement

`bwrap` already read-only binds `/etc`, but on systemd-resolved hosts
`/etc/resolv.conf` is a symlink whose target lives under `/run`. The sandbox
will bind the resolved target file at its own absolute destination when all of
the following are true:

- `/etc/resolv.conf` is a symlink under the supplied host root;
- resolving it stays within that host root; and
- the resolved target is a readable regular file.

The target bind is additive and read-only. The sandbox will not bind `/run`,
will not alter network namespace policy, and will silently omit an absent,
broken, unreadable, or non-regular target. A normal non-symlink resolver file
therefore keeps the existing bwrap argv exactly.

The resolver helper remains root-injectable so macOS tests can construct a
synthetic `/etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf` layout.

## Scope warning

When snapshot reconciliation changes every selected friend to doc scope
because the artifact's invocation path is outside a Git repository, the run
prints one prominent preflight warning to stderr before readiness/dispatch.
The warning states the artifact, explains that friends will receive only the
artifact rather than a repository snapshot, and tells the operator to place
the artifact in the target repository when code inspection is intended.

The warning is semantic rather than progress output: it is emitted even with
`--no-progress`, exactly once per run. Its content is also persisted in the
existing downgrade metadata/report path. Reconciliation remains idempotent so
loop iterations do not repeat it.

## Documentation

The canonical skill/troubleshooting material and README will describe how
artifact location chooses automatic scope:

- repository-contained artifacts get a repository snapshot (including normal
  untracked, non-ignored artifacts);
- outside-repository artifacts are explicitly doc scope and trigger the
  preflight warning;
- ignored artifacts are intentionally excluded from snapshots and fail with
  an actionable snapshot error rather than silently using `HEAD`.

The plugin mirror is synchronized from canonical assets.

## Error handling and compatibility

Existing snapshot behavior and run metadata schema remain unchanged. The DNS
helper is best-effort only for an optional resolver target; it must never make
a formerly runnable sandbox fail. No automatic discovery of an unrelated
repository is added, preserving the rule that invocation path establishes the
review context.

## Tests

Unit tests cover the symlink resolver bind, regular and broken resolver
layouts, preflight output with `--no-progress`, one-warning behavior across
loop reconciliation, and documentation contracts. The complete portable
quality gate remains the final acceptance check; Linux CI exercises bwrap.

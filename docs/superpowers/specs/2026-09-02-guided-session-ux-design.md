# Guided Session UX Design

## Goal

Make Adversarial Friends understandable at the point of use: establish a
review session before spending provider capacity, report progress as friends
finish, provide a read-only status command, and make defaults, profiles, and
claim resolution easy to use without weakening existing authority boundaries.

## Scope and delivery order

The work ships in the user-requested order.

1. **Guided session UX:** first-session preflight, completion feedback, and
   guided setup with a persistent default profile plus a per-task override.
2. **Run status:** a real read-only `afriend status` command.
3. **Profiles:** named, user-owned review profiles and profile management.
4. **Resolution UX:** discovery and selection help for unresolved claims.

Each slice is independently usable and keeps the existing `run`, `doctor`,
`providers`, and `resolve` commands valid.

## Session contract

`/afriend` remains the only direct router. On the first review request in a
host task, and before every requested new loop iteration, it pauses before
dispatch and presents one compact preflight:

> About to start Adversarial Friends to review `<artifact>` in `<mode>` mode
> with `<profile>`. Scope: `<repository snapshot|document only>`. Friends:
> `<name, provider, lens, role>`; external tools: `<denied|explicit grant>`.

The user can accept the resolved default, choose a task-only profile or mode,
change the enabled roster for that task, or stop. The host does not repeat the
pause for later commands in the same review session unless a new loop
iteration is requested. A direct command-line invocation remains
non-interactive: it uses the effective profile and explicit flags.

The preflight is descriptive, never an authority grant. Provider enablement,
external-tool grants, unsafe extra arguments, and sandbox exceptions retain
their current explicit controls.

## Defaults and profiles

The first slice provides immutable built-in profiles:

| Profile | Mode | Intended use |
| --- | --- | --- |
| `quick` | `report` | One parallel critique fan-out. |
| `balanced` | `crossexam` | Independent critique and bounded judging. |
| `thorough` | `loop` | Repeat until the normal convergence rule. |

The user-owned `~/.config/adversarial-friends/session.json` records only
`default_profile`; its default is `quick`, preserving the current one-round
behavior on a fresh machine. `afriend run --profile NAME` is a per-run
override. An explicit `--mode` wins over the profile's mode, and explicit
operational flags continue to win over profile values. No profile stores
provider defaults, models, rosters, credentials, or external-tool authority.

The later profile slice adds named user profiles that inherit a built-in
profile and may set review-safe run options: mode, preset, lenses,
`max_friends`, `require_friends`, timeout, and round/iteration ceilings.
They cannot encode `--friend`, provider enablement, process/sandbox escapes,
environment forwarding, or external-tool grants. `afriend profiles` exposes
`list`, `show`, `create`, `update`, `delete`, and `set-default`; changes are
atomic and confined to the user configuration directory.

## Guided setup

`afriend init --guided` becomes the setup entry point without becoming an
opaque terminal wizard. Its inspectable preview reports discovered providers,
readiness, the Codex advisory-host role when applicable, the effective
external-tool denial, and selectable built-in profiles. A host presents those
choices conversationally. The CLI receives exact selections and persists only
the requested provider defaults, optional Ollama model, default profile, and
the existing generated roster. `afriend init --guided --apply` performs those
exact selections atomically; the no-`--apply` form is a no-write preview. The
existing `afriend init` roster-generation behavior remains unchanged. A guided
apply never overwrites an existing roster without `--force`; it updates only
the documented fields in the user-owned provider and session configuration.

The setup result names every created or changed file and prints the exact
first review command. It does not dispatch friends.

## Lifecycle events and host feedback

Every run writes append-only `events.jsonl` in its run directory. Records have
a schema version, timestamp, run ID, event type, and a bounded public payload.
Initial event types are `run_started`, `friend_finished`, `friend_failed`,
`round_finished`, and `run_finished`. They contain state already safe to show
in existing progress output: friend identity, provider/lens, round, duration,
status, and the final run outcome. They never copy prompts, raw model output,
environment values, credentials, or external-tool arguments.

The existing human stderr progress remains compatible. It is supplemented by
one concise completion line per friend and a final line naming the run,
participation/downgrade state, and the next action. The `/afriend` skill reads
the final event and `report.md`, then tells the user what finished and whether
to inspect, resolve, resume, or start another iteration.

## Run status

`afriend status <run-id-or-path>` reads a run; it never dispatches friends,
changes configuration, or grants authority. It reports run identity, mode,
scope, profile, current/final state, per-friend completion/failure, current
round, claims by state, downgrades, and the recommended next action. It exits
cleanly for terminal runs and gives a usage error for an unknown or malformed
run path.

`--json` returns a versioned machine-readable view. `--watch` tails
`events.jsonl` until a terminal event, rendering only new events and using a
bounded polling interval. It must handle a partially written final JSONL line
as “not yet complete,” never as corruption. `doctor` remains provider
readiness only; the focused `status` skill routes run inspection to the new
command and readiness requests to `doctor`.

## Resolution UX

`afriend resolve <run-id> --list` lists unresolved claims with stable claim
IDs, summary, severity, and current evidence locations. `--next` selects the
highest-priority unresolved claim only when there is exactly one unambiguous
choice; otherwise it prints choices and exits without recording anything.
The existing write form remains authoritative: a disposition and evidence are
still required before a resolution is appended. The host can use `--list` to
ask the user for a selection and evidence, then invoke the existing write
command exactly once.

## Compatibility, safety, and testing

Existing CLI commands and output contracts remain valid: `afriend run` keeps
stdout reserved for its run directory (or `--json`), and `doctor` retains its
current readiness semantics. New human progress and watch output go to stderr
or are explicitly selected. Existing runs lacking `events.jsonl` remain
inspectable from `run.json`, `claims.jsonl`, and round metadata; watch reports
that live events are unavailable rather than inferring them.

Tests cover profile precedence and forbidden persisted fields, setup no-write
preview and explicit-write behavior, event ordering/secret exclusion, status
of live/terminal/legacy runs and torn event tails, watch termination,
resolution listing/selection, skill activation and wording, plugin projection,
and current docs/diagrams. The release-quality gate remains the final check.

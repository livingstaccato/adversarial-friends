# Afriend Activation, Host Friends, and Harness Authority Design

**Date:** 2026-09-01

**Status:** Approved for implementation

## Context

Adversarial Friends 0.2.1 has three mismatches with the intended product:

1. The Codex skill is broadly selected for ordinary plan-review language,
   while the desired conversational command is the narrow phrase `afriend …`
   or `afriend to …`.
2. A Codex-hosted run excludes Codex from automatic discovery. The current
   Codex task orchestrates the review but does not also contribute a review
   unless `--include-self` or an explicit `--friend codex:...` is supplied.
3. Antigravity (`agy`) is policy-blocked under the default external-tool deny
   policy. Its CLI can select a constrained custom agent, but the runner has
   no generic way to stage a provider-controlled harness into an isolated
   friend workspace. The existing escape hatch, `--allow-external-tools`,
   also grants every provider in the run rather than naming only the provider
   that needs the exception.

The design keeps deny-by-default, but it does not require Antigravity to prove
that every possible plugin implementation is absent before it can be useful.
Its controlled agent is the preferred path. A scoped, conspicuous authority
grant is the generic fallback for Antigravity and future harnesses.

## Goals

- Let a user say `afriend to X` without saying “Adversarial Friends” or using
  a `$skill` token.
- Do not select the skill for an ordinary request such as “review this plan.”
- Make a Codex-hosted run use Codex as both orchestrator and automatic friend
  by default.
- Preserve a clear opt-out and report host self-review honestly.
- Make provider-side harness staging an adapter capability, not an Agy branch.
- Make external-tool exceptions provider-scoped and invocation-local.
- Get the installed Antigravity CLI into the ready roster through the generic
  harness path when its constrained agent can be verified.
- Keep resume, audit, readiness, and failure behavior deterministic.

## Non-goals

- The host friend is not claimed to be provider-independent. It is a separate
  subprocess and context, but may use the same provider and model family as
  the orchestrator.
- The skill does not auto-run for generic review wording.
- Authority grants do not become persistent provider preferences.
- The runtime does not install or edit global Antigravity configuration.
- The first implementation does not define a general plugin package manager
  for third-party harness assets. It defines the safe staging contract those
  adapters can use.

## 1. Narrow conversational activation

The canonical skill remains physically located at
`skills/adversarial-friends`, so existing plugin identifiers remain stable.
Its frontmatter description becomes command-like:

> Use only when the user starts a request with `afriend`, says `afriend to …`,
> explicitly names Adversarial Friends, or directly selects this skill.

The description must also say not to trigger for generic “review,” “challenge
this plan,” or “poke holes in this” requests unless `afriend` or the full tool
name appears. This retains Codex's description-based implicit selection only
for an intentionally narrow natural-language command. No undocumented alias
mechanism is assumed.

The skill interprets these equivalent forms:

```text
afriend this plan
afriend to this plan
afriend docs/design.md
afriend to docs/design.md with crossexam
```

If the target resolves to an existing local artifact, the skill passes that
path to `afriend run`. If “this plan/spec/review” has one unambiguous backing
file in the current task, it uses that file. If no file can be identified,
the skill materializes the artifact only when doing so is already within the
user's request; otherwise it asks for the artifact path.

The default conversational mode is `report`, matching the CLI. `crossexam`,
`gate`, and `loop` require the user to name the mode or clearly request its
semantics. The documented defaults remain one critique fan-out for `report`,
three total rounds per judging block, and five maximum loop iterations.

## 2. Codex is orchestrator and friend by default

Host detection remains generic, but automatic self-inclusion changes only for
the Codex host in this release:

- detected host `codex`: include the `codex` provider by default;
- detected non-Codex host: retain the existing exclude-self default;
- no detected host: no special treatment.

This narrowly satisfies the product expectation “if I am in Codex, I am the
Codex friend by default” without silently changing Claude, Agy, or OpenCode
host behavior.

The CLI adds a mutually exclusive `--exclude-self` flag. Effective selection
is tri-state:

1. `--include-self`: include any detected host;
2. `--exclude-self`: exclude any detected host;
3. neither: include Codex, exclude every other detected host.

The saved invocation records the effective boolean decision and the detected
host provider. Resume restores the frozen roster and validates the same
explicit security grants as before; it does not rediscover the host.

Every friend row produced by the detected host carries `host_self_review=true`.
The Markdown report labels it “host self-review.” It counts as a friend for
roster size and judging because it is a separate subprocess and ledger actor,
but documentation explicitly says it is not a separate provider opinion.

## 3. Generic run-local harness staging

An adapter may declare zero or more controlled workspace assets. Each asset
has:

```toml
[[workspace_assets]]
source = "harnesses/agy/afriend-reviewer.md"
target = ".agents/agents/afriend-reviewer/agent.md"
sha256 = "<canonical asset digest>"
```

This is a generic adapter contract. The runner does not know what an “agent,”
“plugin,” or provider-specific harness file means. It only:

1. resolves `source` beneath the package's canonical assets directory;
2. rejects absolute paths, `..`, symlinks, duplicate targets, and digest
   mismatches;
3. resolves `target` beneath the friend-owned isolated workspace;
4. writes the asset before the provider process starts;
5. never writes the asset into the caller's checkout or global home;
6. records source digest, target, and staging status in the friend sidecar;
7. removes it with the rest of the friend isolation directory.

Adapters without `workspace_assets` behave byte-for-byte as before. HTTP
adapters may not declare workspace assets because they have no provider
process workspace.

Staging happens after the friend isolation directory exists and before argv
construction/dispatch. A staging error refuses that friend before the
provider process is contacted. The generic mechanism can support future
harness CLIs by adding adapter TOML and package assets rather than Python
conditionals.

### Antigravity controlled agent

The Agy adapter stages `.agents/agents/afriend-reviewer/agent.md` and selects
it with:

```text
--agent afriend-reviewer
--disable-slash-commands
--mode plan
--sandbox
```

The agent definition:

- permits no tools (`tools: []`);
- cannot be invoked as a subagent;
- does not inherit customizations where the installed Agy schema supports
  that field;
- instructs the model to return only the requested structured review.

The adapter's existing capability probe verifies the required CLI flags.
Development verification performs one small live `stream-json` call from a
staged temporary workspace and checks the initialization event's tool list.
If the installed Agy version still exposes inherited plugin capabilities, the
adapter must not falsely report `external_tools=denied`; it remains
`uncontrolled` and uses the scoped fallback below when the operator opts in.

## 4. Provider-scoped authority fallback

`--allow-external-tools` becomes an optional-value, repeatable flag:

```text
--allow-external-tools agy
--allow-external-tools opencode --allow-external-tools agy
--allow-external-tools                 # legacy/global spelling
--allow-external-tools '*'             # explicit global spelling
```

No value preserves the existing global behavior for compatibility. A named
value allows only that provider. Unknown provider names and contradictory
forms are usage errors before any probe or dispatch.

The runtime replaces its single run-wide enum with an immutable authority
policy containing `allowed_providers`. `policy.for_provider(name)` produces
the existing `DENY` or `ALLOW` decision, so adapter enforcement and argv
construction stay provider-local.

The generic rules are:

- denial remains the default for every provider not named;
- `*` means every provider and cannot be combined with names;
- explicit `--friend` does not bypass the policy;
- `--unsafe-extra-args` still targets every friend and therefore requires
  the global `*` grant, not a subset;
- doctor evaluates the default deny policy and does not infer invocation
  grants;
- readiness may report Agy policy-blocked while a normal run that explicitly
  grants `agy` selects it;
- reports and sidecars record the per-provider decision, sources, and exact
  scoped grant;
- a resume must repeat the identical normalized grant set on its command
  line; run metadata records it but never restores it as authority.

Run metadata adds `external_tool_grants: [string]` and reports
`external_tool_policy` as `deny`, `scoped-allow`, or `allow`. Migration treats
the old boolean `allow_external_tools=true` as the global `*` grant only for
audit; it still cannot authorize a resume without an explicit current flag.

## 5. Readiness and audit semantics

For each provider, readiness applies authority before executable or endpoint
probing. A provider that cannot meet its effective deny policy remains
`policy-blocked`. A provider explicitly allowed for this invocation can be
probed and selected, with `external_tools=explicitly-allowed` stamped into its
friend row.

For staged harnesses, readiness also validates the adapter declaration and
packaged asset digest without touching a provider. Run-time staging validates
the destination again inside the actual isolation directory. This separates
static package integrity from per-dispatch filesystem integrity.

Reports must not summarize a mixed run as simply “external tools allowed.”
They name the granted providers and preserve denial status for the rest.

## 6. Documentation and compatibility

README, the canonical skill, mode/reference documentation, CLI help, examples,
and eval prompts must agree on:

- the `afriend …` conversational grammar;
- Codex's default dual role and `--exclude-self`;
- report/round/loop defaults;
- adapter-controlled harness staging;
- named authority grants and the legacy global spelling;
- Antigravity's controlled-agent path and scoped fallback.

The canonical skill payload under `src/adversarial_friends/assets/` remains
the source of truth and is copied byte-identically into the plugin mirror.
Tests enforce that sync.

## 7. Test and verification strategy

Implementation follows red-green-refactor and adds coverage for:

- narrow skill activation examples and negative generic-review examples;
- parser normalization for named, repeated, wildcard, empty, unknown, and
  contradictory authority grants;
- Codex default inclusion, non-Codex default exclusion, both explicit flags,
  and mutual exclusion;
- frozen resume behavior and exact scoped-grant re-assertion;
- per-provider readiness and argv decisions in mixed rosters;
- generic workspace-asset parsing, path/digest validation, staging, auditing,
  cleanup, and HTTP refusal;
- Agy argv ordering and staged agent discovery;
- a hermetic fake-harness end-to-end test proving the generic staging seam;
- report labels for host self-review and scoped authority;
- plugin payload and version synchronization;
- documentation command/flag assertions.

Final verification runs targeted red-green tests, `make quality`, wheel and
isolated-install checks, a Codex plugin cachebuster/reinstall, and one bounded
live Agy smoke test. The smoke test is evidence about the installed Agy
version, not a permanent semantic guarantee; its observed tool list and CLI
version are recorded in the implementation review.

## 8. Failure behavior

- Ambiguous `afriend to X` target: ask for a path; do not review a guessed
  artifact.
- No usable friends after policy/readiness: existing exit 3.
- Invalid scoped grant or harness declaration: usage error, exit 2, before a
  provider is contacted.
- Harness staging failure: refuse the affected friend and report the exact
  target/reason without exposing file contents.
- Agy controlled agent still exposes tools: keep Agy policy-blocked under
  default deny; `--allow-external-tools agy` is the explicit generic fallback.
- Resume grant mismatch: refuse before opening the run for mutation.

## Acceptance criteria

The work is complete when:

1. A new Codex task can interpret `afriend to <artifact>` without the full
   product name, while generic review language does not select the skill.
2. A Codex-hosted automatic roster includes Codex unless `--exclude-self` is
   passed, and the report labels the result as host self-review.
3. Existing non-Codex host exclusion remains unchanged by default.
4. A generic adapter can stage a digest-pinned workspace asset without a
   provider-specific Python branch.
5. External-tool grants can name one or more providers without allowing the
   rest, while the valueless legacy flag still grants all.
6. Resume and report metadata preserve the exact scoped authority story.
7. Antigravity either runs with a verified empty-tool controlled agent or is
   runnable only through the explicit provider-scoped fallback; it is never
   mislabeled as denied.
8. Canonical and mirrored skill payloads are synchronized and the installed
   Codex plugin is refreshed.
9. All portable quality gates pass.

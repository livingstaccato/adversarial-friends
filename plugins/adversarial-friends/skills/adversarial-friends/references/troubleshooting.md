# Troubleshooting

## The report is empty or very short

Run `afriend doctor`. A friend that is missing, unauthenticated, or lacking a
read-only mode will not appear in the results. An empty report with no failed
friends listed is a real "nothing found"; an empty report where the friend
table shows failures is not — read the friend table before trusting an empty
findings section.

## Verified invocation traps

These were found by running into them while building the adapters this skill
ships, and **most returned exit status 0**. This is why the runner validates
output rather than trusting exit codes (see `ledger.md` and `SKILL.md`'s
"Exit status lies").

| CLI | Trap |
|---|---|
| `codex` | `codex resume` / `codex fork` are interactive ("picker by default"); the non-interactive forms are `codex exec resume` / `codex exec fork` |
| `agy` | `--print`/`-p` takes the prompt as its *value*, so every other flag must precede it on the command line or `--print` swallows the next token as the prompt |
| `claude` | `-p --permission-mode plan` routes the response into `~/.claude/plans/<name>.md` and prints three lines to stdout instead of the findings; the shipped adapter uses `--tools "Read,Grep,Glob"` for read-only instead, which stays in print mode |
| `agy` | on a long task, findings can be routed to a brain artifact file with only a summary and a `file://` link printed to stdout |
| `ollama` | `ollama run` writes ANSI cursor-control codes *inside* its own JSON payload even when stdout is not a terminal, which is why the adapter uses the HTTP API (`POST /api/generate`) instead of the CLI |

Short flags also collide across CLIs — confirmed against each CLI's own
`--help` on 2026-08-22: `-p` is `--print` on `claude` and `agy` but
`--profile` on `codex exec`; `-s` is `--sandbox` on `codex exec` but
`--session` on `opencode run`. Every adapter in `adapters/*.toml` spells its
flags long for exactly this reason — see `test_no_adapter_uses_short_flags`
in `tests/test_adapters.py`, which enforces it.

## A friend times out

The default is 900s (`--timeout` to change it). Reviewing a long document
genuinely takes minutes — a short default would kill real reviews before they
finish. Where a CLI has its own internal timeout (`agy --print-timeout`,
which defaults to 5 minutes), the adapter sets it explicitly to the friend's
configured `--timeout`, and the runner's own kill deadline is `--timeout +
60s` — strictly *greater* than the CLI's own timeout, not equal to it (spec
§11.3). That gap exists so a CLI with its own internal timeout gets the
chance to report its own timeout cleanly and exit, rather than being killed
by the runner at the exact instant it was trying to write that out.

## opencode looks read-only but is not

`opencode` has no read-only flag at all (`readonly_argv` in
`adapters/opencode.toml` is empty), so every `opencode` friend runs at
`scope: doc` by default — it only ever sees the artifact, never the
repository — unless you explicitly ask for repo scope, in which case it still
runs unsandboxed. `afriend doctor` reports this honestly: `readonly=False` for
`opencode` regardless of how it is invoked.

## opencode's effort is unverified

`opencode run --variant <name>` accepts any string silently — no error, no
warning, no distinguishable difference in behavior for a typo versus a real
variant name. There is no way to confirm from the outside which effort level
an `opencode` friend actually ran at, so `afriend doctor` and `run.json` report its
effort as `effort=unverified` rather than echoing back whatever was
requested. Every other shipped adapter (`claude`, `codex`, `agy`) reports
`effort=native` because each one rejects an unsupported level outright.

## ollama reports unreachable, or a run fails

`adapters/ollama.toml` declares `transport = "http"` and talks to
`POST /api/generate` — `ollama run` writes ANSI cursor-control codes inside
its own JSON payload, so the CLI is unusable as a machine interface.

Two things differ from every other friend:

**Availability is a reachable endpoint, not a binary on `PATH`.** `afriend
doctor` probes the server and prints `unreachable  (no server listening)` when
nothing answers. Start it with `ollama serve`.

**A model must be named explicitly.** ollama has no default, and its own error
for an omitted model explains nothing, so the runner refuses before dispatch.
Pass one in the third `--friend` slot:

```bash
afriend run spec.md --friend ollama:security:qwen3:0.6b
```

An ollama friend reports `schema=False readonly=False`. That is accurate, not
a downgrade: a bare model behind an endpoint has no filesystem access to
constrain, so no read-only flag was emitted and nothing was enforced.
Containment comes from doc scope — it is handed only the artifact text.

## gemini does not work

There is no `gemini` adapter in this build — not even a stub. The `gemini`
CLI returns `IneligibleTierError` on the individual free tier ("this client
is no longer supported… migrate to the Antigravity suite"), and Google's own
supported path from there is Antigravity. Use `agy`, which is Google's
Antigravity CLI and is a fully shipped adapter.

## A run directory already exists

`afriend run` refuses to reuse an existing run directory rather than silently
mixing two runs' ledgers together. This is normally unreachable (run ids are
generated internally and include a timestamp plus a random suffix), but it
can happen if `--out` points at a path that collides with a prior run, or at
a path that already exists as a plain file — both fail with a clean exit-2
error, not a traceback.

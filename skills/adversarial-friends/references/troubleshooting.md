# Troubleshooting

## The report is empty or very short

Run `bin/af doctor`. A friend that is missing, unauthenticated, or lacking a
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
| `ollama` | `ollama run` writes ANSI cursor-control codes *inside* its own JSON payload even when stdout is not a terminal — the HTTP API (`POST /api/generate`) avoids that, but this build has no HTTP transport implemented at all; see "ollama does not work" below |

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
runs unsandboxed. `af doctor` reports this honestly: `readonly=False` for
`opencode` regardless of how it is invoked.

## opencode's effort is unverified

`opencode run --variant <name>` accepts any string silently — no error, no
warning, no distinguishable difference in behavior for a typo versus a real
variant name. There is no way to confirm from the outside which effort level
an `opencode` friend actually ran at, so `af doctor` and `run.json` report its
effort as `effort=unverified` rather than echoing back whatever was
requested. Every other shipped adapter (`claude`, `codex`, `agy`) reports
`effort=native` because each one rejects an unsupported level outright.

## ollama does not work

`adapters/ollama.toml` declares `transport = "http"` (talking to
`POST /api/generate` avoids `ollama run`'s ANSI-in-JSON problem, see the
table above), but no HTTP transport is implemented in this build — only
`exec`/`Popen` dispatch is. `--friend ollama:*` is rejected outright with
exit `2` ("not implemented in this build") rather than trying to run it: the
adapter's `binary` field is empty (there is nothing to exec), so without this
check the failure would instead surface much later and far less clearly, as
an opaque `binary not found: ` from `spawn.run_process`. `af doctor` reports
`ollama` as `unimplemented`, not merely "found"/"missing", for the same
reason. Implementing real HTTP dispatch is a feature to build, not a bug to
fix here.

## gemini does not work

There is no `gemini` adapter in this build — not even a stub. The `gemini`
CLI returns `IneligibleTierError` on the individual free tier ("this client
is no longer supported… migrate to the Antigravity suite"), and Google's own
supported path from there is Antigravity. Use `agy`, which is Google's
Antigravity CLI and is a fully shipped adapter.

## A run directory already exists

`af run` refuses to reuse an existing run directory rather than silently
mixing two runs' ledgers together. This is normally unreachable (run ids are
generated internally and include a timestamp plus a random suffix), but it
can happen if `--out` points at a path that collides with a prior run, or at
a path that already exists as a plain file — both fail with a clean exit-2
error, not a traceback.

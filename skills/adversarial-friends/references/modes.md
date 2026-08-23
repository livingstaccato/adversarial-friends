# Modes

| Mode | Status | What it does |
|---|---|---|
| `report` | **implemented** | One round. Every friend critiques in parallel; claims are merged (exact-match only) and ranked by severity in `report.md`. |
| `crossexam` | planned | Friends then judge each other's claims across rounds until claims settle or deadlock. |
| `gate` | planned | Cross-examination, then every surviving non-advisory claim needs an explicit resolution. |
| `loop` | planned | Cross-examination, artifact revised, repeated until two rounds surface nothing new. |

`af run` rejects any `--mode` other than `report` with a usage error (exit 2)
rather than pretending to support it:

```
af: mode 'gate' is not implemented yet; only 'report' is available
```

Cross-examination is the mode this project exists for: it automates the
manual loop of handing one reviewer's findings to another and carrying the
argument back. The ledger `report` already writes (`claims.jsonl`, see
`ledger.md`) is the structure it needs, but no code path currently produces a
`verdict` or `resolution` record, and there is no `--merge=orchestrator`,
`--resume`, `af resolve`, or `af init` subcommand in this build — those are
part of the design this skill is built toward, not something you can invoke
today. Run `bin/af run --help` and `bin/af --help` to see the actual flags
this build accepts.

## Exit codes

`af run` and `af doctor` use these exit codes; not every code is reachable by
every command in this build:

| Code | Meaning | Reachable today via |
|---|---|---|
| `0` | success | `af run --mode report` (at least one friend usable), `af doctor` (at least one friend found) |
| `1` | gate blocked, or run incomplete | `af run --mode report` when every dispatched friend fails |
| `2` | usage/config error | a missing artifact, a malformed `--friend` value, an unknown `cli` in `--friend`, or `--mode` set to anything but `report` |
| `3` | no usable friends for the requested mode | `af run` when discovery finds nothing usable; `af doctor` when no friend binary is found |
| `10` | needs orchestrator | reserved for `--merge=orchestrator` and parse-halt recovery — not implemented in this build |
| `11` | ceiling hit | reserved for `crossexam`/`gate`/`loop` round and cost ceilings — not implemented in this build |

A run cancelled by `SIGINT`/`SIGTERM` exits `128 + signal number` instead of
any of the above, and `af` prints `aborted by signal N` to stderr.

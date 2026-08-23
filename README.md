![adversarial-friends](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/images/brand/adversarial-friends-banner.png)

# Adversarial Friends

A skill that challenges your specs, plans, and reviews by handing them to
**other** agent CLIs — codex, claude, agy, opencode — as independent
adversarial reviewers, then merging their critiques into one ranked findings
report.

It automates a workflow you may already do by hand: run a review, paste the
findings into a different model, ask whether they hold up, carry the argument
back. Doing that manually means holding a claim ledger in your head. This
keeps the ledger on disk.

## Why more than one model

A single reviewer produces confident prose. Several reviewers produce claims
that can be compared — and the disagreements are where the real problems are.
This tool's own design spec was built this way: `codex` returned 17 findings,
`claude` returned 15 plus one marked `unproven` ("lens leaks attribution"),
and a third reviewer, `agy`, independently reproduced two of `claude`'s
findings on its own and caught a shared-worktree race that neither of the
other two had flagged. The `unproven` claim was later confirmed and fixed.
No single reviewer's pass would have surfaced all of it — see the revision
history in [the design spec](docs/superpowers/specs/2026-08-22-adversarial-friends-design.md#19-revision-history)
for the full account.

## Install

Requires Python 3.11+ and at least one agent CLI besides the one you are
running under. No dependencies to install — the runner is stdlib-only.

```bash
git clone https://github.com/livingstaccato/adversarial-friends
cd adversarial-friends
bin/af doctor
```

`doctor` tells you which friends are available and what each can actually
enforce — schema validation, a real read-only mode, a verifiable effort level.

## Use

```bash
bin/af run docs/my-design.md --mode report
```

Read `report.md` in the run directory it prints.

## What's implemented

`report` is the only mode this build runs — every friend critiques the
artifact once, in parallel, and the claims are merged into one report.
Cross-examination (friends judging each other's claims across rounds),
gates, and revision loops are the design this tool is built toward, not
something you can invoke today. `af run --mode crossexam` (or `gate`, or
`loop`) exits `2` with a message saying so rather than pretending to run
them. See [docs/](docs/README.md) for what each mode is meant to become.

Four friends ship: `claude`, `codex`, `agy`, and `opencode`. An `ollama`
adapter is declared but not implemented in this build — it uses an HTTP
transport (no CLI to exec), which this runner does not support yet; `--friend
ollama:*` exits `2` rather than pretending to work. There is no `gemini`
adapter — the `gemini` CLI returns an ineligible-tier error on the
individual free tier, and Google's own supported path from there is
Antigravity, which is `agy`.

## Documentation

See [docs/](docs/README.md).

## License

MIT — see [LICENSE](LICENSE).

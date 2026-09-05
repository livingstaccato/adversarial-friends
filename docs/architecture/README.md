# Architecture Diagrams

Each diagram has a `.puml` source plus committed `.png` and `.svg` renders.
The renders are committed because `README.md` embeds them by absolute
`raw.githubusercontent.com` URL — a relative path breaks on PyPI and anywhere
the README is mirrored.

| Diagram | Source | What it answers |
|---|---|---|
| Module architecture | [`components.puml`](components.puml) | Which module owns scope warnings, resolver-safe confinement, and the rest of a run's boundaries |
| Run flow | [`run-flow.puml`](run-flow.puml) | How `afriend run` resolves automatic artifact-derived or explicit `--repo` context, records Git-blob binding or an independently frozen artifact, admits providers, applies scoped authority, preserves resolver access under Linux confinement, stages harnesses, dispatches a report fan-out, and records downgrades |
| Claim lifecycle | [`claim-lifecycle.puml`](claim-lifecycle.puml) | How two friends finding the same defect become one corroborated claim without losing either attribution |
| Cross-examination states | [`crossexam-states.puml`](crossexam-states.puml) | The eight states a claim can reach under `--mode crossexam`, which are terminal, and which need a human |
| The gate loop | [`gate-workflow.puml`](gate-workflow.puml) | How `--mode gate` and `afriend resolve` fit together, and the two things a resolution can be refused for |
| Skill routing | [`skill-routing.puml`](skill-routing.puml) | How `/afriend` presents session preflight, routes focused operations, records lifecycle events, and exposes read-only run status |

## Regenerating

```bash
make diagrams
```

Requires `plantuml` and `graphviz`:

```bash
brew install plantuml graphviz
```

## Conventions

These are the things that broke in review and are easy to reintroduce:

- **Colour an activity with `:text;<<#RRGGBB>>`, never `#RRGGBB:text;`.**
  The second form is deprecated and PlantUML renders a warning banner *into
  the image* rather than failing the build.
- **Wrap CLI flags in `""` — `""--mode""`, not `--mode`.** A line containing
  two `--` sequences is parsed as strikethrough markup, so `--mode / --preset`
  silently renders struck through. The `""` form also renders monospace.
- **Don't put `<size:...>` tags in `cloud`/`database` labels.** The closing
  `</size>` leaks into the rendered label as literal text.
- Keep diagrams accurate to the code, not to intent. Every step in
  `run-flow.puml` is traceable to `src/afriend/commands/run.py`.

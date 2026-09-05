# Canonical afriend Identity Design

## Purpose

Make `afriend` the single canonical identity for the product while reserving
the previously used and likely mistyped PyPI distribution names without
duplicating runtime code or creating alternate imports.

## Canonical identity

| Surface | Canonical value |
| --- | --- |
| GitHub repository | `livingstaccato/afriend` |
| PyPI distribution | `afriend` |
| Python import | `afriend` |
| Source directory | `src/afriend/` |
| Console command | `afriend` |
| Codex and Claude plugin | `afriend` |
| Documentation and metadata URLs | `livingstaccato/afriend` |

The repository is renamed through GitHub. Its automatic redirect from
`livingstaccato/adversarial-friends` remains in place; no new repository is
created at the old name, because doing so would take over and defeat that
redirect.

## Compatibility distributions

Two deliberately minimal PyPI distributions reserve alternate names:

| Distribution | Behavior |
| --- | --- |
| `adversarial-friends` | Depends exactly on the matching `afriend` version. |
| `afriends` | Depends exactly on the matching `afriend` version. |

They contain no Python modules, package data, console scripts, plugins, or
runtime logic. Installing either distribution installs the real `afriend`
distribution, whose command remains `afriend` and whose import remains
`afriend`. In particular, neither `import afriends` nor
`import adversarial_friends` is supported.

Each compatibility distribution uses the same release version as `afriend`
and pins its dependency to that exact version. This makes every release a
coherent, auditable set and prevents an alias install from silently selecting
an unreviewed future canonical version.

PyPI normalizes case and runs of `.`, `-`, and `_`, so each registered name
also reserves its normalized spelling variants. `afriends` is separately
registered because it is a distinct normalized name. This reservation lowers
exact-name typo and dependency-confusion risk; it does not claim to reserve
every semantic lookalike.

## Repository layout and release flow

The canonical runtime stays at the repository root. Two metadata-only build
projects live in a clearly named compatibility-distributions directory. The
release workflow builds all three projects from the tagged source, verifies
their metadata and artifacts, and performs isolated installations for:

1. `afriend`, confirming `afriend --version` and `import afriend`.
2. `adversarial-friends`, confirming it resolves the matching `afriend`
   command and distribution.
3. `afriends`, confirming the same resolution.

The workflow publishes the three verified artifacts as one ordered release
sequence: canonical `afriend` first, then the two compatibility distributions,
and creates one GitHub release only after all three publishes succeed. The
release notes identify `afriend` as canonical and describe the other
distributions as compatibility and reservation packages.

Trusted publishers for `afriend`, `adversarial-friends`, and `afriends` use
the canonical repository `livingstaccato/afriend`, workflow `release.yml`,
and environment `pypi`. The two new projects use pending publishers before
the first release. The existing `adversarial-friends` project's publisher is
updated after the GitHub rename.

## Versioning and migration state

The already-pushed `v0.6.0` tag remains immutable provenance: its code passed
verification but PyPI rejected publication because the new `afriend` project
was not configured. Do not move or reuse that tag. The canonical-repository
and compatibility-package release is `0.6.1`.

Current documentation names `afriend` directly. The only intentional uses of
`adversarial-friends` are the compatibility distribution, the retained
GitHub redirect, historical records, and explicit compatibility guidance.

## Failure handling

- If a compatibility package cannot resolve its exact canonical dependency,
  its installation fails rather than falling back to unrelated code.
- If any one distribution fails verification or publication, the release
  workflow stops and does not create the GitHub release. PyPI publication is
  not transactional, so an earlier artifact can already be public; recovery
  is a new coordinated version after diagnosing the failure.
- A failed tag is never retargeted. A correction uses a new version and tag.
- The workflow must reject unexpected runtime files, import packages, console
  scripts, or plugin payloads in either compatibility artifact.

## Verification

The implementation must prove:

- The renamed repository, package metadata, plugin metadata, docs, and
  release URLs agree on `afriend`.
- The compatibility wheels contain metadata only and declare the exact
  canonical dependency.
- Each alias installation exposes only the real `afriend` command and import.
- PyPI publisher configuration matches the renamed repository before a new
  release is tagged.
- Full quality, artifact checks, and the release workflow’s supported-Python
  installed-wheel matrix pass before publication.

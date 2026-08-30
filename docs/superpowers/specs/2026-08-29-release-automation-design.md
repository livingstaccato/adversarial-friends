# Adversarial Friends Release Automation Design

**Status:** Approved for the 0.2.0 release

## Goal

Publish the exact source represented by a `v<version>` tag to PyPI, then
create the matching GitHub release, without giving build code access to PyPI
credentials or allowing a tag from an unmerged branch to publish.

## Release contract

The workflow is triggered only by version tags. Before building, it must prove
that the tag name equals `v` plus the repository `VERSION`, and that the tagged
commit is an ancestor of `origin/main`. A failure in either check stops the
release before any package is built or any publishing identity is requested.

The release has three jobs with deliberately separate permissions:

1. `build` checks out the tagged commit, validates the release contract, builds
   one wheel and one source distribution, checks their metadata, smoke-tests
   the wheel's installed `afriend --version`, and uploads the immutable files
   as a workflow artifact. It has read-only repository access and no OIDC token.
2. `publish` downloads only that artifact and publishes it through PyPI Trusted
   Publishing. It is the only job with `id-token: write` and runs in the `pypi`
   GitHub environment.
3. `github-release` runs only after PyPI publication. It downloads the same
   files, extracts the matching version section from `CHANGELOG.md`, and creates
   a GitHub release with the wheel and source distribution attached.

All third-party actions are pinned to full commit SHAs, with the corresponding
release tag recorded in a comment for maintainability.

## Failure behavior

- A mismatched tag or a tag outside `main` fails in `build`.
- A malformed or incomplete distribution fails metadata, filename, or install
  verification before upload.
- A missing or incorrect trusted-publisher configuration fails in `publish`;
  no GitHub release is created.
- A PyPI upload succeeds before GitHub release creation. If GitHub release
  creation then fails, rerunning only that job is safe because it does not
  republish the package.
- Existing PyPI files are never silently skipped; duplicate versions fail.

## External prerequisites

The GitHub repository must have an environment named `pypi`. PyPI's
`adversarial-friends` project must trust:

- owner: `livingstaccato`
- repository: `adversarial-friends`
- workflow: `release.yml`
- environment: `pypi`

The `pypi` environment should require manual approval. These settings are
external deployment controls rather than repository files, so the release
stops before tagging if they cannot be verified or configured safely.

## Verification

A focused pytest contract will inspect the workflow as data and enforce its
trigger, permissions, job separation, validation commands, artifact flow, and
release ordering. The repository's full `make quality` gate and `act --list`
will then validate that the new test and workflow coexist with every portable
project check. Hosted CI on `main` remains the final pre-tag gate.

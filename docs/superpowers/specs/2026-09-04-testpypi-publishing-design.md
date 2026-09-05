# TestPyPI Publishing Design

## Goal

Provide a manual, trusted-publisher TestPyPI rehearsal for the canonical
`afriend` distribution and its two compatibility distributions, without
creating a GitHub release or changing the production PyPI path.

## Architecture

`.github/workflows/test-release.yml` will run only when explicitly dispatched.
Its build job will run the existing six-artifact verifier and upload the
verified artifact bundle. Three dependent publishing jobs will upload `afriend`,
then `adversarial-friends`, then `afriends`. Each job has only `id-token: write`
and exactly one invocation of the PyPA publishing action, passing
`repository-url: https://test.pypi.org/legacy/` to the pinned PyPA publishing
action.

The `testpypi-afriend`, `testpypi-adversarial-friends`, and `testpypi-afriends`
GitHub environments give every pending trusted publisher a distinct OIDC
identity. Separating the jobs also follows the PyPA action's documented limit
of one publishing invocation per job. The workflow deliberately has no tag trigger, changelog
requirement, main-ancestry requirement, or GitHub-release job: it is an index
rehearsal, not a release.

## Operational contract

- The user manually starts the workflow from the GitHub Actions UI.
- A given package version can be uploaded once to TestPyPI. That uniqueness is
  independent of PyPI: `0.6.1` is valid on TestPyPI if it is absent there.
- TestPyPI trusted publishers use repository `livingstaccato/afriend`, workflow
  `test-release.yml`, and environments `testpypi-afriend` (`afriend`),
  `testpypi-adversarial-friends` (`adversarial-friends`), and
  `testpypi-afriends` (`afriends`).
- A failed build prevents every upload; a failed canonical publication prevents
  the typo-alias publication.

## Verification

Focused workflow-contract tests assert manual dispatch, no release-specific
jobs or permissions, exact artifact verification, ordered publishing, OIDC-only
credentials, the TestPyPI endpoint, and both environments. Existing quality
checks continue to validate the generated distributions.

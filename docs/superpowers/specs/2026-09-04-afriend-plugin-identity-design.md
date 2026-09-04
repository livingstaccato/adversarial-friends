# afriend Plugin Identity Design

## Goal

Make `afriend` the installed plugin and qualified-skill namespace while
retaining `adversarial-friends` as the Python distribution, repository name,
product name, and persisted runtime-data namespace.

## Scope

The plugin directory becomes `plugins/afriend`. Its Codex and Claude manifests
use `"name": "afriend"`, and both marketplace entries point to that directory
and name. Codex direct forms become `$afriend:afriend`, `$afriend:review`,
`$afriend:status`, `$afriend:configure`, and `$afriend:resolve`. Friendly copy
continues to use a space: `afriend review`.

The current-facing README, AGENTS instructions, skill entrypoints, evaluation
fixtures, and tests use the new qualified forms. Historical review, plan, and
release records remain evidence of their own time and are unchanged.

## Boundaries

This does not rename the Python distribution (`adversarial-friends`), import
package (`adversarial_friends`), CLI (`afriend`), repository URLs, image assets,
or config and state paths. Those names identify released artifacts and existing
runtime data, not the Codex plugin namespace.

## Verification

Tests first assert the desired `afriend` manifest, directory, marketplace
source, default prompts, and qualified selectors. The implementation then
renames the plugin directory and updates all current-facing references. The
plugin projection, version synchronization, full quality gate, and a local
Codex install must confirm the installed identifier is `afriend@afriend-local`
and no `adversarial-friends@afriend-local` copy remains enabled.

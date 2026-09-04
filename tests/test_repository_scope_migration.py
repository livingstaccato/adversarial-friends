"""Migration coverage for the explicit repository scope metadata."""

import pytest

from adversarial_friends.commands.runmeta import migrate_meta


@pytest.mark.parametrize("interim_shape", ["audit", "downgrade"])
def test_interim_explicit_scope_migration_normalizes_its_audit(interim_shape):
    marker = (
        "repository scope selected explicitly; frozen artifact independently "
        "bound (not Git-blob-bound)."
    )
    raw: dict[str, object] = {"schema_version": 3, "downgrades": ["ordinary warning"]}
    if interim_shape == "audit":
        raw["repository_scope_audit"] = marker
    else:
        raw["downgrades"] = ["ordinary warning", marker]

    migrated = migrate_meta(raw)

    assert migrated["repository_scope_mode"] == "explicit"
    assert migrated["repository_scope_audit"] == marker
    assert migrated["downgrades"] == ["ordinary warning"]

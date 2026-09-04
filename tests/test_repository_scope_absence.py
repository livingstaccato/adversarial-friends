"""Pre-feature run metadata must not acquire repository scope authority."""

import pytest

from adversarial_friends.commands.runmeta import CURRENT_SCHEMA_VERSION, migrate_meta


@pytest.mark.parametrize("marker_field", ["repository_scope_audit", "downgrades"])
def test_missing_scope_mode_is_not_inferred_from_explicit_scope_prose(marker_field):
    marker = (
        "repository scope selected explicitly; frozen artifact independently "
        "bound (not Git-blob-bound)."
    )
    raw: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "downgrades": ["ordinary warning"],
    }
    raw[marker_field] = marker if marker_field == "repository_scope_audit" else [marker]

    migrated = migrate_meta(raw)

    assert "repository_scope_mode" not in migrated
    assert migrated[marker_field] == raw[marker_field]

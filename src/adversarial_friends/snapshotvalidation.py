"""Validation helpers for untrusted snapshot metadata."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
import re
from typing import cast

from .errors import UsageError

COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _validate_commit(value: str, field: str = "commit") -> None:
    if COMMIT_RE.fullmatch(value) is None:
        raise UsageError(f"cannot resume: saved snapshot {field} must be 40 hexadecimal characters")


def _validate_source_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\0" in value
        or candidate.is_absolute()
        or value != candidate.as_posix()
        or not candidate.parts
        or candidate == PurePosixPath(".")
        or ".." in candidate.parts
    ):
        raise UsageError(
            "cannot resume: saved snapshot source_path must be a canonical repository-relative path"
        )
    return value


def _required_string(raw: Mapping[str, object], field: str) -> str:
    if field not in raw:
        raise UsageError(f"cannot resume: saved snapshot field {field!r} is required")
    value = raw[field]
    if not isinstance(value, str) or not value:
        raise UsageError(
            f"cannot resume: saved snapshot field {field!r} must be a non-empty string"
        )
    return value


def _optional_string(raw: Mapping[str, object], field: str) -> str | None:
    if field not in raw:
        raise UsageError(f"cannot resume: saved snapshot field {field!r} is required")
    value = raw[field]
    if value is not None and not isinstance(value, str):
        raise UsageError(f"cannot resume: saved snapshot field {field!r} must be a string or null")
    if value == "":
        raise UsageError(f"cannot resume: saved snapshot field {field!r} must not be empty")
    return value


def _optional_bool(raw: Mapping[str, object], field: str) -> bool | None:
    if field not in raw:
        return None
    value = raw[field]
    if not isinstance(value, bool):
        raise UsageError(f"cannot resume: saved snapshot field {field!r} must be a boolean")
    return value


def _string_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise UsageError(f"cannot resume: saved {context} must be an object with string keys")
    return cast("Mapping[str, object]", value)


def _legacy_dict(meta: Mapping[str, object]) -> dict[str, object]:
    required = ["repo_root", "snapshot_sha", "artifact_hash"]
    missing = [field for field in required if field not in meta]
    if "artifact_path" not in meta and "artifact" not in meta:
        missing.append("artifact_path (or artifact)")
    if missing:
        raise UsageError(
            "cannot resume: saved legacy snapshot metadata is incomplete; missing "
            + ", ".join(missing)
        )
    saved_path = meta.get("artifact_path")
    saved_name = meta.get("artifact")
    artifact_path: object
    if isinstance(saved_path, str) and saved_path:
        artifact_path = saved_path
    elif isinstance(saved_name, str) and saved_name:
        artifact_path = saved_name
    else:
        artifact_path = saved_path if "artifact_path" in meta else saved_name
    return {
        "repo_root": meta["repo_root"],
        "commit": meta["snapshot_sha"],
        "tree": None,
        "artifact_path": artifact_path,
        "artifact_hash": meta["artifact_hash"],
        "predecessor": None,
    }


def _has_complete_legacy_shape(meta: Mapping[str, object]) -> bool:
    return all(field in meta for field in ("repo_root", "snapshot_sha", "artifact_hash")) and (
        "artifact_path" in meta or "artifact" in meta
    )


def _validate_present_snapshot_fields(raw: Mapping[str, object]) -> None:
    if "repo_root" in raw:
        _optional_string(raw, "repo_root")
    commit = _optional_string(raw, "commit") if "commit" in raw else None
    tree = _optional_string(raw, "tree") if "tree" in raw else None
    if "artifact_path" in raw:
        _required_string(raw, "artifact_path")
    artifact_hash = _required_string(raw, "artifact_hash") if "artifact_hash" in raw else None
    predecessor = _optional_string(raw, "predecessor") if "predecessor" in raw else None
    source_path = _optional_string(raw, "source_path") if "source_path" in raw else None
    _optional_bool(raw, "artifact_bound_to_snapshot")
    if commit is not None:
        _validate_commit(commit)
    if tree is not None:
        _validate_commit(tree, "tree")
    if artifact_hash is not None and HASH_RE.fullmatch(artifact_hash) is None:
        raise UsageError(
            "cannot resume: saved snapshot artifact_hash must be sha256:<64 lowercase hex digits>"
        )
    if predecessor is not None and (
        COMMIT_RE.fullmatch(predecessor) is None and HASH_RE.fullmatch(predecessor) is None
    ):
        raise UsageError(
            "cannot resume: saved snapshot predecessor must be a commit or artifact hash"
        )
    if source_path is not None:
        _validate_source_path(source_path)


def _reject_inconsistent_explicit_binding(raw: Mapping[str, object]) -> None:
    binding = _optional_bool(raw, "artifact_bound_to_snapshot")
    if binding is None:
        return
    if binding and raw.get("repo_root") is None:
        raise UsageError(
            "cannot resume: saved snapshot artifact_bound_to_snapshot requires a repository"
        )
    if binding and raw.get("source_path") is None:
        raise UsageError(
            "cannot resume: saved snapshot artifact_bound_to_snapshot requires source_path"
        )
    if not binding and raw.get("source_path") is not None:
        raise UsageError(
            "cannot resume: saved snapshot source_path requires artifact_bound_to_snapshot"
        )


def _nested_legacy_conflict(
    raw: Mapping[str, object], meta: Mapping[str, object], *, complete: bool
) -> None:
    comparisons = {
        "repo_root": "repo_root",
        "commit": "snapshot_sha",
        "artifact_hash": "artifact_hash",
    }
    for nested_field, legacy_field in comparisons.items():
        if legacy_field not in meta:
            continue
        nested_value = raw.get(nested_field)
        legacy_value = meta[legacy_field]
        legacy_value_is_valid = (
            (
                legacy_field == "repo_root"
                and (legacy_value is None or (isinstance(legacy_value, str) and bool(legacy_value)))
            )
            or (
                legacy_field == "snapshot_sha"
                and (
                    legacy_value is None
                    or (
                        isinstance(legacy_value, str)
                        and COMMIT_RE.fullmatch(legacy_value) is not None
                    )
                )
            )
            or (
                legacy_field == "artifact_hash"
                and isinstance(legacy_value, str)
                and HASH_RE.fullmatch(legacy_value) is not None
            )
        )
        if (
            legacy_value_is_valid
            and nested_field in raw
            and (complete or nested_value is not None)
            and nested_value != legacy_value
        ):
            raise UsageError(
                f"cannot resume: saved nested snapshot conflicts with legacy field {nested_field!r}"
            )

"""Validate and securely stage digest-pinned adapter workspace assets."""

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re

from .errors import UsageError
from .paths import assets_root
from .secureio import secure_create_bytes, secure_mkdir, secure_read_bytes

# Harnesses are expected to be small text/config payloads. These ceilings keep
# both staging work and its durable audit comfortably below the existing 32 MiB
# secure-read ceiling used for per-friend metadata.
MAX_WORKSPACE_ASSETS = 32
MAX_WORKSPACE_ASSET_PATH_BYTES = 1024
MAX_WORKSPACE_ASSET_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_ASSET_TOTAL_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_ASSET_AUDIT_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_FIELDS = frozenset({"source", "target", "sha256"})
_AUDIT_FIELDS = frozenset({"source", "target", "expected_sha256", "observed_sha256", "status"})
_AUDIT_STATUSES = frozenset(
    {
        "staged",
        "failed-digest-mismatch",
        "failed-source-unavailable",
        "failed-aggregate-too-large",
        "failed-target-exists-or-unsafe",
        "failed-invalid-declaration",
        "not-staged",
    }
)


@dataclass(frozen=True)
class WorkspaceAsset:
    """One package-owned source and its run-local destination."""

    source: str
    target: str
    sha256: str


@dataclass(frozen=True)
class WorkspaceAssetAudit:
    """Actual staging outcome, safe to persist and render."""

    source: str
    target: str
    expected_sha256: str | None
    observed_sha256: str | None
    status: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "source": self.source,
            "target": self.target,
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "status": self.status,
        }


class WorkspaceAssetStagingError(Exception):
    """A per-friend refusal carrying every declaration's staging audit."""

    def __init__(self, message: str, audits: tuple[WorkspaceAssetAudit, ...]) -> None:
        super().__init__(message)
        self.audits = audits


def _audit_footprint(entries: list[dict[str, str | None]]) -> int:
    return len(f"workspace_assets={entries!r}\n".encode())


def _canonical_relative(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise UsageError(f"workspace_assets {field} must be a nonempty relative POSIX path")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise UsageError(f"workspace_assets {field} must be a canonical relative POSIX path")
    try:
        path_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise UsageError(f"workspace_assets {field} must be valid UTF-8") from exc
    if path_bytes > MAX_WORKSPACE_ASSET_PATH_BYTES:
        raise UsageError(
            f"workspace_assets {field} exceeds the {MAX_WORKSPACE_ASSET_PATH_BYTES}-byte "
            "UTF-8 limit"
        )
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise UsageError(f"workspace_assets {field} has an invalid path segment")
    canonical = PurePosixPath(value).as_posix()
    if canonical != value or PurePosixPath(value).is_absolute():
        raise UsageError(f"workspace_assets {field} must be a canonical relative POSIX path")
    return value


def _validate_declarations(
    assets: tuple[WorkspaceAsset, ...], *, transport: str
) -> tuple[WorkspaceAsset, ...]:
    if transport == "http" and assets:
        raise UsageError("HTTP adapters may not declare workspace_assets")
    if len(assets) > MAX_WORKSPACE_ASSETS:
        raise UsageError(f"workspace_assets count exceeds the {MAX_WORKSPACE_ASSETS}-asset limit")
    seen_targets: set[str] = set()
    target_parts: list[tuple[str, ...]] = []
    validated: list[WorkspaceAsset] = []
    for asset in assets:
        if not isinstance(asset, WorkspaceAsset):
            raise UsageError("workspace_assets entries must be WorkspaceAsset values")
        source = _canonical_relative(asset.source, "source")
        target = _canonical_relative(asset.target, "target")
        if type(asset.sha256) is not str or not _SHA256_RE.fullmatch(asset.sha256):
            raise UsageError("workspace_assets sha256 must be exactly 64 lowercase hex characters")
        if target in seen_targets:
            raise UsageError(f"workspace_assets has duplicate target {target!r}")
        parts = PurePosixPath(target).parts
        for prior in target_parts:
            shared = min(len(parts), len(prior))
            if parts[:shared] == prior[:shared]:
                raise UsageError(
                    "workspace_assets targets may not have an ancestor/descendant overlap"
                )
        seen_targets.add(target)
        target_parts.append(parts)
        validated.append(WorkspaceAsset(source, target, asset.sha256))
    largest_outcome = [
        WorkspaceAssetAudit(
            asset.source,
            asset.target,
            asset.sha256,
            asset.sha256,
            "failed-target-exists-or-unsafe",
        ).as_dict()
        for asset in validated
    ]
    if _audit_footprint(largest_outcome) > MAX_WORKSPACE_ASSET_AUDIT_BYTES:
        raise UsageError(
            "workspace_assets audit aggregate bytes exceed the "
            f"{MAX_WORKSPACE_ASSET_AUDIT_BYTES}-byte limit"
        )
    return tuple(validated)


def _read_source(asset: WorkspaceAsset, source_root: Path) -> bytes:
    try:
        return secure_read_bytes(
            source_root / Path(*PurePosixPath(asset.source).parts),
            root=source_root,
            max_bytes=MAX_WORKSPACE_ASSET_BYTES,
        )
    except OSError as exc:
        raise UsageError(
            f"workspace_assets source {asset.source!r} is unavailable or unsafe"
        ) from exc


def validate_workspace_assets(
    assets: tuple[WorkspaceAsset, ...],
    *,
    transport: str,
    source_root: Path | None = None,
) -> tuple[WorkspaceAsset, ...]:
    """Validate declarations and pinned package bytes without writing."""
    validated = _validate_declarations(assets, transport=transport)
    root = assets_root() if source_root is None else Path(source_root)
    total_bytes = 0
    for asset in validated:
        payload = _read_source(asset, root)
        observed = hashlib.sha256(payload).hexdigest()
        if observed != asset.sha256:
            raise UsageError(f"workspace_assets source {asset.source!r} digest mismatch")
        total_bytes += len(payload)
        if total_bytes > MAX_WORKSPACE_ASSET_TOTAL_BYTES:
            raise UsageError(
                "workspace_assets aggregate staged bytes exceed the "
                f"{MAX_WORKSPACE_ASSET_TOTAL_BYTES}-byte limit"
            )
    return validated


def parse_workspace_assets(
    value: object,
    *,
    transport: str,
    source_root: Path | None = None,
) -> tuple[WorkspaceAsset, ...]:
    """Parse adapter TOML ``[[workspace_assets]]`` tables and validate bytes."""
    if type(value) is not list:
        raise UsageError("workspace_assets must be an array of tables")
    parsed: list[WorkspaceAsset] = []
    for entry in value:
        if type(entry) is not dict or set(entry) != _ASSET_FIELDS:
            raise UsageError("workspace_assets entries require exactly source, target, and sha256")
        parsed.append(
            WorkspaceAsset(
                source=entry["source"],
                target=entry["target"],
                sha256=entry["sha256"],
            )
        )
    return validate_workspace_assets(tuple(parsed), transport=transport, source_root=source_root)


def normalize_workspace_asset_audits(value: object) -> list[dict[str, str | None]]:
    """Validate attacker-editable persisted staging outcomes for replay."""
    if type(value) is not list:
        raise UsageError("workspace_assets audit must be a list")
    if len(value) > MAX_WORKSPACE_ASSETS:
        raise UsageError(
            f"workspace_assets audit count exceeds the {MAX_WORKSPACE_ASSETS}-asset limit"
        )
    normalized: list[dict[str, str | None]] = []
    for entry in value:
        if type(entry) is not dict or set(entry) != _AUDIT_FIELDS:
            raise UsageError("workspace_assets audit entries have an invalid shape")
        source = _canonical_relative(entry["source"], "audit source")
        target = _canonical_relative(entry["target"], "audit target")
        expected = entry["expected_sha256"]
        observed = entry["observed_sha256"]
        status = entry["status"]
        if expected is not None and (
            type(expected) is not str or not _SHA256_RE.fullmatch(expected)
        ):
            raise UsageError("workspace_assets audit expected_sha256 is invalid")
        if observed is not None and (
            type(observed) is not str or not _SHA256_RE.fullmatch(observed)
        ):
            raise UsageError("workspace_assets audit observed_sha256 is invalid")
        if type(status) is not str or status not in _AUDIT_STATUSES:
            raise UsageError("workspace_assets audit status is invalid")
        if status != "failed-invalid-declaration" and expected is None:
            raise UsageError("workspace_assets audit expected_sha256 is missing")
        if status == "failed-invalid-declaration" and observed is not None:
            raise UsageError("workspace_assets invalid declaration audit observed bytes")
        if status == "staged" and observed != expected:
            raise UsageError("workspace_assets staged audit digest is inconsistent")
        if status == "failed-digest-mismatch" and (observed is None or observed == expected):
            raise UsageError("workspace_assets mismatch audit digest is inconsistent")
        if status in {"failed-source-unavailable", "not-staged"} and observed is not None:
            raise UsageError("workspace_assets unstaged audit must not have an observed digest")
        if status == "failed-target-exists-or-unsafe" and observed != expected:
            raise UsageError("workspace_assets target refusal audit digest is inconsistent")
        if status == "failed-aggregate-too-large" and observed != expected:
            raise UsageError("workspace_assets aggregate refusal audit digest is inconsistent")
        normalized.append(
            {
                "source": source,
                "target": target,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "status": status,
            }
        )
    footprint = _audit_footprint(normalized)
    if footprint > MAX_WORKSPACE_ASSET_AUDIT_BYTES:
        raise UsageError(
            "workspace_assets audit aggregate bytes exceed the "
            f"{MAX_WORKSPACE_ASSET_AUDIT_BYTES}-byte limit"
        )
    return normalized


def _audit(asset: WorkspaceAsset, observed: str | None, status: str) -> WorkspaceAssetAudit:
    return WorkspaceAssetAudit(
        source=asset.source,
        target=asset.target,
        expected_sha256=asset.sha256,
        observed_sha256=observed,
        status=status,
    )


def _safe_audit_path(value: object) -> str:
    try:
        return _canonical_relative(value, "audit path")
    except UsageError:
        return "invalid"


def _invalid_declaration_audits(
    assets: tuple[WorkspaceAsset, ...],
) -> tuple[WorkspaceAssetAudit, ...]:
    bounded: list[WorkspaceAssetAudit] = []
    for asset in assets[:MAX_WORKSPACE_ASSETS]:
        if not isinstance(asset, WorkspaceAsset):
            candidate = WorkspaceAssetAudit(
                "invalid", "invalid", None, None, "failed-invalid-declaration"
            )
        else:
            candidate = WorkspaceAssetAudit(
                source=_safe_audit_path(asset.source),
                target=_safe_audit_path(asset.target),
                expected_sha256=(
                    asset.sha256
                    if type(asset.sha256) is str and _SHA256_RE.fullmatch(asset.sha256)
                    else None
                ),
                observed_sha256=None,
                status="failed-invalid-declaration",
            )
        proposed = [entry.as_dict() for entry in (*bounded, candidate)]
        if _audit_footprint(proposed) > MAX_WORKSPACE_ASSET_AUDIT_BYTES:
            break
        bounded.append(candidate)
    if not bounded:
        bounded.append(
            WorkspaceAssetAudit("invalid", "invalid", None, None, "failed-invalid-declaration")
        )
    return tuple(bounded)


def _raise_staging(
    assets: tuple[WorkspaceAsset, ...],
    completed: list[WorkspaceAssetAudit],
    failed_index: int,
    status: str,
    observed: str | None,
) -> None:
    failed = assets[failed_index]
    completed.append(_audit(failed, observed, status))
    completed.extend(_audit(asset, None, "not-staged") for asset in assets[failed_index + 1 :])
    raise WorkspaceAssetStagingError(
        f"workspace asset staging refused: {failed.target}: {status}", tuple(completed)
    )


def stage_workspace_assets(
    assets: tuple[WorkspaceAsset, ...],
    isolation_root: Path,
    *,
    source_root: Path | None = None,
) -> tuple[WorkspaceAssetAudit, ...]:
    """Stage assets below one isolation root without following or replacing names."""
    if not assets:
        return ()
    try:
        validated = _validate_declarations(assets, transport="exec")
    except UsageError as exc:
        raise WorkspaceAssetStagingError(
            "workspace asset staging refused: invalid declaration",
            _invalid_declaration_audits(assets),
        ) from exc
    root = assets_root() if source_root is None else Path(source_root)
    isolation = Path(isolation_root)
    prepared: list[tuple[WorkspaceAsset, bytes, str]] = []
    total_bytes = 0
    for index, asset in enumerate(validated):
        not_staged = [_audit(prior, None, "not-staged") for prior in validated[:index]]
        try:
            payload = _read_source(asset, root)
        except UsageError:
            _raise_staging(validated, not_staged, index, "failed-source-unavailable", None)
        observed = hashlib.sha256(payload).hexdigest()
        if observed != asset.sha256:
            _raise_staging(validated, not_staged, index, "failed-digest-mismatch", observed)
        total_bytes += len(payload)
        if total_bytes > MAX_WORKSPACE_ASSET_TOTAL_BYTES:
            _raise_staging(validated, not_staged, index, "failed-aggregate-too-large", observed)
        prepared.append((asset, payload, observed))

    audits: list[WorkspaceAssetAudit] = []
    for index, (asset, payload, observed) in enumerate(prepared):
        relative = PurePosixPath(asset.target)
        target = isolation.joinpath(*relative.parts)
        try:
            if len(relative.parts) > 1:
                secure_mkdir(target.parent, parents=True, exist_ok=True, root=isolation)
            secure_create_bytes(target, payload, root=isolation)
        except OSError:
            _raise_staging(validated, audits, index, "failed-target-exists-or-unsafe", observed)
        audits.append(_audit(asset, observed, "staged"))
    return tuple(audits)

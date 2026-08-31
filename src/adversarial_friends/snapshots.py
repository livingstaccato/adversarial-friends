"""Immutable artifact/repository identity for fresh runs and resumes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
import dataclasses
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import stat
import subprocess
from typing import cast

from . import isolation
from .errors import UsageError

COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SNAPSHOT_FIELDS = frozenset(
    {"repo_root", "commit", "tree", "artifact_path", "artifact_hash", "predecessor"}
)


def _unavailable(detail: str) -> UsageError:
    return UsageError(f"cannot resume: saved snapshot is unavailable: {detail}")


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    except OSError as exc:
        raise _unavailable(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git lookup failed"
        raise _unavailable(detail)
    return result.stdout.strip()


def resume_frozen_artifact(run_dir: Path) -> Path:
    artifact_dir = run_dir / "artifact"
    try:
        entries = list(artifact_dir.iterdir())
    except OSError as exc:
        raise _unavailable(
            f"frozen artifact directory is unavailable: {artifact_dir}: {exc}"
        ) from exc
    if len(entries) != 1:
        raise _unavailable(
            f"frozen artifact directory must contain exactly one entry; found {len(entries)}"
        )
    frozen = entries[0]
    try:
        mode = frozen.lstat().st_mode
    except OSError as exc:
        raise _unavailable(f"frozen artifact is unavailable: {frozen}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise _unavailable(f"frozen artifact must be one non-symlink regular file: {frozen}")
    return frozen


def _validate_commit(value: str, field: str = "commit") -> None:
    if COMMIT_RE.fullmatch(value) is None:
        raise UsageError(f"cannot resume: saved snapshot {field} must be 40 hexadecimal characters")


def verify_commit(repo: Path, commit: str) -> None:
    _validate_commit(commit)
    _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")


def git_tree(repo: Path, commit: str) -> str:
    _validate_commit(commit)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    if COMMIT_RE.fullmatch(tree) is None:
        raise _unavailable("git returned an invalid tree object name")
    return tree


def _repository_artifact(repo: Path, artifact: Path) -> tuple[Path | None, Path]:
    try:
        resolved_repo = repo.resolve(strict=True)
        resolved_artifact = artifact.resolve(strict=True)
    except OSError as exc:
        raise UsageError(
            f"cannot create snapshot: repository artifact is unavailable: {exc}"
        ) from exc
    try:
        relative = resolved_artifact.relative_to(resolved_repo)
    except ValueError:
        return None, resolved_artifact
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise UsageError("cannot create snapshot: repository artifact path is unsafe")
    return relative, resolved_artifact


def _verify_source_target(artifact: Path, expected: Path) -> None:
    try:
        current = artifact.resolve(strict=True)
    except OSError as exc:
        raise UsageError(
            f"cannot create snapshot: repository artifact became unavailable: {exc}"
        ) from exc
    if current != expected:
        raise UsageError(
            "cannot create snapshot: repository artifact target changed while "
            "the snapshot was captured"
        )


def _commit_blob(repo: Path, commit: str, relative: Path) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{relative.as_posix()}"],
            capture_output=True,
        )
    except OSError as exc:
        raise UsageError(
            f"cannot create snapshot: cannot read captured artifact blob: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "blob is missing"
        raise UsageError(
            f"cannot create snapshot: captured commit artifact is unavailable: {detail}"
        )
    return result.stdout


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
        # Preserve the actual hostile value so _from_dict reports its field
        # and type instead of laundering it through str().
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


@dataclass(frozen=True)
class SnapshotIdentity:
    repo_root: Path | None
    commit: str | None
    tree: str | None
    artifact_path: str
    artifact_hash: str
    predecessor: str | None = None

    @classmethod
    def create(
        cls,
        repo_root: Path | None,
        artifact: Path,
        digest: str,
        *,
        predecessor: str | None = None,
        source_artifact: Path | None = None,
    ) -> SnapshotIdentity:
        if repo_root is not None and not isinstance(repo_root, Path):
            raise UsageError("snapshot repo_root must be a Path or null")
        if not isinstance(artifact, Path):
            raise UsageError("snapshot artifact must be a Path")
        if not isinstance(digest, str):
            raise UsageError("snapshot artifact hash must be a string")
        if HASH_RE.fullmatch(digest) is None:
            raise UsageError("snapshot artifact hash must be sha256:<64 lowercase hex digits>")
        if predecessor is not None and (
            not isinstance(predecessor, str)
            or (COMMIT_RE.fullmatch(predecessor) is None and HASH_RE.fullmatch(predecessor) is None)
        ):
            raise UsageError("snapshot predecessor must be a commit or artifact hash")
        if source_artifact is not None and not isinstance(source_artifact, Path):
            raise UsageError("snapshot source_artifact must be a Path or null")
        binding = (
            _repository_artifact(repo_root, source_artifact)
            if repo_root is not None and source_artifact is not None
            else None
        )
        relative = binding[0] if binding is not None else None
        source_target = binding[1] if binding is not None else None
        if repo_root is not None and source_artifact is not None and relative is None:
            repo_root = None
        try:
            actual_digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError as exc:
            raise UsageError(
                f"cannot create snapshot: artifact is unreadable: {artifact}: {exc}"
            ) from exc
        if actual_digest != digest:
            raise UsageError("snapshot artifact hash does not match the artifact's exact bytes")
        captured_repo = repo_root
        commit = isolation.snapshot_commit(captured_repo) if captured_repo is not None else None
        if commit is not None:
            _validate_commit(commit)
            if relative is not None:
                if captured_repo is None or source_artifact is None or source_target is None:
                    raise UsageError(
                        "cannot create snapshot: repository artifact binding is incomplete"
                    )
                _verify_source_target(source_artifact, source_target)
                blob_digest = (
                    "sha256:"
                    + hashlib.sha256(_commit_blob(captured_repo, commit, relative)).hexdigest()
                )
                if blob_digest != digest:
                    raise UsageError(
                        "cannot create snapshot: captured commit artifact does not match "
                        "the frozen artifact bytes"
                    )
        tree = (
            git_tree(captured_repo, commit)
            if captured_repo is not None and commit is not None
            else None
        )
        return cls(captured_repo, commit, tree, str(artifact), digest, predecessor)

    @classmethod
    def _from_dict(cls, raw: object) -> SnapshotIdentity:
        raw = _string_mapping(raw, "snapshot")
        repo_text = _optional_string(raw, "repo_root")
        commit = _optional_string(raw, "commit")
        tree = _optional_string(raw, "tree")
        artifact_path = _required_string(raw, "artifact_path")
        artifact_hash = _required_string(raw, "artifact_hash")
        predecessor = _optional_string(raw, "predecessor")
        if (repo_text is None) != (commit is None):
            raise UsageError(
                "cannot resume: saved snapshot repo_root and commit must be present together"
            )
        if repo_text is None and tree is not None:
            raise UsageError("cannot resume: saved snapshot tree requires repo_root and commit")
        if commit is not None:
            _validate_commit(commit)
        if tree is not None:
            _validate_commit(tree, "tree")
        if predecessor is not None and (
            COMMIT_RE.fullmatch(predecessor) is None and HASH_RE.fullmatch(predecessor) is None
        ):
            raise UsageError(
                "cannot resume: saved snapshot predecessor must be a commit or artifact hash"
            )
        if HASH_RE.fullmatch(artifact_hash) is None:
            raise UsageError(
                "cannot resume: saved snapshot artifact_hash must be "
                "sha256:<64 lowercase hex digits>"
            )
        return cls(
            Path(repo_text) if repo_text is not None else None,
            commit,
            tree,
            artifact_path,
            artifact_hash,
            predecessor,
        )

    @classmethod
    def from_meta(cls, meta: object) -> SnapshotIdentity:
        meta = _string_mapping(meta, "snapshot metadata")
        if "snapshot" not in meta:
            return cls._from_dict(_legacy_dict(meta))

        raw_value = meta["snapshot"]
        nested_error: UsageError | None = None
        nested: SnapshotIdentity | None = None
        try:
            raw = _string_mapping(raw_value, "snapshot")
        except UsageError as exc:
            raw = None
            nested_error = exc
        if raw is not None:
            try:
                if _SNAPSHOT_FIELDS.issubset(raw):
                    nested = cls._from_dict(raw)
                else:
                    _validate_present_snapshot_fields(raw)
            except UsageError as exc:
                nested_error = exc

        legacy_error: UsageError | None = None
        try:
            legacy = cls._from_dict(_legacy_dict(meta))
        except UsageError as exc:
            legacy = None
            legacy_error = exc
        if nested is not None:
            assert raw is not None
            _nested_legacy_conflict(raw, meta, complete=True)
            return nested
        if legacy is not None:
            if raw is not None and nested_error is None:
                _nested_legacy_conflict(raw, meta, complete=False)
            return legacy
        if _has_complete_legacy_shape(meta):
            assert legacy_error is not None
            raise legacy_error
        if nested_error is not None:
            raise nested_error from None
        assert raw is not None
        return cls._from_dict(raw)

    @classmethod
    def from_current_meta(cls, meta: object) -> SnapshotIdentity:
        """Read the authoritative current-schema identity without legacy fallback."""
        mapped = _string_mapping(meta, "snapshot metadata")
        if "snapshot" not in mapped:
            raise UsageError("cannot resume: saved snapshot field is required")
        raw = _string_mapping(mapped["snapshot"], "snapshot")
        current = cls._from_dict(raw)
        _nested_legacy_conflict(raw, mapped, complete=True)
        return current

    def _verify_repo_root(self) -> None:
        assert self.repo_root is not None
        try:
            if not self.repo_root.is_dir():
                raise _unavailable(f"saved snapshot repository is unavailable: {self.repo_root}")
            recorded = self.repo_root.resolve()
        except OSError as exc:
            raise _unavailable(
                f"saved snapshot repository is unavailable: {self.repo_root}: {exc}"
            ) from exc
        try:
            top = Path(_git(self.repo_root, "rev-parse", "--show-toplevel")).resolve()
        except OSError as exc:
            raise _unavailable(
                f"saved snapshot repository is unavailable: {self.repo_root}: {exc}"
            ) from exc
        if top != recorded:
            raise UsageError(
                "cannot resume: saved snapshot repository root does not match "
                f"the available repository: recorded {recorded}, actual {top}"
            )

    def verify(self, frozen: Path) -> SnapshotIdentity:
        try:
            actual_hash = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
        except OSError as exc:
            raise _unavailable(
                f"frozen artifact is missing or unreadable: {frozen}: {exc}"
            ) from exc
        if actual_hash != self.artifact_hash:
            raise UsageError("cannot resume: frozen artifact hash does not match saved snapshot")
        if self.repo_root is None:
            return self
        assert self.commit is not None
        # Re-check here so even manually constructed identities cannot pass a
        # ref-like or option-like string to Git.
        _validate_commit(self.commit)
        self._verify_repo_root()
        try:
            verify_commit(self.repo_root, self.commit)
        except UsageError as exc:
            detail = str(exc)
            if "missing" not in detail:
                detail = f"saved snapshot commit is missing; {detail}"
            raise UsageError(detail) from exc
        actual_tree = git_tree(self.repo_root, self.commit)
        if self.tree is not None and actual_tree != self.tree:
            raise UsageError("cannot resume: saved snapshot tree does not match commit")
        return dataclasses.replace(self, tree=actual_tree)

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_root": str(self.repo_root) if self.repo_root is not None else None,
            "commit": self.commit,
            "tree": self.tree,
            "artifact_path": self.artifact_path,
            "artifact_hash": self.artifact_hash,
            "predecessor": self.predecessor,
        }


def select_snapshot(
    repo_root: Path | None,
    frozen: Path,
    digest: str,
    resume_meta: Mapping[str, object] | None,
    *,
    source_artifact: Path | None = None,
) -> SnapshotIdentity:
    """Create exactly once for a fresh run; verify exactly once for resume."""
    if resume_meta is not None:
        return SnapshotIdentity.from_meta(resume_meta).verify(frozen)
    return SnapshotIdentity.create(repo_root, frozen, digest, source_artifact=source_artifact)


def history_from_meta(
    meta: Mapping[str, object], current: SnapshotIdentity
) -> list[SnapshotIdentity]:
    if "snapshot_history" not in meta:
        return [current]
    raw = meta["snapshot_history"]
    if not isinstance(raw, list) or not raw:
        raise UsageError("cannot resume: saved snapshot_history must be a non-empty list")
    history: list[SnapshotIdentity] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise UsageError(f"cannot resume: saved snapshot_history[{index}] must be an object")
        try:
            history.append(SnapshotIdentity._from_dict(entry))
        except UsageError as exc:
            raise UsageError(
                f"cannot resume: saved snapshot_history[{index}] is invalid: {exc}"
            ) from exc
    if history[-1].tree is None and dataclasses.replace(history[-1], tree=current.tree) == current:
        # A first migration may have written the nested/history shape before
        # tree verification was introduced. Complete the current entry in
        # memory; the caller atomically persists it after this validation.
        history[-1] = current
    _validate_history_chain(history, current)
    return history


def _identity_token(identity: SnapshotIdentity) -> str:
    return identity.commit or identity.artifact_hash


def _validate_history_chain(history: list[SnapshotIdentity], current: SnapshotIdentity) -> None:
    seen: set[str] = set()
    for index, identity in enumerate(history):
        token = _identity_token(identity)
        if token in seen:
            raise UsageError(
                f"cannot resume: saved snapshot_history contains duplicate identity {token}"
            )
        seen.add(token)
        expected = None if index == 0 else _identity_token(history[index - 1])
        if identity.predecessor != expected:
            raise UsageError(
                f"cannot resume: saved snapshot_history[{index}] predecessor does not "
                "link to the prior identity"
            )
    if history[-1] != current:
        raise UsageError(
            "cannot resume: saved snapshot_history must contain the current snapshot "
            "exactly once and final"
        )


def record_snapshot(
    meta: MutableMapping[str, object],
    current: SnapshotIdentity,
    history: Iterable[SnapshotIdentity],
) -> None:
    """Write canonical fields first, then the two v0.2 compatibility keys."""
    ordered = list(history)
    if not ordered:
        raise UsageError("cannot resume: saved snapshot_history must be a non-empty list")
    _validate_history_chain(ordered, current)
    meta["snapshot"] = current.to_dict()
    meta["snapshot_history"] = [identity.to_dict() for identity in ordered]
    meta["repo_root"] = str(current.repo_root) if current.repo_root is not None else None
    meta["snapshot_sha"] = current.commit

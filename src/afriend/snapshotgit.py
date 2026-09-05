"""Recover legacy artifact bindings from immutable Git objects."""

from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess

from .errors import UsageError

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_MAX_SYMLINKS = 64


def _unavailable(detail: str) -> UsageError:
    return UsageError(f"cannot resume: saved snapshot is unavailable: {detail}")


def legacy_invocation_path(repo: Path, raw: str) -> str:
    if "\0" in raw:
        raise UsageError("cannot resume: saved source artifact path contains NUL")
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(repo.absolute())
        except ValueError as exc:
            raise UsageError(
                "cannot resume: saved source artifact path is outside the recorded repository: "
                f"{raw!r}"
            ) from exc
        value = relative.as_posix()
    else:
        value = PurePosixPath(raw).as_posix()
    return _validate_path(value, f"saved source artifact path is unsafe: {raw!r}")


def _validate_path(value: str, detail: str) -> str:
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
        raise UsageError(f"cannot resume: {detail}")
    return value


def _tree_entry(repo: Path, commit: str, source_path: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "ls-tree",
                "-z",
                commit,
                "--",
                f":(literal){source_path}",
            ],
            capture_output=True,
        )
    except OSError as exc:
        raise _unavailable(f"cannot inspect saved commit artifact: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "tree lookup failed"
        raise _unavailable(f"saved commit artifact is unavailable: {detail}")
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise _unavailable(f"saved commit artifact is unavailable: {source_path!r}")
    header, encoded_path = records[0].split(b"\t", 1)
    try:
        recorded_path = encoded_path.decode("utf-8")
        mode, object_type, _object_id = header.decode("ascii").split(" ", 2)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _unavailable("saved commit tree contains an invalid artifact entry") from exc
    if recorded_path != source_path:
        raise _unavailable(f"saved commit artifact is unavailable: {source_path!r}")
    return mode, object_type


def _blob(repo: Path, commit: str, source_path: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{source_path}"],
            capture_output=True,
        )
    except OSError as exc:
        raise _unavailable(f"cannot read saved commit symlink: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "blob is missing"
        raise _unavailable(f"saved commit artifact is unavailable: {detail}")
    return result.stdout


def _symlink_target(repo: Path, commit: str, source_path: str) -> str:
    try:
        target = _blob(repo, commit, source_path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unavailable("saved commit symlink target is not valid UTF-8") from exc
    if not target or "\0" in target:
        raise _unavailable("saved commit symlink target is empty or contains NUL")
    return target


def _target_base(repo: Path, link_path: str, target: str) -> str:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        normalized = PurePosixPath(posixpath.normpath(target))
        repo_path = PurePosixPath(posixpath.normpath(repo.absolute().as_posix()))
        try:
            return normalized.relative_to(repo_path).as_posix()
        except ValueError as exc:
            raise _unavailable(
                f"saved commit symlink target is outside the recorded repository: {target!r}"
            ) from exc
    return posixpath.normpath((PurePosixPath(link_path).parent / target_path).as_posix())


def _resolved_target(repo: Path, link_path: str, target: str, suffix: tuple[str, ...]) -> str:
    base = _target_base(repo, link_path, target)
    candidate = posixpath.normpath(PurePosixPath(base, *suffix).as_posix())
    if candidate == ".." or candidate.startswith("../"):
        raise _unavailable(
            f"saved commit symlink target is outside the recorded repository: {target!r}"
        )
    return _validate_path(candidate, f"saved commit symlink target is unsafe: {target!r}")


def resolve_saved_source(repo: Path, commit: str, invocation_path: str) -> str:
    if _COMMIT_RE.fullmatch(commit) is None:
        raise UsageError("cannot resume: saved snapshot commit must be 40 hexadecimal characters")
    current = _validate_path(
        invocation_path,
        f"saved source artifact path is unsafe: {invocation_path!r}",
    )
    seen: set[str] = set()
    symlinks = 0
    while True:
        if current in seen:
            raise _unavailable("saved commit artifact contains a symlink cycle")
        seen.add(current)
        parts = PurePosixPath(current).parts
        for index in range(len(parts)):
            prefix = PurePosixPath(*parts[: index + 1]).as_posix()
            mode, object_type = _tree_entry(repo, commit, prefix)
            is_last = index == len(parts) - 1
            if mode == "120000" and object_type == "blob":
                if symlinks >= _MAX_SYMLINKS:
                    raise _unavailable("saved commit artifact exceeds the symlink depth limit")
                target = _symlink_target(repo, commit, prefix)
                current = _resolved_target(repo, prefix, target, parts[index + 1 :])
                symlinks += 1
                break
            if not is_last:
                if mode != "040000" or object_type != "tree":
                    raise _unavailable(
                        f"saved commit artifact component is not a directory: {prefix!r}"
                    )
                continue
            if mode in {"100644", "100755"} and object_type == "blob":
                return current
            raise _unavailable(
                f"saved commit artifact is not a regular file or symlink: {current!r}"
            )

"""Recover legacy artifact bindings from immutable Git objects."""

from pathlib import Path, PurePosixPath
import posixpath
import subprocess

from .errors import UsageError


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
            ["git", "-C", str(repo), "ls-tree", "-z", commit, "--", source_path],
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


def resolve_saved_source(repo: Path, commit: str, invocation_path: str) -> str:
    current = invocation_path
    seen: set[str] = set()
    for _depth in range(64):
        if current in seen:
            raise _unavailable("saved commit artifact contains a symlink cycle")
        seen.add(current)
        mode, object_type = _tree_entry(repo, commit, current)
        if mode in {"100644", "100755"} and object_type == "blob":
            return current
        if mode != "120000" or object_type != "blob":
            raise _unavailable(
                f"saved commit artifact is not a regular file or symlink: {current!r}"
            )
        target = _symlink_target(repo, commit, current)
        target_path = PurePosixPath(target)
        if target_path.is_absolute():
            target_path = PurePosixPath(posixpath.normpath(target))
            repo_path = PurePosixPath(repo.absolute().as_posix())
            try:
                candidate = target_path.relative_to(repo_path).as_posix()
            except ValueError as exc:
                raise _unavailable(
                    f"saved commit symlink target is outside the recorded repository: {target!r}"
                ) from exc
        else:
            candidate = posixpath.normpath((PurePosixPath(current).parent / target_path).as_posix())
        if candidate == ".." or candidate.startswith("../"):
            raise _unavailable(
                f"saved commit symlink target is outside the recorded repository: {target!r}"
            )
        current = _validate_path(candidate, f"saved commit symlink target is unsafe: {target!r}")
    raise _unavailable("saved commit artifact exceeds the symlink depth limit")

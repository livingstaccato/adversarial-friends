"""Bounded, no-follow JSON loading for operator-controlled files."""

import json
import os
from pathlib import Path
import stat
from typing import Any

from .errors import UsageError
from .outcomes import json_node_count

# Matches the per-stream friend-output ceiling. A valid response can approach
# that size, while a sparse or hostile file cannot make json.loads allocate
# beyond it.
MAX_JSON_FILE_BYTES = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024


def read_bounded_bytes(path: Path, *, label: str, max_bytes: int = MAX_JSON_FILE_BYTES) -> bytes:
    target = Path(path)
    try:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise UsageError(f"{label} {target} must be a regular file, not a symlink")
        if info.st_size > max_bytes:
            raise UsageError(f"{label} {target} exceeds the {max_bytes}-byte limit")
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except UsageError:
        raise
    except OSError as exc:
        raise UsageError(f"cannot read {label} {target}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise UsageError(f"{label} {target} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UsageError(f"{label} {target} exceeds the {max_bytes}-byte limit")
        finished = os.fstat(descriptor)
        if (finished.st_size, finished.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise UsageError(f"{label} {target} changed while it was read")
        return b"".join(chunks)
    except OSError as exc:
        raise UsageError(f"cannot read {label} {target}: {exc}") from exc
    finally:
        os.close(descriptor)


def load_json_object(
    path: Path, *, label: str, max_bytes: int = MAX_JSON_FILE_BYTES
) -> dict[str, Any]:
    payload = read_bounded_bytes(path, label=label, max_bytes=max_bytes)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{label} {path} must be valid UTF-8") from exc
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise UsageError(f"{label} {path} is not valid JSON within bounds: {exc}") from exc
    if type(value) is not dict:
        raise UsageError(f"{label} {path} must contain a JSON object")
    try:
        json_node_count(value, label)
    except (RecursionError, TypeError, ValueError) as exc:
        raise UsageError(f"{label} {path} exceeds the metadata bound: {exc}") from exc
    return value

"""Run directory layout.

The run directory lives outside the worktree. Putting it inside the repository
would let `codex review --uncommitted` -- "staged, unstaged, and untracked" --
review the tool's own scratch files as part of the diff under review.
"""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import IO, Any

from .errors import UsageError
from .ids import validate_friend_name
from .ledger import Ledger
from .trust import contain_path


def default_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(state) / "adversarial-friends" / "runs"


class RunLocked(UsageError):
    """Another process is writing this run directory."""


class RunStore:
    # The exclusive lock this process holds on its run directory, kept for
    # the process's lifetime (see `lock`).
    _lock_handle: "IO[str] | None" = None

    def __init__(self, root: Path, run_id: str, resume: bool = False) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.run_dir = self.root / run_id
        if resume:
            # A resumed run deliberately reopens a directory that already
            # holds a ledger, an artifact copy, and a round-1 REQUEST -- the
            # refusal below exists to stop two DIFFERENT runs sharing a
            # directory, which is the opposite case.
            if not self.run_dir.is_dir():
                raise UsageError(f"cannot resume: no such run directory: {self.run_dir}")
            self.ledger = Ledger(self.run_dir / "claims.jsonl")
            return
        if self.run_dir.exists():
            # A prior run (or a caller-supplied --out that collides with one)
            # already occupies this path. Silently reusing it via
            # mkdir(..., exist_ok=True) would append this run's claims into
            # a ledger that may already hold another run's records, and
            # round_dir()/friend_paths() would happily overwrite that run's
            # friend output too. Refuse instead of mixing two runs together.
            raise UsageError(f"run directory already exists: {self.run_dir}")
        try:
            self.run_dir.mkdir(parents=True)
        except OSError as exc:
            # E.g. an ancestor path component (commonly --out itself)
            # already exists as a plain file rather than a directory.
            # mkdir() raises a raw NotADirectoryError/OSError in that case;
            # surfaced here as a clean, actionable UsageError instead of an
            # unhandled traceback out of cmd_run.
            raise UsageError(f"cannot create run directory {self.run_dir}: {exc}") from exc
        self.ledger = Ledger(self.run_dir / "claims.jsonl")

    def lock(self) -> None:
        """Take the run directory's exclusive lock for the rest of the process.

        A fresh run is protected by the "already exists" refusal below, but
        a resume deliberately reopens a directory that has one -- so two
        CI workers that both notice the same RESPONSE.json could reconstruct
        the same state, dispatch the same round twice, append duplicate
        aliases and verdicts to one ledger, and overwrite each other's
        round files and run.json. The last writer's metadata then describes
        one of the two executions while the ledger holds both.

        `flock` is advisory and process-scoped, released by the OS when this
        process exits however it exits -- including a kill that gives no
        `finally` a chance to run. The handle is kept on the instance for
        exactly that lifetime.
        """
        self._lock_handle = (self.run_dir / ".lock").open("w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise RunLocked(
                f"run directory is locked by another process: {self.run_dir}. "
                "Two runs writing one directory duplicate ledger records and "
                "overwrite each other's output; wait for the other to finish."
            ) from exc
        self._lock_handle.write(f"{os.getpid()}\n")
        self._lock_handle.flush()

    def round_dir(self, round_no: int) -> Path:
        path = self.run_dir / f"round-{round_no}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def friend_prompt_path(self, round_no: int, friend_name: str) -> Path:
        """Path for the exact prompt text a friend received, written next
        to its .raw/.meta so a human can see what it was actually asked."""
        validate_friend_name(friend_name)
        base = self.round_dir(round_no)
        return contain_path(self.run_dir, base / f"{friend_name}.prompt")

    def friend_paths(self, round_no: int, friend_name: str) -> tuple[Path, Path, Path]:
        validate_friend_name(friend_name)
        base = self.round_dir(round_no)
        paths = tuple(
            contain_path(self.run_dir, base / f"{friend_name}{suffix}")
            for suffix in (".raw", ".json", ".meta")
        )
        return paths  # type: ignore[return-value]

    def friend_err_path(self, round_no: int, friend_name: str) -> Path:
        """Path for a friend's captured stderr, written next to its
        .raw/.json/.meta. A separate method (not a 4th element of
        friend_paths' tuple) so every existing `raw, parsed, meta =
        store.friend_paths(...)` call site keeps unpacking exactly 3 values."""
        validate_friend_name(friend_name)
        base = self.round_dir(round_no)
        return contain_path(self.run_dir, base / f"{friend_name}.err")

    def artifact_copy(self, source: Path) -> tuple[Path, str]:
        target_dir = self.run_dir / "artifact"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(source).name
        shutil.copy2(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return target, f"sha256:{digest}"

    def _write_atomic(self, path: Path, text: str) -> Path:
        """Write via a temporary file in the same directory, then rename.

        `write_text` truncates first and writes second, so a process that
        dies in between leaves the file existing and invalid. For `run.json`
        that is unrecoverable: `--resume` reads it to reconstruct the run's
        configuration, so a half-written one turns a crash into permanent
        loss of a run that may represent an hour of metered CLI time. This
        file is rewritten at the end of every iteration of a `loop`, so the
        window is not a single moment at the end -- it recurs.

        `rename` within one directory is atomic on POSIX and on Windows via
        `os.replace`, which `Path.replace` uses. A reader therefore sees the
        old complete file or the new complete file, never a partial one.

        The directory is fsync'd after the rename so the rename itself
        survives a power loss, not merely the bytes it points at. Both
        fsyncs are best-effort: a filesystem that does not support them
        (some network mounts) must not turn a completed run into a crash,
        and the write has already succeeded by that point.
        """
        tmp = path.with_name(f".{path.name}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        tmp.replace(path)
        with contextlib.suppress(OSError):
            fd = os.open(self.run_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return path

    def write_run_json(self, meta: dict[str, Any]) -> Path:
        return self._write_atomic(
            self.run_dir / "run.json", json.dumps(meta, indent=2, sort_keys=True)
        )

    def write_report(self, text: str) -> Path:
        return self._write_atomic(self.run_dir / "report.md", text)

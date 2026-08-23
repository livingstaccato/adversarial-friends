"""Run directory layout.

The run directory lives outside the worktree. Putting it inside the repository
would let `codex review --uncommitted` -- "staged, unstaged, and untracked" --
review the tool's own scratch files as part of the diff under review.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

from .errors import UsageError
from .ids import validate_friend_name
from .ledger import Ledger
from .trust import contain_path


def default_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(state) / "adversarial-friends" / "runs"


class RunStore:
    def __init__(self, root: Path, run_id: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.run_dir = self.root / run_id
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

    def round_dir(self, round_no: int) -> Path:
        path = self.run_dir / f"round-{round_no}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def friend_paths(self, round_no: int, friend_name: str) -> tuple[Path, Path, Path]:
        validate_friend_name(friend_name)
        base = self.round_dir(round_no)
        paths = tuple(contain_path(self.run_dir, base / f"{friend_name}{suffix}")
                      for suffix in (".raw", ".json", ".meta"))
        return paths  # type: ignore[return-value]

    def artifact_copy(self, source: Path) -> tuple[Path, str]:
        target_dir = self.run_dir / "artifact"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(source).name
        shutil.copy2(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return target, f"sha256:{digest}"

    def write_run_json(self, meta: dict) -> Path:
        path = self.run_dir / "run.json"
        path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def write_report(self, text: str) -> Path:
        path = self.run_dir / "report.md"
        path.write_text(text, encoding="utf-8")
        return path

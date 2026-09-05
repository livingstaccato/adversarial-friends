"""Which directory counts as a CLI's installation root (§12.2).

`policy_for` grants the directory above `bin/` so a CLI can read its own
libraries -- opencode keeps a 61MB `node_modules/` there. The rule was
"the parent of any directory named bin", and a crossexam of sandbox.py
pointed out what that grants when the binary is a real file rather than a
symlink: `~/bin/tool` resolves to `~/bin`, whose parent is the home
directory the sandbox exists to remove.
"""

from pathlib import Path

from afriend import sandbox


def _tool(directory: Path, name: str = "tool") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_a_general_purpose_bin_does_not_grant_its_parent(tmp_path, monkeypatch):
    """`~/bin/tool` and `~/.local/bin/tool` are the normal shape for a
    curl-installer or a single-file binary. Granting their parent hands a
    confined friend the whole home directory -- and the report still says it
    was confined."""
    home = tmp_path / "home"
    tool = _tool(home / "bin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _binary: str(tool))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    policy = sandbox.policy_for(tmp_path / "work", "tool", (), ())

    assert home not in policy.read_paths, [str(p) for p in policy.read_paths]
    assert (home / "bin") in policy.read_paths


def test_a_real_install_root_beside_bin_is_still_granted(tmp_path, monkeypatch):
    """The case the rule exists for: a package-manager layout where the
    CLI's libraries are its siblings' siblings, which is why granting only
    `bin/` left opencode unable to read itself."""
    root = tmp_path / "home" / ".opencode"
    tool = _tool(root / "bin", "opencode")
    (root / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _binary: str(tool))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    policy = sandbox.policy_for(tmp_path / "work", "opencode", (), ())

    assert root in policy.read_paths, [str(p) for p in policy.read_paths]


def test_the_home_directory_is_never_an_install_root(tmp_path, monkeypatch):
    """Even a home directory that happens to contain a `lib/` is not one:
    the sandbox's whole purpose is removing the rest of the home directory,
    so no heuristic may hand it back."""
    home = tmp_path / "home"
    tool = _tool(home / "bin")
    (home / "lib").mkdir(parents=True)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _binary: str(tool))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    policy = sandbox.policy_for(tmp_path / "work", "tool", (), ())

    assert home not in policy.read_paths, [str(p) for p in policy.read_paths]

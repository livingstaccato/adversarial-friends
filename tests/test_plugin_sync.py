import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_plugin_sync.py"
    spec = importlib.util.spec_from_file_location("plugin_sync", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_tree_rejects_canonical_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "inside.md").write_text("inside")
    try:
        (source / "link.md").symlink_to(source / "inside.md")
    except OSError:
        pytest.skip("symlinks unsupported")
    with pytest.raises(ValueError, match="symlink"):
        _module().project_tree(source, Path())


def test_copy_rejects_literal_skills_symlink_without_touching_victim(tmp_path, monkeypatch):
    module = _module()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("safe")
    skills = plugin / "skills"
    try:
        skills.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unsupported")
    monkeypatch.setattr(module, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(module, "SKILLS", skills)
    assert module.copy_expected({Path("afriend/SKILL.md"): b"new"}) == 2
    assert (victim / "keep").read_text() == "safe"


def test_copy_rolls_back_when_staged_replace_fails(tmp_path, monkeypatch):
    module = _module()
    plugin = tmp_path / "plugin"
    skills = plugin / "skills"
    (skills / "afriend").mkdir(parents=True)
    old = skills / "afriend" / "SKILL.md"
    old.write_text("old")
    monkeypatch.setattr(module, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(module, "SKILLS", skills)
    original_replace = Path.replace

    def fail_stage_replace(self, target):
        if self.name == "skills" and self.parent.name.startswith(".skills-stage-"):
            raise OSError("injected stage failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_stage_replace)
    with pytest.raises(OSError, match="injected"):
        module.copy_expected({Path("afriend/SKILL.md"): b"new"})
    assert old.read_text() == "old"
    assert not list(plugin.glob(".skills-stage-*"))
    assert not list(plugin.glob(".skills-backup-*"))


def test_verification_rejects_nested_plugin_symlink_without_following_it(tmp_path, monkeypatch):
    module = _module()
    plugin = tmp_path / "plugin"
    skills = plugin / "skills"
    target = skills / "afriend" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("expected")
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    try:
        (skills / "afriend" / "outside.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported")
    monkeypatch.setattr(module, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(module, "SKILLS", skills)
    monkeypatch.setattr(
        module, "expected_plugin_files", lambda: {Path("afriend/SKILL.md"): b"expected"}
    )
    assert module.main([]) == 2
    assert outside.read_text() == "untouched"

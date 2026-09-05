import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _module():
    path = REPO / "scripts" / "check_version_sync.py"
    spec = importlib.util.spec_from_file_location("version_sync", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_manifest(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version}))


def _write_compatibility_project(path: Path, name: str, version: str, dependency: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                f'version = "{version}"',
                f'dependencies = ["{dependency}"]',
                "",
            ]
        )
    )


def test_codex_manifest_allows_only_a_codex_cachebuster(monkeypatch, tmp_path):
    module = _module()
    version = tmp_path / "VERSION"
    version.write_text("0.5.1")
    marketplace = tmp_path / "marketplace.json"
    claude = tmp_path / "claude.json"
    codex = tmp_path / "codex.json"
    _write_manifest(marketplace, "0.5.1")
    _write_manifest(claude, "0.5.1")
    _write_manifest(codex, "0.5.1+codex.local-20260904-205235")
    monkeypatch.setattr(module, "VERSION_FILE", version)
    monkeypatch.setattr(module, "MANIFESTS", [marketplace, claude, codex])
    monkeypatch.setattr(module, "CODEX_MANIFEST", codex, raising=False)
    monkeypatch.setattr(module, "COMPATIBILITY_PROJECTS", (), raising=False)
    monkeypatch.setattr(module, "cli_version", lambda: "0.5.1", raising=False)

    assert module.main() == 0
    _write_manifest(codex, "0.5.1+local-20260904-205235")
    assert module.main() == 1


def test_non_codex_manifest_rejects_a_codex_cachebuster(monkeypatch, tmp_path):
    module = _module()
    version = tmp_path / "VERSION"
    version.write_text("0.5.1")
    marketplace = tmp_path / "marketplace.json"
    claude = tmp_path / "claude.json"
    codex = tmp_path / "codex.json"
    _write_manifest(marketplace, "0.5.1+codex.local-20260904-205235")
    _write_manifest(claude, "0.5.1")
    _write_manifest(codex, "0.5.1")
    monkeypatch.setattr(module, "VERSION_FILE", version)
    monkeypatch.setattr(module, "MANIFESTS", [marketplace, claude, codex])
    monkeypatch.setattr(module, "CODEX_MANIFEST", codex, raising=False)
    monkeypatch.setattr(module, "COMPATIBILITY_PROJECTS", (), raising=False)
    monkeypatch.setattr(module, "cli_version", lambda: "0.5.1", raising=False)

    assert module.main() == 1


def test_compatibility_projects_must_match_the_canonical_version(monkeypatch, tmp_path):
    module = _module()
    version = tmp_path / "VERSION"
    version.write_text("0.6.1")
    manifests = [tmp_path / f"manifest-{index}.json" for index in range(3)]
    for manifest in manifests:
        _write_manifest(manifest, "0.6.1")
    old_name = tmp_path / "adversarial-friends.toml"
    typo_name = tmp_path / "afriends.toml"
    _write_compatibility_project(old_name, "adversarial-friends", "0.6.1", "afriend==0.6.1")
    _write_compatibility_project(typo_name, "afriends", "0.6.1", "afriend==0.6.1")
    monkeypatch.setattr(module, "VERSION_FILE", version)
    monkeypatch.setattr(module, "MANIFESTS", manifests)
    monkeypatch.setattr(module, "CODEX_MANIFEST", manifests[-1], raising=False)
    monkeypatch.setattr(
        module,
        "COMPATIBILITY_PROJECTS",
        [("adversarial-friends", old_name), ("afriends", typo_name)],
        raising=False,
    )
    monkeypatch.setattr(module, "cli_version", lambda: "0.6.1", raising=False)

    assert module.main() == 0
    _write_compatibility_project(typo_name, "afriends", "0.6.1", "afriend==0.6.0")
    assert module.main() == 1

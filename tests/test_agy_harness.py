"""Hermetic contract tests for Antigravity's controlled reviewer agent."""

import hashlib
import json
import os
from pathlib import Path
import sys

from e2e_helpers import _safe_path_dir, run_af

from afriend.adapters import load_adapters
from afriend.paths import ADAPTER_DIR

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "afriend" / "assets"
AGENT = ASSETS / "harnesses" / "agy" / "afriend-reviewer.md"
MIRROR_AGENT = (
    REPO
    / "plugins"
    / "afriend"
    / "skills"
    / "afriend"
    / "harnesses"
    / "agy"
    / "afriend-reviewer.md"
)
TARGET = ".agents/agents/afriend-reviewer/agent.md"


def _run_dir(tmp_path: Path) -> Path:
    return next((tmp_path / "runs").iterdir())


def _frontmatter(text: str) -> list[str]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return lines[1:end]


def test_packaged_agy_agent_uses_supported_restrictive_frontmatter():
    text = AGENT.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)

    assert "name: afriend-reviewer" in frontmatter
    assert "tools: []" in frontmatter
    assert "subagent: false" in frontmatter
    assert "disable-model-invocation: true" in frontmatter
    assert "inheritCustomizations: false" in frontmatter
    assert "structured adversarial review" in text.lower()
    assert "return only" in text.lower()
    assert "tools are denied" not in text.lower()


def test_agy_adapter_pins_the_controlled_agent_content():
    payload = AGENT.read_bytes()
    expected_digest = hashlib.sha256(payload).hexdigest()
    adapter = load_adapters(ADAPTER_DIR)["agy"]

    assert len(adapter.workspace_assets) == 1
    declared = adapter.workspace_assets[0]
    assert declared.source == "harnesses/agy/afriend-reviewer.md"
    assert declared.target == TARGET
    assert declared.sha256 == expected_digest
    assert len(declared.sha256) == 64
    assert declared.sha256 == declared.sha256.lower()


def test_canonical_agy_agent_and_plugin_mirror_are_byte_identical():
    assert MIRROR_AGENT.read_bytes() == AGENT.read_bytes()


def _write_fake_agy(binary: Path, contact: Path, expected_payload: bytes) -> None:
    expected_digest = hashlib.sha256(expected_payload).hexdigest()
    script = f"""#!{sys.executable}
import hashlib
import json
from pathlib import Path
import sys

target = Path({TARGET!r})
payload = target.read_bytes()
argv = sys.argv[1:]
assert hashlib.sha256(payload).hexdigest() == {expected_digest!r}
assert argv.count('--mode') == 1
assert argv[argv.index('--mode') + 1] == 'plan'
assert argv[argv.index('--agent') + 1] == 'afriend-reviewer'
assert '--disable-slash-commands' in argv
assert '--sandbox' in argv
assert argv.index('--agent') < argv.index('--print')
assert argv.index('--disable-slash-commands') < argv.index('--print')
assert argv.index('--mode') < argv.index('--print')
assert argv.index('--sandbox') < argv.index('--print')
contact = Path({str(contact)!r})
contact.write_text(json.dumps({{
    'argv': argv,
    'digest': hashlib.sha256(payload).hexdigest(),
    'content': payload.decode(),
    'target': str(target),
}}))
finding = {{
    'severity': 'low',
    'claim': 'hermetic agy harness probe',
    'location': None,
    'evidence': 'controlled reviewer was staged before fake CLI contact',
    'failure_scenario': 'n/a',
    'suggested_fix': 'n/a',
}}
print(json.dumps({{'response': json.dumps({{'findings': [finding]}})}}))
"""
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)


def test_agy_is_blocked_by_default_then_scoped_grant_stages_and_audits_agent(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n", encoding="utf-8")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    contact = tmp_path / "agy-contact.json"
    payload = AGENT.read_bytes()
    _write_fake_agy(binary_dir / "agy", contact, payload)
    path = f"{binary_dir}{os.pathsep}{_safe_path_dir()}"

    blocked = run_af(
        tmp_path,
        artifact,
        "--friend",
        "agy:ops",
        env_extra={"PATH": path},
    )
    assert blocked.returncode == 2
    assert "agy cannot deny external tools" in blocked.stderr
    assert "--allow-external-tools" in blocked.stderr
    assert not contact.exists()
    assert not (tmp_path / "runs").exists()

    allowed = run_af(
        tmp_path,
        artifact,
        "--friend",
        "agy:ops",
        "--allow-external-tools=agy",
        env_extra={"PATH": path},
    )
    assert allowed.returncode == 0, allowed.stderr

    observed = json.loads(contact.read_text(encoding="utf-8"))
    assert observed["target"] == TARGET
    assert observed["digest"] == hashlib.sha256(payload).hexdigest()
    assert observed["content"].encode() == payload
    assert observed["argv"][-2] == "--print"

    run_dir = _run_dir(tmp_path)
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    friend = run_meta["friends"][0]
    assert friend["external_tools"] == "explicitly-allowed"
    assert friend["workspace_assets"] == [
        {
            "source": "harnesses/agy/afriend-reviewer.md",
            "target": TARGET,
            "expected_sha256": hashlib.sha256(payload).hexdigest(),
            "observed_sha256": hashlib.sha256(payload).hexdigest(),
            "status": "staged",
        }
    ]

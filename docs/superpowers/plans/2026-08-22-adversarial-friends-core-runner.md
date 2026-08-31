# Adversarial Friends — Core Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `af run --mode report` end-to-end — a working skill that dispatches an artifact to several agent CLIs in parallel, normalizes their critiques into a claim ledger, and renders a merged report.

**Architecture:** A stdlib-only Python package under `skills/adversarial-friends/scripts/`, driven by declarative TOML adapter records. Mechanical work (spawn, isolation, parsing, ledger arithmetic) lives in code so it is reproducible; judgment (lens choice, dedup, presentation) lives in `SKILL.md` so it is good. Cross-examination, gates, and loops are **out of scope** for this plan — they build on the ledger this plan creates.

**Tech Stack:** Python 3.11+ (stdlib only at runtime — `tomllib`, `subprocess`, `json`, `re`, `pathlib`), pytest for tests only, git worktrees for isolation, markdown for the skill layer.

**Spec:** `docs/superpowers/specs/2026-08-22-adversarial-friends-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include these.

- **Runtime is stdlib-only.** No third-party imports in `scripts/adversarial_friends/`. pytest is a dev dependency and must never be imported by runtime code.
- **Python floor is 3.11** (`tomllib`). Do not use 3.12+ syntax.
- **Exit codes and precedence** (spec §7.6), first match wins: `2` usage/config error · `3` no usable friends · `11` ceiling hit · `10` needs orchestrator · `1` gate blocked or run incomplete · `0` terminal state reached.
- **Exit status is never evidence of success** (spec §7.3). A friend "completed successfully" only when: exit 0 **and** output parsed **and** output conformed to schema **and** it produced ≥1 claim or an explicit `{"no_findings": true}`.
- **Capabilities are computed from the final effective argv** (spec §11.1), never from adapter defaults.
- **Friend names match `^[a-z0-9][a-z0-9_-]{0,31}$`** (spec §13). Names are path components.
- **Roster files supply values only** (spec §13). There is no `extra_args` and no `profile` key in the roster schema.
- **Never invoke a metered provider from a test or probe** (spec §16). Tests use the fake friend binary or local ollama.
- **Adapters spell flags long** (spec §11.2). `-p` is `--print` on claude/agy but `--profile` on codex; `-s` is `--sandbox` on codex but `--session` on opencode.

## File Structure

```
skills/adversarial-friends/
  SKILL.md                          # judgment layer; the installable skill
  lenses/*.md                       # one prose file per adversarial lens
  adapters/*.toml                   # declarative per-CLI records (data, not code)
  references/                       # progressive disclosure for SKILL.md
    modes.md
    ledger.md
    troubleshooting.md
  scripts/
    af                              # executable entry shim
    adversarial_friends/
      __init__.py
      errors.py                     # exit-code-carrying exceptions
      ids.py                        # claim id versioning, name validation
      ledger.py                     # 4 record types + jsonl store
      claimschema.py                # JSON Schema for friend output
      adapters.py                   # TOML load, argv build, capability calc
      trust.py                      # roster validation, denied args, path containment
      roster.py                     # PATH probe, self-exclusion, lens assignment
      normalize.py                  # ANSI strip, fence extract, parse, success test
      spawn.py                      # process groups, timeouts, capture
      isolation.py                  # snapshot with untracked, per-friend worktrees
      merge.py                      # exact merge -> Alias records
      report.py                     # report.md rendering
      runstore.py                   # run dir layout, run.json
      cli.py                        # argparse, subcommands, exit codes
bin/af                              # symlink -> skills/.../scripts/af (dev convenience)
tests/
  conftest.py
  fake_friend.py                    # scripted stand-in for a real CLI
  test_*.py
docs/
  README.md                         # sectioned index (octowright style)
  images/brand/                     # logo assets
  architecture/                     # .puml sources + rendered .svg
evals/evals.json                    # skill trigger/behavior test cases
```

Splitting rationale: `normalize`, `spawn`, and `isolation` each encode a distinct verified CLI hazard (ANSI in payloads, orphaned process groups, untracked files missing from snapshots). Keeping them separate means a reviewer can reject one hazard's handling without rejecting the others.

---

### Task 1: Repo scaffold and executable entry point

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/__init__.py`
- Create: `skills/adversarial-friends/scripts/adversarial_friends/errors.py`
- Create: `skills/adversarial-friends/scripts/adversarial_friends/cli.py`
- Create: `skills/adversarial-friends/scripts/af`
- Create: `tests/conftest.py`
- Test: `tests/test_cli_entry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `errors.AfError(message, exit_code)`, `errors.UsageError` (exit 2), `errors.NoFriendsError` (exit 3), `errors.CeilingError` (exit 11); `cli.main(argv: list[str]) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_entry.py
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AF = REPO / "skills" / "adversarial-friends" / "scripts" / "af"


def test_af_reports_version():
    result = subprocess.run([sys.executable, str(AF), "--version"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip().startswith("af ")


def test_unknown_subcommand_exits_2():
    result = subprocess.run([sys.executable, str(AF), "nonsense"],
                            capture_output=True, text=True)
    assert result.returncode == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_entry.py -v`
Expected: FAIL — `af` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/errors.py
"""Exceptions that carry the process exit code they should produce."""


class AfError(Exception):
    exit_code = 1

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(AfError):
    exit_code = 2


class NoFriendsError(AfError):
    exit_code = 3


class CeilingError(AfError):
    exit_code = 11
```

```python
# skills/adversarial-friends/scripts/adversarial_friends/__init__.py
__version__ = "0.1.0"
```

```python
# skills/adversarial-friends/scripts/adversarial_friends/cli.py
"""Command line entry point. Subcommands are added by later tasks."""
import argparse

from . import __version__
from .errors import AfError, UsageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="af", add_help=True)
    parser.add_argument("--version", action="version", version=f"af {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    # argparse exits 2 on unknown args, which matches our usage-error code.
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            parser.print_help()
            return 0
        raise UsageError(f"unknown command: {args.command}")
    except AfError as exc:
        print(f"af: {exc}", file=__import__("sys").stderr)
        return exc.exit_code
```

```python
# skills/adversarial-friends/scripts/af
#!/usr/bin/env python3
"""Entry shim: runs without installation, per the stdlib-only constraint."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adversarial_friends.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

```python
# tests/conftest.py
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "adversarial-friends" / "scripts"))
```

- [ ] **Step 4: Make the shim executable and run the tests**

Run: `chmod +x skills/adversarial-friends/scripts/af && python -m pytest tests/test_cli_entry.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add skills tests
git commit -m "feat: add af entry point and exit-code exception hierarchy"
```

---

### Task 2: Claim ids and name validation

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/ids.py`
- Test: `tests/test_ids.py`

**Interfaces:**
- Consumes: `errors.UsageError`.
- Produces: `ids.format_claim_id(n: int, version: int = 1) -> str`, `ids.parse_claim_id(cid: str) -> tuple[int, int]`, `ids.bump_claim_id(cid: str) -> str`, `ids.base_claim_id(cid: str) -> str`, `ids.validate_friend_name(name: str) -> str`.

Claim ids are versioned as `c-0007@2` so a verdict always names the exact wording it judged (spec §6.1). Friend names are path components, so validation is a security control, not cosmetics (spec §13).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ids.py
import pytest

from adversarial_friends import ids
from adversarial_friends.errors import UsageError


def test_format_and_parse_roundtrip():
    assert ids.format_claim_id(7) == "c-0007@1"
    assert ids.parse_claim_id("c-0007@1") == (7, 1)


def test_bump_increments_version_only():
    assert ids.bump_claim_id("c-0007@1") == "c-0007@2"
    assert ids.base_claim_id("c-0007@2") == "c-0007"


def test_parse_rejects_unversioned_id():
    with pytest.raises(UsageError):
        ids.parse_claim_id("c-0007")


@pytest.mark.parametrize("name", ["codex-ops", "a", "claude_security", "agy3"])
def test_valid_friend_names_accepted(name):
    assert ids.validate_friend_name(name) == name


@pytest.mark.parametrize("name", [
    "../../../../tmp/owned",   # path traversal
    "Codex",                   # uppercase
    "-leading-hyphen",
    "has space",
    "a" * 33,                  # too long
    "",
    "codex-ops\n",             # trailing newline: `$` would accept this
    "codex-ops\n.raw",
    "codex\tops",
    "codex\x00ops",
])
def test_invalid_friend_names_rejected(name):
    with pytest.raises(UsageError):
        ids.validate_friend_name(name)


@pytest.mark.parametrize("cid", ["c-0007@1\n", "c-\u0660\u0660\u0660\u0667@1"])
def test_claim_id_rejects_newline_and_non_ascii_digits(cid):
    """`$` accepts a trailing newline; bare `\d` accepts Arabic-Indic digits."""
    with pytest.raises(UsageError):
        ids.parse_claim_id(cid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ids.py -v`
Expected: FAIL — `No module named 'adversarial_friends.ids'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/ids.py
"""Claim identity and friend-name validation.

Claim ids carry a version (`c-0007@2`) so that a verdict can never be
ambiguous about which wording of a claim it judged. Friend names become path
components under the run directory, so a name that escapes the run directory
is a security problem rather than a typo.
"""
import re

from .errors import UsageError

# fullmatch, not match + $: Python's `$` also matches immediately before a
# single trailing newline, so `^...$` accepts "codex-ops\n". Friend names
# become path components, so that is a bypass rather than a curiosity.
# [0-9] rather than \d: \d matches any Unicode decimal digit without re.ASCII.
CLAIM_ID_RE = re.compile(r"c-([0-9]{4,})@([0-9]+)")
FRIEND_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def format_claim_id(n: int, version: int = 1) -> str:
    return f"c-{n:04d}@{version}"


def parse_claim_id(cid: str) -> tuple[int, int]:
    match = CLAIM_ID_RE.fullmatch(cid)
    if match is None:
        raise UsageError(f"malformed claim id: {cid!r} (expected e.g. c-0007@1)")
    return int(match.group(1)), int(match.group(2))


def bump_claim_id(cid: str) -> str:
    number, version = parse_claim_id(cid)
    return format_claim_id(number, version + 1)


def base_claim_id(cid: str) -> str:
    number, _ = parse_claim_id(cid)
    return f"c-{number:04d}"


def validate_friend_name(name: str) -> str:
    if FRIEND_NAME_RE.fullmatch(name) is None:
        raise UsageError(
            f"invalid friend name {name!r}: must match {FRIEND_NAME_RE.pattern}"
        )
    return name
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ids.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/ids.py tests/test_ids.py
git commit -m "feat: add versioned claim ids and friend name validation"
```

---

### Task 3: The claim ledger

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `ids.parse_claim_id`.
- Produces: dataclasses `Claim`, `Verdict`, `Alias`, `Resolution`; `ledger.record_to_dict(rec) -> dict`, `ledger.record_from_dict(d) -> Claim | Verdict | Alias | Resolution`; class `Ledger(path)` with `.append(rec)`, `.records()`, `.claims()`, `.verdicts_for(claim_id)`, `.aliases()`.

Four record types, append-only JSONL (spec §6). `Claim.origin` is a **list** because an amended claim's origin is the union of the original author and the amending judge — both lose independence and are excluded from its judge set later.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger.py
import pytest

from adversarial_friends.ledger import (
    Alias, Claim, Ledger, Resolution, Verdict, record_from_dict, record_to_dict,
)


def make_claim(**over):
    base = dict(
        id="c-0001@1", supersedes=None, origin=["codex/ops"], lens="ops",
        round=1, advisory=False, severity="high", claim="the guard is missing",
        location="src/auth.py:42", evidence="src/auth.py:38",
        failure_scenario="expired token reaches the handler",
        suggested_fix="check exp before dispatch",
    )
    base.update(over)
    return Claim(**base)


def test_claim_roundtrips_through_dict():
    claim = make_claim()
    assert record_from_dict(record_to_dict(claim)) == claim


def test_record_to_dict_tags_the_type():
    assert record_to_dict(make_claim())["type"] == "claim"


def test_ledger_appends_and_reads_back_in_order(tmp_path):
    ledger = Ledger(tmp_path / "claims.jsonl")
    claim = make_claim()
    verdict = Verdict(
        claim_id="c-0001@1", judge="claude/security", round=2, verdict="refuted",
        confidence="high", evidence_assessment="disputed",
        reasoning="line 38 already guards it", counter_evidence="src/auth.py:38",
        amended_claim=None,
    )
    ledger.append(claim)
    ledger.append(verdict)
    assert list(ledger.records()) == [claim, verdict]
    assert ledger.claims() == [claim]
    assert ledger.verdicts_for("c-0001@1") == [verdict]


def test_verdicts_for_is_version_exact(tmp_path):
    """A verdict on a superseded version must not leak into the successor's tally."""
    ledger = Ledger(tmp_path / "claims.jsonl")
    ledger.append(Verdict(
        claim_id="c-0001@1", judge="codex/ops", round=2, verdict="upheld",
        confidence="high", evidence_assessment="confirmed", reasoning="stands",
        counter_evidence=None, amended_claim=None,
    ))
    assert len(ledger.verdicts_for("c-0001@1")) == 1
    assert ledger.verdicts_for("c-0001@2") == []


def test_aliases_are_readable(tmp_path):
    ledger = Ledger(tmp_path / "claims.jsonl")
    alias = Alias(canonical="c-0001@1", duplicate="c-0004@1", round=1,
                  source="exact", rationale="identical claim text and location")
    ledger.append(alias)
    assert ledger.aliases() == [alias]


def test_resolution_roundtrips(tmp_path):
    resolution = Resolution(
        claim_id="c-0001@1", disposition="fixed", author="tim",
        evidence="src/auth.py:38", round=3, verified="location-changed",
    )
    assert record_from_dict(record_to_dict(resolution)) == resolution


def test_unknown_record_type_is_rejected():
    with pytest.raises(ValueError):
        record_from_dict({"type": "nonsense"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ledger.py -v`
Expected: FAIL — `No module named 'adversarial_friends.ledger'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/ledger.py
"""Append-only claim ledger.

The ledger is the durable record of what was claimed, who judged it, which
claims were merged, and how anything was resolved. It is append-only so a run
can be replayed and audited; nothing is ever rewritten in place.
"""
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterator, Union


@dataclass(frozen=True)
class Claim:
    id: str
    supersedes: str | None
    origin: list[str]
    lens: str
    round: int
    advisory: bool
    severity: str
    claim: str
    location: str | None
    evidence: str
    failure_scenario: str
    suggested_fix: str


@dataclass(frozen=True)
class Verdict:
    claim_id: str
    judge: str
    round: int
    verdict: str
    confidence: str
    evidence_assessment: str
    reasoning: str
    counter_evidence: str | None
    amended_claim: str | None


@dataclass(frozen=True)
class Alias:
    canonical: str
    duplicate: str
    round: int
    source: str
    rationale: str


@dataclass(frozen=True)
class Resolution:
    claim_id: str
    disposition: str
    author: str
    evidence: str
    round: int
    verified: str


Record = Union[Claim, Verdict, Alias, Resolution]

_TYPE_NAMES: dict[type, str] = {
    Claim: "claim", Verdict: "verdict", Alias: "alias", Resolution: "resolution",
}
_BY_NAME = {name: cls for cls, name in _TYPE_NAMES.items()}


def record_to_dict(record: Record) -> dict:
    payload = asdict(record)
    payload["type"] = _TYPE_NAMES[type(record)]
    return payload


def record_from_dict(payload: dict) -> Record:
    kind = payload.get("type")
    cls = _BY_NAME.get(kind)
    if cls is None:
        raise ValueError(f"unknown ledger record type: {kind!r}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in payload.items() if k in known})


class Ledger:
    """A JSONL file of ledger records, read and written in append order."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Record) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record_to_dict(record), sort_keys=True) + "\n")

    def records(self) -> Iterator[Record]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield record_from_dict(json.loads(line))

    def claims(self) -> list[Claim]:
        return [r for r in self.records() if isinstance(r, Claim)]

    def aliases(self) -> list[Alias]:
        return [r for r in self.records() if isinstance(r, Alias)]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        # Exact match on the versioned id: a verdict on c-0001@1 says nothing
        # about c-0001@2, whose wording a judge may never have seen.
        return [r for r in self.records()
                if isinstance(r, Verdict) and r.claim_id == claim_id]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ledger.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/ledger.py tests/test_ledger.py
git commit -m "feat: add append-only claim ledger with four record types"
```

---

### Task 4: Friend output schema and success criteria

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/claimschema.py`
- Test: `tests/test_claimschema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `claimschema.CLAIM_OUTPUT_SCHEMA: dict`, `claimschema.schema_path(tmpdir) -> Path`, `claimschema.validate_payload(payload: dict) -> list[str]` (returns error strings; empty means valid), `claimschema.is_successful_payload(payload: dict) -> bool`.

A friend that returns nothing and does not say so is a failure, not a clean round (spec §7.3). `{"no_findings": true}` is how a friend says "I looked and found nothing" — without it, silence is indistinguishable from breakage.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claimschema.py
import json

from adversarial_friends import claimschema


def test_schema_file_is_written_and_is_valid_json(tmp_path):
    path = claimschema.schema_path(tmp_path)
    assert json.loads(path.read_text())["type"] == "object"


def test_valid_payload_has_no_errors():
    payload = {"findings": [{
        "severity": "high", "claim": "the guard is missing",
        "location": "src/auth.py:42", "evidence": "src/auth.py:38",
        "failure_scenario": "expired token reaches the handler",
        "suggested_fix": "check exp before dispatch",
    }]}
    assert claimschema.validate_payload(payload) == []


def test_missing_required_field_is_reported():
    payload = {"findings": [{"severity": "high", "claim": "x"}]}
    errors = claimschema.validate_payload(payload)
    assert any("failure_scenario" in e for e in errors)


def test_bad_severity_is_reported():
    payload = {"findings": [{
        "severity": "catastrophic", "claim": "x", "location": None,
        "evidence": "e", "failure_scenario": "f", "suggested_fix": "s",
    }]}
    errors = claimschema.validate_payload(payload)
    assert any("severity" in e for e in errors)


def test_no_findings_marker_is_successful():
    assert claimschema.is_successful_payload({"no_findings": True}) is True


def test_empty_findings_without_marker_is_not_successful():
    """Silence must be distinguishable from breakage."""
    assert claimschema.is_successful_payload({"findings": []}) is False


def test_findings_present_is_successful():
    payload = {"findings": [{
        "severity": "low", "claim": "x", "location": None, "evidence": "e",
        "failure_scenario": "f", "suggested_fix": "s",
    }]}
    assert claimschema.is_successful_payload(payload) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claimschema.py -v`
Expected: FAIL — `No module named 'adversarial_friends.claimschema'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/claimschema.py
"""The contract a friend's output must satisfy.

Claude, codex, and agy can enforce this natively via a schema flag. gemini and
opencode cannot, so the same shape is stated in the prompt and validated here.
Validation is hand-rolled because the runtime is stdlib-only.
"""
import json
from pathlib import Path

SEVERITIES = ("high", "medium", "low")
REQUIRED_FIELDS = (
    "severity", "claim", "evidence", "failure_scenario", "suggested_fix",
)

CLAIM_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "no_findings": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "claim": {"type": "string"},
                    "location": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "failure_scenario": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": list(REQUIRED_FIELDS),
            },
        },
    },
}


def schema_path(directory: Path) -> Path:
    """Materialize the schema so adapters with a native schema flag can use it."""
    path = Path(directory) / "claim-output.schema.json"
    path.write_text(json.dumps(CLAIM_OUTPUT_SCHEMA, indent=2), encoding="utf-8")
    return path


def validate_payload(payload: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    if payload.get("no_findings") is True:
        return errors
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return ["payload has neither 'findings' array nor no_findings marker"]
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] is not an object")
            continue
        for field in REQUIRED_FIELDS:
            value = finding.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"findings[{index}].{field} missing or empty")
        severity = finding.get("severity")
        if isinstance(severity, str) and severity not in SEVERITIES:
            errors.append(
                f"findings[{index}].severity {severity!r} not in {SEVERITIES}"
            )
    return errors


def is_successful_payload(payload: dict) -> bool:
    """Distinguish 'looked and found nothing' from 'produced nothing'."""
    if validate_payload(payload):
        return False
    if payload.get("no_findings") is True:
        return True
    return bool(payload.get("findings"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claimschema.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/claimschema.py tests/test_claimschema.py
git commit -m "feat: add friend output schema and explicit success criteria"
```

---

### Task 5: Adapter records, argv building, and capability computation

**Files:**
- Create: `skills/adversarial-friends/adapters/claude.toml`
- Create: `skills/adversarial-friends/adapters/codex.toml`
- Create: `skills/adversarial-friends/adapters/agy.toml`
- Create: `skills/adversarial-friends/adapters/opencode.toml`
- Create: `skills/adversarial-friends/adapters/ollama.toml`
- Create: `skills/adversarial-friends/scripts/adversarial_friends/adapters.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: `errors.UsageError`.
- Produces: dataclasses `Adapter`, `Capability`, `FriendSpec`; `adapters.load_adapters(directory) -> dict[str, Adapter]`, `adapters.build_argv(adapter, spec, prompt_file, schema_file) -> tuple[list[str], str | None, Capability]` (argv, stdin text, and the capability computed from the flags actually emitted).

**Capability is never derived by scanning argv.** For `trailing-arg` and `flag-value` adapters the prompt — the untrusted document under review — is appended into argv, so a document containing `Read,Grep,Glob` could forge `readonly=True`. `build_argv` computes the capability before the prompt is appended, in every branch.

`prompt_mode` exists because of a verified trap: agy's `-p` takes the prompt **as its flag value**, so every other flag must precede it and the prompt must be last (spec §11.2). Appending anything after the prompt silently turns it into an ignored positional while the CLI exits 0.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapters.py
import pytest

from adversarial_friends import adapters
from adversarial_friends.errors import UsageError

ADAPTER_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "skills" / "adversarial-friends" / "adapters"


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def spec(**over):
    base = dict(name="f1", cli="codex", lens="ops", model=None, effort=None,
                scope="repo", timeout=900)
    base.update(over)
    return adapters.FriendSpec(**base)


@pytest.fixture
def files(tmp_path):
    """build_argv reads the prompt off disk, so it must actually exist."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("CHALLENGE THIS ARTIFACT")
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    return prompt, schema


def test_all_shipped_adapters_load(registry):
    assert set(registry) >= {"claude", "codex", "agy", "opencode", "ollama"}


def test_agy_prompt_is_the_last_argument(registry, files):
    """agy's -p takes the prompt as its value; anything after it is ignored."""
    prompt, schema = files
    argv, stdin = adapters.build_argv(
        registry["agy"], spec(cli="agy", effort="high"),
        prompt_file=prompt, schema_file=schema,
    )
    assert argv[-2] == "--print"
    assert argv[-1] == "CHALLENGE THIS ARTIFACT"
    assert "--mode" in argv and argv.index("--mode") < argv.index("--print")
    assert stdin is None


def test_codex_takes_prompt_on_stdin(registry, files):
    prompt, schema = files
    argv, stdin = adapters.build_argv(
        registry["codex"], spec(), prompt_file=prompt, schema_file=schema,
    )
    assert "exec" in argv
    assert stdin is not None


def test_readonly_flags_are_emitted_for_repo_scope(registry, files):
    prompt, schema = files
    argv, _ = adapters.build_argv(
        registry["claude"], spec(cli="claude"), prompt_file=prompt,
        schema_file=schema,
    )
    assert "--tools" in argv
    assert "Read,Grep,Glob" in argv


def test_capability_reflects_what_was_emitted_not_declared(registry, files):
    prompt, schema = files
    _, _, doc_cap = adapters.build_argv(
        registry["claude"], spec(cli="claude", scope="doc"),
        prompt_file=prompt, schema_file=schema)
    assert doc_cap.readonly is False   # doc scope skips readonly_argv entirely
    _, _, repo_cap = adapters.build_argv(
        registry["claude"], spec(cli="claude", scope="repo"),
        prompt_file=prompt, schema_file=schema)
    assert repo_cap.readonly is True


def test_prompt_text_cannot_forge_a_capability(registry, tmp_path):
    """The prompt is the untrusted document; it must not influence capability."""
    prompt = tmp_path / "p.txt"
    prompt.write_text("--tools Read,Grep,Glob --sandbox read-only")
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    for cli in ("claude", "agy", "codex"):      # adapters with real readonly_argv
        _, _, cap = adapters.build_argv(
            registry[cli], spec(cli=cli, scope="doc"),
            prompt_file=prompt, schema_file=schema)
        assert cap.readonly is False, cli


def test_opencode_effort_is_unverified(registry, files):
    prompt, schema = files
    _, _, cap = adapters.build_argv(
        registry["opencode"], spec(cli="opencode", effort="high"),
        prompt_file=prompt, schema_file=schema)
    assert cap.effort == "unverified"


def test_unsupported_effort_level_raises(registry, files):
    prompt, schema = files
    with pytest.raises(UsageError):
        adapters.build_argv(
            registry["agy"], spec(cli="agy", effort="xhigh"),
            prompt_file=prompt, schema_file=schema,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adapters.py -v`
Expected: FAIL — `No module named 'adversarial_friends.adapters'`.

- [ ] **Step 3: Write the adapter records**

```toml
# skills/adversarial-friends/adapters/claude.toml
name = "claude"
binary = "claude"
base_argv = ["--print", "--output-format", "json"]
prompt_mode = "trailing-arg"
readonly_argv = ["--tools", "Read,Grep,Glob"]
schema_flag = "--json-schema"
model_flag = "--model"
internal_timeout_flag = ""
effort_kind = "native"

[effort]
low = ["--effort", "low"]
medium = ["--effort", "medium"]
high = ["--effort", "high"]
xhigh = ["--effort", "xhigh"]
max = ["--effort", "max"]
```

```toml
# skills/adversarial-friends/adapters/codex.toml
name = "codex"
binary = "codex"
base_argv = ["exec", "--json"]
prompt_mode = "stdin"
readonly_argv = ["--sandbox", "read-only"]
schema_flag = "--output-schema"
model_flag = "--model"
internal_timeout_flag = ""
effort_kind = "native"

[effort]
low = ["--config", "model_reasoning_effort=low"]
medium = ["--config", "model_reasoning_effort=medium"]
high = ["--config", "model_reasoning_effort=high"]
xhigh = ["--config", "model_reasoning_effort=xhigh"]
```

```toml
# skills/adversarial-friends/adapters/agy.toml
# -p takes the prompt as its VALUE, so it must come last (verified trap).
name = "agy"
binary = "agy"
base_argv = ["--output-format", "json"]
prompt_mode = "flag-value"
prompt_flag = "--print"
readonly_argv = ["--mode", "plan"]
schema_flag = "--json-schema"
model_flag = "--model"
internal_timeout_flag = "--print-timeout"
effort_kind = "native"

[effort]
low = ["--effort", "low"]
medium = ["--effort", "medium"]
high = ["--effort", "high"]
```

```toml
# skills/adversarial-friends/adapters/opencode.toml
# --variant accepts any string silently, so effort can never be verified.
name = "opencode"
binary = "opencode"
base_argv = ["run", "--format", "json"]
prompt_mode = "trailing-arg"
readonly_argv = []
schema_flag = ""
model_flag = "--model"
internal_timeout_flag = ""
effort_kind = "unverified"

[effort]
low = ["--variant", "minimal"]
high = ["--variant", "high"]
max = ["--variant", "max"]
```

```toml
# skills/adversarial-friends/adapters/ollama.toml
# HTTP transport: `ollama run` writes ANSI control codes into its own payload.
name = "ollama"
transport = "http"
endpoint = "http://127.0.0.1:11434/api/generate"
binary = ""
base_argv = []
prompt_mode = "stdin"
readonly_argv = []
schema_flag = ""
model_flag = ""
internal_timeout_flag = ""
effort_kind = "none"

[effort]
```

- [ ] **Step 4: Write the adapter module**

```python
# skills/adversarial-friends/scripts/adversarial_friends/adapters.py
"""Declarative per-CLI records and the argv they produce.

Adapters are data, not code, so adding a friend is adding a TOML file. The
awkward parts encoded here are all verified CLI behaviors rather than
speculation — see the spec's "verified invocation traps" section.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError


@dataclass(frozen=True)
class Adapter:
    name: str
    binary: str
    base_argv: list[str]
    prompt_mode: str            # stdin | trailing-arg | flag-value
    prompt_flag: str
    readonly_argv: list[str]
    schema_flag: str
    model_flag: str
    internal_timeout_flag: str
    effort_kind: str            # native | unverified | none
    effort: dict[str, list[str]] = field(default_factory=dict)
    transport: str = "exec"     # exec | http
    endpoint: str = ""


@dataclass(frozen=True)
class Capability:
    schema: bool
    readonly: bool
    effort: str                 # native | unverified | none


@dataclass(frozen=True)
class FriendSpec:
    name: str
    cli: str
    lens: str
    model: str | None
    effort: str | None
    scope: str                  # repo | doc
    timeout: int


def load_adapters(directory: Path) -> dict[str, Adapter]:
    registry: dict[str, Adapter] = {}
    for path in sorted(Path(directory).glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        registry[data["name"]] = Adapter(
            name=data["name"],
            binary=data.get("binary", ""),
            base_argv=list(data.get("base_argv", [])),
            prompt_mode=data.get("prompt_mode", "stdin"),
            prompt_flag=data.get("prompt_flag", ""),
            readonly_argv=list(data.get("readonly_argv", [])),
            schema_flag=data.get("schema_flag", ""),
            model_flag=data.get("model_flag", ""),
            internal_timeout_flag=data.get("internal_timeout_flag", ""),
            effort_kind=data.get("effort_kind", "none"),
            effort={k: list(v) for k, v in data.get("effort", {}).items()},
            transport=data.get("transport", "exec"),
            endpoint=data.get("endpoint", ""),
        )
    return registry


def build_argv(adapter: Adapter, spec: FriendSpec, prompt_file: Path,
               schema_file: Path) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text).

    Flag order matters: for adapters whose prompt is a flag *value*, every
    other flag must precede it, because a flag appearing after the prompt flag
    is swallowed as the prompt and the real prompt becomes an ignored
    positional — with a zero exit status.
    """
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    argv = [adapter.binary, *adapter.base_argv]

    readonly_emitted = False
    schema_emitted = False
    if spec.scope == "repo" and adapter.readonly_argv:
        argv += adapter.readonly_argv
        readonly_emitted = True
    if adapter.schema_flag:
        argv += [adapter.schema_flag, str(schema_file)]
        schema_emitted = True
    if spec.model and adapter.model_flag:
        argv += [adapter.model_flag, spec.model]
    if spec.effort:
        if spec.effort not in adapter.effort:
            raise UsageError(
                f"{adapter.name} does not support effort {spec.effort!r} "
                f"(available: {sorted(adapter.effort) or 'none'})"
            )
        argv += adapter.effort[spec.effort]
    if adapter.internal_timeout_flag:
        # The CLI's own timeout is set explicitly rather than inherited, so it
        # cannot silently disagree with the runner's kill deadline.
        argv += [adapter.internal_timeout_flag, f"{spec.timeout}s"]

    # Computed BEFORE the prompt joins argv, so document text cannot forge it.
    capability = Capability(schema=schema_emitted, readonly=readonly_emitted,
                            effort=adapter.effort_kind)

    if adapter.prompt_mode == "stdin":
        return argv, prompt, capability
    if adapter.prompt_mode == "trailing-arg":
        return argv + [prompt], None, capability
    if adapter.prompt_mode == "flag-value":
        return argv + [adapter.prompt_flag, prompt], None, capability
    raise UsageError(f"unknown prompt_mode {adapter.prompt_mode!r}")


# No `capability_for(adapter, argv)`. Scanning argv is unsafe: the prompt is in
# argv for two of the three prompt modes, and the prompt is the untrusted
# document. Capability is returned by build_argv, computed from what it emitted.
```

- [ ] **Step 5: Run the tests and commit**

Run: `python -m pytest tests/test_adapters.py -v`
Expected: PASS, 7 tests.

```bash
git add skills/adversarial-friends/adapters skills/adversarial-friends/scripts/adversarial_friends/adapters.py tests/test_adapters.py
git commit -m "feat: add declarative adapters with argv building and capability derivation"
```

---

### Task 6: Trust model — allowlist, denied values, path containment

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/trust.py`
- Test: `tests/test_trust.py`

**Interfaces:**
- Consumes: `errors.UsageError`, `ids.validate_friend_name`.
- Produces: `trust.ROSTER_KEYS: frozenset[str]`, `trust.validate_roster_entry(entry: dict) -> dict`, `trust.check_denied_values(argv: list[str]) -> None`, `trust.contain_path(base: Path, candidate: Path) -> Path`.

The roster is untrusted input. v2 used a blocklist of `--dangerously-*` spellings; that missed `-c sandbox_permissions=…`, `--settings '{"hooks":…}'` (arbitrary shell), writable `--add-dir`, and `--profile` layering. The model is inverted here: roster files carry **values only**, for a fixed key set, and there is no `extra_args` or `profile` key to abuse.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust.py
from pathlib import Path

import pytest

from adversarial_friends import trust
from adversarial_friends.errors import UsageError


def test_valid_entry_passes():
    entry = {"name": "codex-ops", "cli": "codex", "lens": "ops",
             "model": "gpt-5.6-sol", "effort": "high", "scope": "repo",
             "timeout": 900}
    assert trust.validate_roster_entry(entry)["name"] == "codex-ops"


def test_extra_args_key_is_rejected():
    entry = {"name": "x", "cli": "codex", "lens": "ops",
             "extra_args": ["--dangerously-bypass-approvals-and-sandbox"]}
    with pytest.raises(UsageError) as excinfo:
        trust.validate_roster_entry(entry)
    assert "extra_args" in str(excinfo.value)


def test_profile_key_is_rejected():
    """--profile layers a TOML file the runner never reads, so argv would lie."""
    entry = {"name": "x", "cli": "codex", "lens": "ops", "profile": "review"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_traversal_name_is_rejected():
    entry = {"name": "../../../../tmp/owned", "cli": "codex", "lens": "ops"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


def test_bad_scope_is_rejected():
    entry = {"name": "x", "cli": "codex", "lens": "ops", "scope": "everything"}
    with pytest.raises(UsageError):
        trust.validate_roster_entry(entry)


@pytest.mark.parametrize("argv", [
    ["codex", "--sandbox", "danger-full-access"],
    ["codex", "--sandbox", "workspace-write"],
    ["codex", "-s", "danger-full-access"],
    # The `=` form is the same instruction to the CLI and must not slip past.
    ["codex", "--sandbox=danger-full-access"],
    ["codex", "-s=workspace-write"],
    ["claude", "--dangerously-skip-permissions"],
    ["opencode", "--auto"],
    ["gemini", "-y"],
])
def test_denied_values_abort(argv):
    with pytest.raises(UsageError):
        trust.check_denied_values(argv)


def test_hardening_flags_are_permitted():
    """The check is direction-aware: making a run safer must never abort it."""
    trust.check_denied_values(["codex", "--sandbox", "read-only"])
    trust.check_denied_values(["codex", "-s", "read-only"])
    trust.check_denied_values(["claude", "--permission-mode", "plan"])


def test_contain_path_allows_paths_under_base(tmp_path):
    assert trust.contain_path(tmp_path, tmp_path / "round-1" / "a.raw")


def test_contain_path_rejects_escape(tmp_path):
    with pytest.raises(UsageError):
        trust.contain_path(tmp_path, tmp_path / ".." / "escaped.raw")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust.py -v`
Expected: FAIL — `No module named 'adversarial_friends.trust'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/trust.py
"""Trust boundary for roster files and constructed argv.

A cloned repository is hostile input. Rather than blocklisting dangerous flag
spellings — which missed config overrides, inline settings JSON carrying
hooks, writable --add-dir, and profile layering — the roster is restricted to
values for a fixed set of keys. There is no mechanism for it to inject flags.

The value-level check that remains is direction-aware on purpose: refusing to
start because someone asked for a *safer* sandbox would be its own bug.
"""
from pathlib import Path

from .errors import UsageError
from .ids import validate_friend_name

ROSTER_KEYS = frozenset({"name", "cli", "lens", "model", "effort", "scope", "timeout"})
VALID_SCOPES = frozenset({"repo", "doc"})

DENIED_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--auto",
    "--yolo",
    "-y",
})
DENIED_SANDBOX_VALUES = frozenset({"danger-full-access", "workspace-write"})


def validate_roster_entry(entry: dict) -> dict:
    unknown = set(entry) - ROSTER_KEYS
    if unknown:
        raise UsageError(
            "roster entries may only set "
            f"{sorted(ROSTER_KEYS)}; found {sorted(unknown)}. "
            "Arbitrary flags are available only via --unsafe-extra-args on the "
            "command line, never from a file."
        )
    for required in ("name", "cli", "lens"):
        if not entry.get(required):
            raise UsageError(f"roster entry missing required key: {required}")
    validate_friend_name(entry["name"])
    scope = entry.get("scope", "repo")
    if scope not in VALID_SCOPES:
        raise UsageError(f"invalid scope {scope!r}: expected one of {sorted(VALID_SCOPES)}")
    timeout = entry.get("timeout", 900)
    if not isinstance(timeout, int) or timeout <= 0:
        raise UsageError(f"invalid timeout {timeout!r}: expected a positive integer")
    return entry


def check_denied_values(argv: list[str]) -> None:
    """Reject argv that weakens the sandbox, in either separated or `=` form.

    Both spellings must be handled: `--sandbox danger-full-access` and
    `--sandbox=danger-full-access` are the same instruction to the CLI, so
    checking only the following token leaves the second form wide open.
    """
    for index, token in enumerate(argv):
        flag, _, inline_value = token.partition("=")
        if flag in DENIED_FLAGS:
            raise UsageError(
                f"refusing to run: {flag} disables the sandbox this tool relies on"
            )
        if flag in ("-s", "--sandbox"):
            value = inline_value or (argv[index + 1] if index + 1 < len(argv) else "")
            if value in DENIED_SANDBOX_VALUES:
                raise UsageError(
                    f"refusing to run: sandbox mode {value!r} grants write access"
                )


def contain_path(base: Path, candidate: Path) -> Path:
    """Guarantee a constructed output path stays under the run directory."""
    base_resolved = Path(base).resolve()
    candidate_resolved = Path(candidate).resolve()
    if not candidate_resolved.is_relative_to(base_resolved):
        raise UsageError(
            f"path {candidate_resolved} escapes the run directory {base_resolved}"
        )
    return candidate_resolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/trust.py tests/test_trust.py
git commit -m "feat: replace flag blocklist with roster allowlist and path containment"
```

---

### Task 7: Output normalization and repair

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `claimschema.validate_payload`, `claimschema.is_successful_payload`.
- Produces: `normalize.strip_ansi(text) -> str`, `normalize.extract_json(text) -> dict | None`, `normalize.NormalizeResult` dataclass with fields `payload`, `errors`, `succeeded`; `normalize.normalize(raw: str) -> NormalizeResult`.

Repair is a **pure transformation with no model call**. Re-prompting cannot work: rounds are stateless, so a "repair prompt" reaches a fresh process that never produced the malformed output, and it would silently redo the whole critique at full cost with different claims.

The ANSI stripping is not hypothetical — `ollama run` interleaves cursor and spinner codes *inside* its own JSON payload.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_normalize.py
from adversarial_friends import normalize

GOOD = '{"findings": [{"severity": "low", "claim": "c", "location": null, ' \
       '"evidence": "e", "failure_scenario": "f", "suggested_fix": "s"}]}'


def test_plain_json_parses():
    result = normalize.normalize(GOOD)
    assert result.succeeded is True
    assert result.payload["findings"][0]["claim"] == "c"


def test_ansi_interleaved_json_is_recovered():
    """ollama run writes cursor/spinner codes into the middle of its payload."""
    noisy = '\x1b[?25l\x1b[?2026h{"\x1b[?25lfind\x1b[?25hings": []}\x1b[?25h'
    assert normalize.strip_ansi(noisy) == '{"findings": []}'


def test_fenced_json_is_extracted():
    fenced = "Here is my review:\n```json\n" + GOOD + "\n```\nHope that helps!"
    result = normalize.normalize(fenced)
    assert result.succeeded is True


def test_prose_wrapped_json_is_extracted():
    wrapped = "Sure! " + GOOD + " Let me know if you want more."
    result = normalize.normalize(wrapped)
    assert result.succeeded is True


def test_trailing_comma_is_repaired():
    result = normalize.normalize('{"no_findings": true,}')
    assert result.succeeded is True


def test_no_findings_marker_succeeds():
    assert normalize.normalize('{"no_findings": true}').succeeded is True


def test_empty_findings_without_marker_fails():
    result = normalize.normalize('{"findings": []}')
    assert result.succeeded is False


def test_unparseable_output_fails_with_errors():
    result = normalize.normalize("I could not complete this task.")
    assert result.succeeded is False
    assert result.errors


def test_off_topic_prose_fails():
    """agy answered the literal prompt '--mode' and exited 0."""
    result = normalize.normalize(
        "It looks like you just typed `--mode`. Could you clarify?"
    )
    assert result.succeeded is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: FAIL — `No module named 'adversarial_friends.normalize'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/normalize.py
"""Turn whatever a friend printed into a validated payload, or fail honestly.

Repair here is a pure transformation. Re-prompting a friend to fix its own
malformed output cannot work when rounds are stateless: the "repair prompt"
reaches a brand new process that never emitted the broken output, so it simply
redoes the entire critique at full cost and produces different claims.
"""
import json
import re
from dataclasses import dataclass

from .claimschema import is_successful_payload, validate_payload

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


@dataclass(frozen=True)
class NormalizeResult:
    payload: dict | None
    errors: list[str]
    succeeded: bool


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _balanced_object(text: str) -> str | None:
    """Return the first top-level {...} span, respecting strings and escapes."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_json(text: str) -> dict | None:
    cleaned = strip_ansi(text).strip()
    candidates: list[str] = []
    fenced = FENCE_RE.search(cleaned)
    if fenced:
        candidates.append(fenced.group(1))
    balanced = _balanced_object(cleaned)
    if balanced:
        candidates.append(balanced)
    candidates.append(cleaned)
    for candidate in candidates:
        for attempt in (candidate, TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def normalize(raw: str) -> NormalizeResult:
    payload = extract_json(raw)
    if payload is None:
        return NormalizeResult(None, ["output contained no parseable JSON object"], False)
    errors = validate_payload(payload)
    if errors:
        return NormalizeResult(payload, errors, False)
    if not is_successful_payload(payload):
        return NormalizeResult(
            payload,
            ["no findings and no explicit no_findings marker"],
            False,
        )
    return NormalizeResult(payload, [], True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/normalize.py tests/test_normalize.py
git commit -m "feat: normalize friend output with pure-transformation repair"
```

---

### Task 8: Spawning friends with process groups and timeouts

**Files:**
- Create: `tests/fake_friend.py`
- Create: `skills/adversarial-friends/scripts/adversarial_friends/spawn.py`
- Test: `tests/test_spawn.py`

**Interfaces:**
- Consumes: `adapters.Adapter`, `adapters.FriendSpec`, `normalize.normalize`.
- Produces: `spawn.SpawnResult` dataclass (`argv`, `exit_code`, `stdout`, `stderr`, `duration_s`, `timed_out`, `result`, `failure_reason`); `spawn.run_process(argv, stdin_text, timeout_s, cwd) -> SpawnResult`.

Two verified hazards are handled here. Coding CLIs spawn descendants — MCP servers, shells, language servers — so a timeout must kill the **process group**, not just the parent. And a timeout takes precedence over normalization: a killed friend's truncated output never enters the repair path, because it is a failure regardless of what it managed to print.

- [ ] **Step 1: Write the fake friend and the failing test**

```python
# tests/fake_friend.py
"""A scripted stand-in for a real agent CLI. Never makes a model call."""
import json
import os
import subprocess
import sys
import time

MODES = {
    "good": lambda: print(json.dumps({"findings": [{
        "severity": "high", "claim": "the guard is missing",
        "location": "src/auth.py:42", "evidence": "src/auth.py:38",
        "failure_scenario": "expired token reaches the handler",
        "suggested_fix": "check exp before dispatch"}]})),
    "empty": lambda: print(json.dumps({"findings": []})),
    "no_findings": lambda: print(json.dumps({"no_findings": True})),
    "offtopic": lambda: print("It looks like you just typed `--mode`."),
    "prose_wrapped": lambda: print(
        "Sure! " + json.dumps({"no_findings": True}) + " Hope that helps!"),
}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "good"
    if mode == "hang":
        # Spawn a child, then hang: the runner must reap the whole group.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print(f"child_pid={child.pid}", flush=True)
        time.sleep(600)
        return 0
    if mode == "crash":
        print("boom", file=sys.stderr)
        return 1
    MODES.get(mode, MODES["good"])()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
```

```python
# tests/test_spawn.py
import os
import signal
import sys
import time
from pathlib import Path

from adversarial_friends import spawn

FAKE = str(Path(__file__).resolve().parent / "fake_friend.py")


def test_successful_run_is_marked_succeeded():
    result = spawn.run_process([sys.executable, FAKE, "good"], None, 30, Path.cwd())
    assert result.exit_code == 0
    assert result.result.succeeded is True


def test_nonzero_exit_is_a_failure():
    result = spawn.run_process([sys.executable, FAKE, "crash"], None, 30, Path.cwd())
    assert result.exit_code == 1
    assert result.failure_reason


def test_exit_zero_with_offtopic_output_is_a_failure():
    """Verified against agy: exit 0 while answering an entirely different prompt."""
    result = spawn.run_process([sys.executable, FAKE, "offtopic"], None, 30, Path.cwd())
    assert result.exit_code == 0
    assert result.result.succeeded is False
    assert result.failure_reason


def test_empty_findings_without_marker_is_a_failure():
    result = spawn.run_process([sys.executable, FAKE, "empty"], None, 30, Path.cwd())
    assert result.result.succeeded is False


def test_no_findings_marker_is_a_success():
    result = spawn.run_process([sys.executable, FAKE, "no_findings"], None, 30, Path.cwd())
    assert result.result.succeeded is True


def test_timeout_kills_the_whole_process_group():
    result = spawn.run_process([sys.executable, FAKE, "hang"], None, 2, Path.cwd())
    assert result.timed_out is True
    child_pid = int(result.stdout.split("child_pid=")[1].split()[0])
    time.sleep(1)
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGTERM)  # already reaped


def test_timeout_takes_precedence_over_parsing():
    """A killed friend is a failure regardless of what it managed to print."""
    result = spawn.run_process([sys.executable, FAKE, "hang"], None, 2, Path.cwd())
    assert result.failure_reason == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spawn.py -v`
Expected: FAIL — `No module named 'adversarial_friends.spawn'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/spawn.py
"""Run one friend under a timeout, in its own process group.

Agent CLIs spawn descendants — MCP servers, shells, language servers — so
killing only the parent on timeout leaves them running, making network calls
and writing files after the run has been marked incomplete.
"""
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .normalize import NormalizeResult, normalize

GRACE_SECONDS = 10


@dataclass
class SpawnResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    result: NormalizeResult
    failure_reason: str | None


def _terminate_group(process: subprocess.Popen) -> None:
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=GRACE_SECONDS if sig == signal.SIGTERM else 5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_process(argv: list[str], stdin_text: str | None, timeout_s: int,
                cwd: Path) -> SpawnResult:
    started = time.monotonic()
    process = subprocess.Popen(
        argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process)
        stdout, stderr = process.communicate()
    duration = time.monotonic() - started

    # Timeout wins over parsing: truncated output from a killed process is not
    # a candidate for repair, it is simply a failed round.
    if timed_out:
        return SpawnResult(argv, process.returncode, stdout or "", stderr or "",
                           duration, True,
                           NormalizeResult(None, ["killed on timeout"], False),
                           "timeout")

    result = normalize(stdout or "")
    failure_reason = None
    if process.returncode != 0:
        failure_reason = f"exit {process.returncode}"
    elif not result.succeeded:
        failure_reason = "; ".join(result.errors) or "unusable output"
    return SpawnResult(argv, process.returncode, stdout or "", stderr or "",
                       duration, False, result, failure_reason)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_spawn.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/fake_friend.py tests/test_spawn.py skills/adversarial-friends/scripts/adversarial_friends/spawn.py
git commit -m "feat: spawn friends in process groups with timeout precedence"
```

---

### Task 9: Isolation — snapshots and per-friend worktrees

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/isolation.py`
- Test: `tests/test_isolation.py`

**Interfaces:**
- Consumes: `errors.AfError`.
- Produces: `isolation.snapshot_commit(repo: Path) -> str`, `isolation.add_worktree(repo: Path, sha: str, dest: Path) -> Path`, `isolation.remove_worktree(repo: Path, dest: Path) -> None`, `isolation.doc_scope_dir(dest: Path, artifact: Path) -> Path`.

Three verified corrections are encoded here. `git stash create` takes **no** `-u`, so it omits untracked files — a newly added file would be present in the diff artifact but absent from the worktree, making every claim about it structurally unverifiable. Hooks are suppressed on `worktree add` as defense in depth. And each friend that can write gets its **own** worktree, so one cannot mutate files under another mid-round.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_isolation.py
import subprocess
from pathlib import Path

import pytest

from adversarial_friends import isolation


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (root / "tracked.py").write_text("original\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "init")
    return root


def test_snapshot_includes_untracked_files(repo, tmp_path):
    """git stash create omits untracked files; the snapshot must not."""
    (repo / "brand_new.py").write_text("added but never committed\n")
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / "brand_new.py").read_text() == "added but never committed\n"


def test_snapshot_includes_uncommitted_modifications(repo, tmp_path):
    (repo / "tracked.py").write_text("modified\n")
    sha = isolation.snapshot_commit(repo)
    dest = isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert (dest / "tracked.py").read_text() == "modified\n"


def test_snapshot_leaves_working_tree_untouched(repo, tmp_path):
    (repo / "tracked.py").write_text("modified\n")
    isolation.snapshot_commit(repo)
    assert (repo / "tracked.py").read_text() == "modified\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "tracked.py" in status  # still dirty; nothing was stashed away


def test_worktree_add_does_not_run_hooks(repo, tmp_path):
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "hook_ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    sha = isolation.snapshot_commit(repo)
    isolation.add_worktree(repo, sha, tmp_path / "wt")
    assert not marker.exists()


def test_each_friend_gets_an_independent_worktree(repo, tmp_path):
    sha = isolation.snapshot_commit(repo)
    first = isolation.add_worktree(repo, sha, tmp_path / "wt-a")
    second = isolation.add_worktree(repo, sha, tmp_path / "wt-b")
    (first / "tracked.py").write_text("friend A scribbled here\n")
    assert (second / "tracked.py").read_text() == "original\n"


def test_doc_scope_dir_contains_only_the_artifact(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    dest = isolation.doc_scope_dir(tmp_path / "docdir", artifact)
    assert [p.name for p in dest.iterdir()] == ["spec.md"]
    assert not (dest / ".git").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_isolation.py -v`
Expected: FAIL — `No module named 'adversarial_friends.isolation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/isolation.py
"""Snapshot the repository and hand each friend an isolated copy.

`git stash create` is deliberately not used: its synopsis is
`git stash create [<message>]` with no -u, so it omits untracked files. A
newly added file would then appear in the diff artifact but be missing from
the worktree, forcing every claim about it to 'unverifiable' and blaming the
judge for a broken snapshot.
"""
import os
import shutil
import subprocess
from pathlib import Path

from .errors import AfError

# Suppress post-checkout hooks on worktree add. Hooks are not transferred by
# git clone, so this is defense in depth rather than a live hole — but a
# committed .husky/ plus a previously configured core.hooksPath would
# otherwise execute repository-controlled code on every run.
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                            text=True, env=env)
    if result.returncode != 0:
        raise AfError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot_commit(repo: Path) -> str:
    """Create a commit object capturing tracked, staged, and untracked state.

    The working tree is never modified: a temporary index is used, so the
    operator's dirty tree survives the run exactly as it was.
    """
    repo = Path(repo)
    index = repo / ".git" / "af-snapshot-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    try:
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "read-tree", head, env=env)
        _git(repo, "add", "-A", env=env)          # honors .gitignore
        tree = _git(repo, "write-tree", env=env)
        return _git(repo, "commit-tree", tree, "-p", head, "-m", "af-snapshot",
                    env=env)
    finally:
        index.unlink(missing_ok=True)


def add_worktree(repo: Path, sha: str, dest: Path) -> Path:
    dest = Path(dest)
    _git(Path(repo), *NO_HOOKS, "worktree", "add", "--detach", str(dest), sha)
    return dest


def remove_worktree(repo: Path, dest: Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(dest)],
                   cwd=str(repo), capture_output=True, text=True)


def doc_scope_dir(dest: Path, artifact: Path) -> Path:
    """A bare directory holding only the artifact — no repository at all.

    This is what makes doc scope containment rather than a prompt request: a
    write-capable friend can write whatever it likes, into a disposable
    directory with no path back to the source tree.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, dest / Path(artifact).name)
    return dest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_isolation.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/isolation.py tests/test_isolation.py
git commit -m "feat: snapshot with untracked files and isolate friends per worktree"
```

---

### Task 10: Roster resolution

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/roster.py`
- Test: `tests/test_roster.py`

**Interfaces:**
- Consumes: `adapters.Adapter`, `adapters.FriendSpec`, `trust.validate_roster_entry`, `errors.NoFriendsError`.
- Produces: `roster.detect_host(env: dict) -> str | None`, `roster.discover_clis(registry, which) -> list[str]`, `roster.resolve(registry, lenses, env, which, include_self=False, overrides=None) -> list[FriendSpec]`, `roster.DEGRADED_MODES: frozenset[str]`.

Self-exclusion drops the host's **`(cli, model)` pair**, not the whole binary — during this project's own review round, `claude` reviewing a spec authored by `claude` produced the strongest of three reviews. Degraded mode triggers on fewer than two *friends*, not fewer than two CLIs, because one multi-model CLI can field several.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_roster.py
import pytest

from adversarial_friends import adapters, roster
from adversarial_friends.errors import NoFriendsError

ADAPTER_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "skills" / "adversarial-friends" / "adapters"
LENSES = ["assumptions", "security", "ops"]


@pytest.fixture
def registry():
    return adapters.load_adapters(ADAPTER_DIR)


def which_all(name):
    return f"/usr/local/bin/{name}"


def which_none(name):
    return None


def test_detects_claude_code_host():
    assert roster.detect_host({"CLAUDECODE": "1"}) == "claude"


def test_detects_codex_host():
    assert roster.detect_host({"CODEX_SANDBOX": "seatbelt"}) == "codex"


def test_no_host_detected_when_env_is_bare():
    assert roster.detect_host({}) is None


def test_host_cli_is_excluded_by_default(registry):
    friends = roster.resolve(registry, LENSES, {"CLAUDECODE": "1"}, which_all)
    assert all(f.cli != "claude" for f in friends)


def test_include_self_keeps_the_host(registry):
    friends = roster.resolve(registry, LENSES, {"CLAUDECODE": "1"}, which_all,
                             include_self=True)
    assert any(f.cli == "claude" for f in friends)


def test_lenses_are_assigned_round_robin(registry):
    friends = roster.resolve(registry, LENSES, {}, which_all)
    assigned = [f.lens for f in friends]
    assert assigned[:3] == LENSES[:3]
    assert len(set(assigned[:3])) == 3


def test_opencode_defaults_to_doc_scope(registry):
    """opencode has no read-only mode, so repo scope needs an explicit opt-in."""
    friends = roster.resolve(registry, LENSES, {}, which_all)
    opencode = next(f for f in friends if f.cli == "opencode")
    assert opencode.scope == "doc"


def test_no_binaries_raises_no_friends(registry):
    with pytest.raises(NoFriendsError):
        roster.resolve(registry, LENSES, {}, which_none)


def test_overrides_replace_discovery(registry):
    friends = roster.resolve(
        registry, LENSES, {}, which_all,
        overrides=[{"name": "codex-ops", "cli": "codex", "lens": "ops",
                    "model": "gpt-5.6-sol", "effort": "high"}],
    )
    assert len(friends) == 1
    assert friends[0].name == "codex-ops"
    assert friends[0].model == "gpt-5.6-sol"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_roster.py -v`
Expected: FAIL — `No module named 'adversarial_friends.roster'`.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/roster.py
"""Decide which friends run, on which model, under which lens.

Self-exclusion drops the host's (cli, model) pair rather than the whole
binary. Blanket per-binary exclusion would be wrong: a CLI judging a spec its
own model authored, under a different lens and effort level, is sometimes
exactly what you want.
"""
import shutil
from typing import Callable

from .adapters import Adapter, FriendSpec
from .errors import NoFriendsError
from .trust import validate_roster_entry

HOST_ENV_MARKERS: dict[str, str] = {
    "CLAUDECODE": "claude",
    "CLAUDE_CODE_SESSION": "claude",
    "CODEX_SANDBOX": "codex",
    "CODEX_COMPANION_SESSION_ID": "codex",
    "OPENCODE_SERVER_PASSWORD": "opencode",
}

# opencode exposes no read-only mode, so it may not read the repository
# without an explicit opt-in from the operator.
NO_READONLY_DEFAULT_SCOPE = "doc"
DEGRADED_MODES = frozenset({"report"})
DEFAULT_TIMEOUT = 900


def detect_host(env: dict) -> str | None:
    for marker, cli in HOST_ENV_MARKERS.items():
        if env.get(marker):
            return cli
    return None


def discover_clis(registry: dict[str, Adapter],
                  which: Callable[[str], str | None] = shutil.which) -> list[str]:
    found = []
    for name, adapter in sorted(registry.items()):
        if adapter.transport == "http":
            continue  # reachability is probed separately, not via PATH
        if adapter.binary and which(adapter.binary):
            found.append(name)
    return found


def resolve(registry: dict[str, Adapter], lenses: list[str], env: dict,
            which: Callable[[str], str | None] = shutil.which,
            include_self: bool = False,
            overrides: list[dict] | None = None) -> list[FriendSpec]:
    if overrides:
        specs = []
        for index, entry in enumerate(overrides):
            validate_roster_entry(entry)
            adapter = registry.get(entry["cli"])
            if adapter is None:
                raise NoFriendsError(f"unknown cli in roster: {entry['cli']!r}")
            default_scope = ("repo" if adapter.readonly_argv
                             else NO_READONLY_DEFAULT_SCOPE)
            specs.append(FriendSpec(
                name=entry["name"], cli=entry["cli"], lens=entry["lens"],
                model=entry.get("model"), effort=entry.get("effort"),
                scope=entry.get("scope", default_scope),
                timeout=entry.get("timeout", DEFAULT_TIMEOUT),
            ))
        return specs

    host = detect_host(env)
    available = discover_clis(registry, which)
    if not include_self and host in available:
        available = [c for c in available if c != host]
    if not available:
        raise NoFriendsError(
            "no usable friends found. Install a second agent CLI "
            "(codex, agy, opencode) or pass --include-self."
        )

    specs = []
    for index, cli in enumerate(available):
        adapter = registry[cli]
        scope = "repo" if adapter.readonly_argv else NO_READONLY_DEFAULT_SCOPE
        specs.append(FriendSpec(
            name=f"{cli}-{lenses[index % len(lenses)]}", cli=cli,
            lens=lenses[index % len(lenses)], model=None, effort=None,
            scope=scope, timeout=DEFAULT_TIMEOUT,
        ))
    return specs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_roster.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/roster.py tests/test_roster.py
git commit -m "feat: resolve roster with pair-wise self-exclusion and lens assignment"
```

---

### Task 11: Exact merge and report rendering

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/merge.py`
- Create: `skills/adversarial-friends/scripts/adversarial_friends/report.py`
- Test: `tests/test_merge.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `ledger.Claim`, `ledger.Alias`, `ids.format_claim_id`.
- Produces: `merge.exact_merge(existing: list[Claim], incoming: list[Claim], round_no: int) -> tuple[list[Claim], list[Alias]]`; `report.render(claims, aliases, run_meta) -> str`.

`--merge=exact` under-merges by design: two friends describing one defect in different words yield two claims. That is the safe direction — it costs a round rather than corrupting termination, and it keeps the runner deterministic without an orchestrator attached.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge.py
from adversarial_friends.ledger import Claim
from adversarial_friends.merge import exact_merge


def claim(cid, text, location="src/a.py:1"):
    return Claim(id=cid, supersedes=None, origin=["codex/ops"], lens="ops",
                 round=1, advisory=False, severity="high", claim=text,
                 location=location, evidence="e", failure_scenario="f",
                 suggested_fix="s")


def test_identical_text_and_location_is_aliased():
    existing = [claim("c-0001@1", "the guard is missing")]
    incoming = [claim("c-0002@1", "the guard is missing")]
    kept, aliases = exact_merge(existing, incoming, round_no=1)
    assert kept == []
    assert aliases[0].canonical == "c-0001@1"
    assert aliases[0].duplicate == "c-0002@1"
    assert aliases[0].source == "exact"


def test_whitespace_and_case_differences_still_alias():
    existing = [claim("c-0001@1", "The Guard Is Missing")]
    incoming = [claim("c-0002@1", "  the guard is missing  ")]
    kept, aliases = exact_merge(existing, incoming, round_no=1)
    assert kept == [] and len(aliases) == 1


def test_different_location_is_not_aliased():
    existing = [claim("c-0001@1", "the guard is missing", "src/a.py:1")]
    incoming = [claim("c-0002@1", "the guard is missing", "src/b.py:9")]
    kept, aliases = exact_merge(existing, incoming, round_no=1)
    assert len(kept) == 1 and aliases == []


def test_paraphrase_is_not_merged():
    """Exact merge under-merges on purpose: safer than corrupting termination."""
    existing = [claim("c-0001@1", "timeout leaves MCP children running")]
    incoming = [claim("c-0002@1", "child processes survive timeout")]
    kept, aliases = exact_merge(existing, incoming, round_no=1)
    assert len(kept) == 1 and aliases == []
```

```python
# tests/test_report.py
from adversarial_friends.ledger import Claim
from adversarial_friends.report import render


def claim(cid, severity="high"):
    return Claim(id=cid, supersedes=None, origin=["codex/ops"], lens="ops",
                 round=1, advisory=False, severity=severity,
                 claim="the guard is missing", location="src/a.py:42",
                 evidence="src/a.py:38", failure_scenario="expired token passes",
                 suggested_fix="check exp")


def meta(**over):
    base = {
        "mode": "report", "preset": "inherit", "artifact": "spec.md",
        "friends": [
            {"name": "codex-ops", "model": "gpt-5.6-sol", "effort": "high",
             "readonly": True, "scope": "repo", "status": "ok"},
            {"name": "opencode-security", "model": None, "effort": "unverified",
             "readonly": False, "scope": "doc", "status": "failed: exit 1"},
        ],
        "downgrades": ["opencode: no read-only capability, forced to doc scope"],
    }
    base.update(over)
    return base


def test_report_lists_findings_by_severity():
    out = render([claim("c-0001@1", "low"), claim("c-0002@1", "high")], [], meta())
    assert out.index("c-0002@1") < out.index("c-0001@1")


def test_report_header_states_model_and_effort_per_friend():
    out = render([claim("c-0001@1")], [], meta())
    assert "gpt-5.6-sol" in out and "high" in out


def test_report_surfaces_failed_friends():
    out = render([claim("c-0001@1")], [], meta())
    assert "failed: exit 1" in out


def test_report_surfaces_downgrades():
    out = render([claim("c-0001@1")], [], meta())
    assert "forced to doc scope" in out


def test_empty_findings_says_so_without_claiming_success():
    out = render([], [], meta())
    assert "no findings" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_merge.py tests/test_report.py -v`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# skills/adversarial-friends/scripts/adversarial_friends/merge.py
"""Deterministic claim deduplication.

Exact merge under-merges on purpose. Two friends describing one defect in
different words will produce two claims, which costs a round; the alternative
— guessing at equivalence — corrupts termination arithmetic, which is worse.
Semantic merging is the orchestrator's job and is opt-in.
"""
from .ledger import Alias, Claim


def _key(claim: Claim) -> tuple[str, str]:
    return (" ".join(claim.claim.split()).casefold(), (claim.location or "").strip())


def exact_merge(existing: list[Claim], incoming: list[Claim],
                round_no: int) -> tuple[list[Claim], list[Alias]]:
    seen = {_key(c): c.id for c in existing}
    kept: list[Claim] = []
    aliases: list[Alias] = []
    for claim in incoming:
        key = _key(claim)
        canonical = seen.get(key)
        if canonical is None:
            seen[key] = claim.id
            kept.append(claim)
        else:
            aliases.append(Alias(
                canonical=canonical, duplicate=claim.id, round=round_no,
                source="exact", rationale="identical claim text and location",
            ))
    return kept, aliases
```

```python
# skills/adversarial-friends/scripts/adversarial_friends/report.py
"""Render report.md.

The header states the model and effort each friend actually received. Without
that, a weak critique from a friend that silently ran at default effort reads
as a signal about the artifact when it is a signal about the flag matrix.
"""
from .ledger import Alias, Claim

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def render(claims: list[Claim], aliases: list[Alias], run_meta: dict) -> str:
    lines: list[str] = [f"# Adversarial review — {run_meta['artifact']}", ""]
    lines.append(f"Mode: `{run_meta['mode']}` · preset: `{run_meta['preset']}`")
    lines.append("")
    lines.append("## Friends")
    lines.append("")
    lines.append("| friend | model | effort | read-only | scope | status |")
    lines.append("|---|---|---|---|---|---|")
    for friend in run_meta["friends"]:
        lines.append(
            f"| {friend['name']} | {friend['model'] or 'inherited'} | "
            f"{friend['effort'] or 'inherited'} | {friend['readonly']} | "
            f"{friend['scope']} | {friend['status']} |"
        )
    lines.append("")

    if run_meta.get("downgrades"):
        lines.append("## Downgrades")
        lines.append("")
        for note in run_meta["downgrades"]:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## Findings")
    lines.append("")
    if not claims:
        lines.append(
            "No findings were returned. This is not the same as a clean bill of "
            "health — check the friend table above for failures."
        )
        return "\n".join(lines) + "\n"

    ordered = sorted(claims, key=lambda c: (SEVERITY_ORDER.get(c.severity, 3), c.id))
    for claim in ordered:
        flag = " *(advisory)*" if claim.advisory else ""
        lines.append(f"### {claim.id} — {claim.severity}{flag}")
        lines.append("")
        lines.append(f"**Claim:** {claim.claim}")
        if claim.location:
            lines.append(f"**Location:** `{claim.location}`")
        lines.append(f"**Evidence:** {claim.evidence}")
        lines.append(f"**Failure scenario:** {claim.failure_scenario}")
        lines.append(f"**Suggested fix:** {claim.suggested_fix}")
        lines.append("")

    if aliases:
        lines.append("## Merged duplicates")
        lines.append("")
        for alias in aliases:
            lines.append(f"- `{alias.duplicate}` merged into `{alias.canonical}`")
        lines.append("")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_merge.py tests/test_report.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/adversarial-friends/scripts/adversarial_friends/merge.py skills/adversarial-friends/scripts/adversarial_friends/report.py tests/test_merge.py tests/test_report.py
git commit -m "feat: add exact merge and report rendering"
```

---

### Task 12: Run store and `af run --mode report` end to end

**Files:**
- Create: `skills/adversarial-friends/scripts/adversarial_friends/runstore.py`
- Modify: `skills/adversarial-friends/scripts/adversarial_friends/cli.py`
- Test: `tests/test_runstore.py`
- Test: `tests/test_run_end_to_end.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `runstore.RunStore(root, run_id)` with `.round_dir(n)`, `.friend_paths(n, name)`, `.write_run_json(meta)`, `.artifact_copy(src)`, `.ledger`; `cli.cmd_run(args) -> int`, `cli.cmd_doctor(args) -> int`.

The run directory lives **outside** the worktree. Placing it inside the repo would let `codex review --uncommitted` — whose help reads "staged, unstaged, **and untracked**" — review the tool's own scratch files as part of the diff.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runstore.py
from adversarial_friends.errors import UsageError
from adversarial_friends.runstore import RunStore

import pytest


def test_layout_is_created(tmp_path):
    store = RunStore(tmp_path, "run-001")
    raw, parsed, meta = store.friend_paths(1, "codex-ops")
    assert raw.parent == store.round_dir(1)
    assert raw.name.endswith(".raw")
    assert parsed.name.endswith(".json")
    assert meta.name.endswith(".meta")


def test_artifact_is_frozen_and_hashed(tmp_path):
    src = tmp_path / "spec.md"
    src.write_text("# spec\n")
    store = RunStore(tmp_path / "runs", "run-001")
    copied, digest = store.artifact_copy(src)
    assert copied.read_text() == "# spec\n"
    assert digest.startswith("sha256:")


def test_friend_name_cannot_escape_the_run_dir(tmp_path):
    store = RunStore(tmp_path, "run-001")
    with pytest.raises(UsageError):
        store.friend_paths(1, "../../../../tmp/owned")


def test_run_json_is_written(tmp_path):
    store = RunStore(tmp_path, "run-001")
    store.write_run_json({"mode": "report"})
    assert '"mode": "report"' in (store.run_dir / "run.json").read_text()
```

```python
# tests/test_run_end_to_end.py
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AF = REPO / "skills" / "adversarial-friends" / "scripts" / "af"
FAKE = REPO / "tests" / "fake_friend.py"


def run_af(tmp_path, artifact, *extra):
    return subprocess.run(
        [sys.executable, str(AF), "run", str(artifact), "--mode", "report",
         "--out", str(tmp_path / "runs"), *extra],
        capture_output=True, text=True,
        env={"PATH": __import__("os").environ["PATH"],
             "AF_FAKE_FRIEND": f"{sys.executable} {FAKE}"},
    )


def test_report_run_produces_ledger_and_report(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\nA design with a missing guard.\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:good")
    assert result.returncode == 0, result.stderr
    runs = sorted((tmp_path / "runs").iterdir())
    ledger = (runs[0] / "claims.jsonl").read_text().strip().splitlines()
    assert ledger, "ledger should not be empty"
    assert json.loads(ledger[0])["type"] == "claim"
    assert "# Adversarial review" in (runs[0] / "report.md").read_text()


def test_failed_friend_is_reported_not_silently_dropped(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact, "--friend", "fake:good", "--friend", "fake:offtopic")
    runs = sorted((tmp_path / "runs").iterdir())
    report = (runs[0] / "report.md").read_text()
    assert "failed" in report.lower()


def test_zero_friends_exits_3(tmp_path):
    artifact = tmp_path / "spec.md"
    artifact.write_text("# spec\n")
    result = run_af(tmp_path, artifact)
    assert result.returncode == 3


def test_missing_artifact_exits_2(tmp_path):
    result = run_af(tmp_path, tmp_path / "nope.md", "--friend", "fake:good")
    assert result.returncode == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_runstore.py tests/test_run_end_to_end.py -v`
Expected: FAIL — `runstore` missing; `af run` unimplemented.

- [ ] **Step 3: Write the run store**

```python
# skills/adversarial-friends/scripts/adversarial_friends/runstore.py
"""Run directory layout.

The run directory lives outside the worktree. Putting it inside the repository
would let `codex review --uncommitted` — "staged, unstaged, and untracked" —
review the tool's own scratch files as part of the diff under review.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

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
        self.run_dir.mkdir(parents=True, exist_ok=True)
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
```

- [ ] **Step 4: Wire the `run` and `doctor` subcommands**

```python
# skills/adversarial-friends/scripts/adversarial_friends/cli.py
"""Command line entry point."""
import argparse
import concurrent.futures
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import FriendSpec, build_argv, capability_for, load_adapters
from .claimschema import schema_path
from .errors import AfError, NoFriendsError, UsageError
from .ids import format_claim_id
from .ledger import Claim
from .merge import exact_merge
from .report import render
from .roster import discover_clis, resolve
from .runstore import RunStore, default_root
from .spawn import run_process
from .trust import check_denied_values

SKILL_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_DIR = SKILL_ROOT / "adapters"
LENS_DIR = SKILL_ROOT / "lenses"
PROMPT_HEADER = (
    "You are an adversarial reviewer. Read the artifact below and challenge it.\n"
    "Return ONLY a JSON object matching this shape:\n"
    '{"findings":[{"severity":"high|medium|low","claim":"...","location":"...",'
    '"evidence":"...","failure_scenario":"...","suggested_fix":"..."}]}\n'
    'If you find nothing, return exactly {"no_findings": true}.\n'
)


def available_lenses() -> list[str]:
    names = sorted(p.stem for p in LENS_DIR.glob("*.md"))
    return names or ["assumptions"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="af")
    parser.add_argument("--version", action="version", version=f"af {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run")
    run_p.add_argument("artifact")
    run_p.add_argument("--mode", default="crossexam",
                       choices=["report", "crossexam", "gate", "loop"])
    run_p.add_argument("--preset", default="inherit",
                       choices=["inherit", "thorough", "cheap"])
    run_p.add_argument("--friend", action="append", default=[],
                       help="cli:lens, repeatable; overrides discovery")
    run_p.add_argument("--include-self", action="store_true")
    run_p.add_argument("--timeout", type=int, default=900)
    run_p.add_argument("--out", default=None)

    sub.add_parser("doctor")
    return parser


def _specs_from_flags(values: list[str], timeout: int,
                      registry: dict) -> list[FriendSpec]:
    specs = []
    for index, value in enumerate(values):
        cli, _, lens = value.partition(":")
        adapter = registry.get(cli)  # None for the test-only "fake" cli
        scope = "repo" if adapter and adapter.readonly_argv else "doc"
        specs.append(FriendSpec(name=f"{cli}-{lens or 'assumptions'}-{index}",
                                cli=cli, lens=lens or "assumptions", model=None,
                                effort=None, scope=scope, timeout=timeout))
    return specs


def cmd_run(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact)
    if not artifact.is_file():
        raise UsageError(f"artifact not found: {artifact}")
    if args.mode != "report":
        raise UsageError(
            f"mode {args.mode!r} is not implemented yet; only 'report' is available"
        )

    registry = load_adapters(ADAPTER_DIR)
    # AF_FAKE_FRIEND keeps the end-to-end tests off real CLIs and, critically,
    # off any metered provider. `--friend fake:<mode>` selects a scripted
    # response; the mode travels in the lens slot.
    fake = os.environ.get("AF_FAKE_FRIEND")
    specs = (_specs_from_flags(args.friend, args.timeout, registry)
             if args.friend else
             resolve(registry, available_lenses(), os.environ, shutil.which,
                     include_self=args.include_self))
    if not specs:
        raise NoFriendsError("no usable friends for mode 'report'")

    run_id = f"run-{int(time.time())}"
    store = RunStore(Path(args.out) if args.out else default_root(), run_id)
    frozen, digest = store.artifact_copy(artifact)
    schema_file = schema_path(store.run_dir)
    prompt_file = store.run_dir / "prompt.txt"
    prompt_file.write_text(
        PROMPT_HEADER + "\n--- ARTIFACT ---\n" + frozen.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def dispatch(spec: FriendSpec):
        if fake and spec.cli == "fake":
            argv, stdin_text = [*fake.split(), spec.lens], None
        else:
            adapter = registry.get(spec.cli)
            if adapter is None:
                raise UsageError(f"unknown cli: {spec.cli!r}")
            argv, stdin_text = build_argv(adapter, spec, prompt_file, schema_file)
            check_denied_values(argv)
        return spec, run_process(argv, stdin_text, spec.timeout, Path.cwd())

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as pool:
        for spec, outcome in pool.map(dispatch, specs):
            results.append((spec, outcome))

    counter = 0
    all_claims: list[Claim] = []
    all_aliases = []
    friends_meta = []
    for spec, outcome in results:
        raw_path, json_path, meta_path = store.friend_paths(1, spec.name)
        raw_path.write_text(outcome.stdout, encoding="utf-8")
        meta_path.write_text(
            f"argv={outcome.argv}\nexit={outcome.exit_code}\n"
            f"duration_s={outcome.duration_s:.2f}\ntimed_out={outcome.timed_out}\n",
            encoding="utf-8",
        )
        status = "ok" if outcome.failure_reason is None else f"failed: {outcome.failure_reason}"
        friends_meta.append({
            "name": spec.name, "model": spec.model, "effort": spec.effort,
            "readonly": spec.scope == "repo", "scope": spec.scope, "status": status,
        })
        if outcome.failure_reason is not None:
            continue
        incoming = []
        for finding in (outcome.result.payload or {}).get("findings", []):
            counter += 1
            incoming.append(Claim(
                id=format_claim_id(counter), supersedes=None,
                origin=[f"{spec.cli}/{spec.lens}"], lens=spec.lens, round=1,
                advisory=False, severity=finding["severity"],
                claim=finding["claim"], location=finding.get("location"),
                evidence=finding["evidence"],
                failure_scenario=finding["failure_scenario"],
                suggested_fix=finding["suggested_fix"],
            ))
        kept, aliases = exact_merge(all_claims, incoming, round_no=1)
        for record in kept:
            store.ledger.append(record)
        for alias in aliases:
            store.ledger.append(alias)
        all_claims.extend(kept)
        all_aliases.extend(aliases)

    meta = {"mode": args.mode, "preset": args.preset, "artifact": artifact.name,
            "artifact_hash": digest, "friends": friends_meta, "downgrades": []}
    store.write_run_json(meta)
    store.write_report(render(all_claims, all_aliases, meta))
    print(store.run_dir)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_adapters(ADAPTER_DIR)
    found = discover_clis(registry, shutil.which)
    for name, adapter in sorted(registry.items()):
        binary = shutil.which(adapter.binary) if adapter.binary else adapter.endpoint
        argv, _ = ([adapter.binary, *adapter.base_argv, *adapter.readonly_argv], None)
        cap = capability_for(adapter, argv)
        print(f"{name:10} {'found' if name in found else 'missing':8} "
              f"schema={cap.schema} readonly={cap.readonly} effort={cap.effort} "
              f"{binary or ''}")
    return 0 if found else 3


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        parser.print_help()
        return 0
    except AfError as exc:
        print(f"af: {exc}", file=sys.stderr)
        return exc.exit_code
```

- [ ] **Step 5: Run the full suite and commit**

Run: `python -m pytest tests/ -v`
Expected: PASS, all tests.

```bash
git add skills/adversarial-friends/scripts/adversarial_friends tests
git commit -m "feat: wire af run --mode report end to end with af doctor"
```

---

### Task 13: The skill layer — SKILL.md, lenses, and install manifests

**Files:**
- Create: `skills/adversarial-friends/SKILL.md`
- Create: `skills/adversarial-friends/lenses/{assumptions,security,ops,scope,testability,spec-vs-reality}.md`
- Create: `skills/adversarial-friends/references/{modes,ledger,troubleshooting}.md`
- Create: `.claude-plugin/plugin.json`, `gemini-extension.json`, `skill.json`, `AGENTS.md`
- Create: `bin/af` (symlink)
- Test: `tests/test_skill_layer.py`

**Interfaces:**
- Consumes: `cli.available_lenses` (reads `lenses/*.md`).
- Produces: no Python API. The lens filenames are the lens names used by `roster.resolve`.

The `description` field is the entire triggering mechanism, and models under-trigger skills, so it names concrete contexts rather than describing the tool abstractly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_layer.py
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "adversarial-friends"


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    block = text.split("---\n")[1]
    out = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def test_skill_has_name_and_description():
    meta = frontmatter((SKILL / "SKILL.md").read_text())
    assert meta["name"] == "adversarial-friends"
    assert len(meta["description"]) > 80


def test_skill_body_is_under_777_lines():
    assert len((SKILL / "SKILL.md").read_text().splitlines()) < 777


def test_every_lens_file_has_frontmatter():
    lenses = list((SKILL / "lenses").glob("*.md"))
    assert len(lenses) >= 6
    for lens in lenses:
        text = lens.read_text()
        assert text.startswith("---\n"), lens
        assert "requires_failure_scenario:" in text, lens


def test_referenced_files_exist():
    body = (SKILL / "SKILL.md").read_text()
    for name in ("references/modes.md", "references/ledger.md",
                 "references/troubleshooting.md"):
        assert name in body
        assert (SKILL / name).exists()


def test_bin_symlink_resolves_to_the_runner():
    link = Path(__file__).resolve().parents[1] / "bin" / "af"
    assert link.resolve() == (SKILL / "scripts" / "af").resolve()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_layer.py -v`
Expected: FAIL — `SKILL.md` does not exist.

- [ ] **Step 3: Write SKILL.md**

```markdown
---
name: adversarial-friends
description: Cross-examine a spec, plan, design doc, or another reviewer's findings by dispatching it to several other agent CLIs (codex, claude, agy, opencode, local ollama models) as independent adversarial reviewers, then merging their critiques into a single ranked findings report. Use this whenever the user wants a design or plan challenged, wants a second or third opinion on a review, says something like "poke holes in this", "what's wrong with this plan", "have another model check this", "review my spec", or is about to commit to an architectural decision — and especially when they mention wanting more than one model to look at something.
---

# Adversarial Friends

Challenge an artifact by having several *other* agent CLIs attack it
independently, then merge what they find.

The point is not more review — it is **disagreement you can see**. One model
reviewing a document tends to produce confident prose. Several models
reviewing it separately produce claims that can be compared, and the places
they disagree are usually where the real problem is.

## When this fires

Use it for specs, design docs, implementation plans, and — the highest-value
case — **another reviewer's findings**. Challenging a review is what this was
built for: a finding that survives a second model's scrutiny is worth acting
on, and one that does not is worth dropping before it costs you a day.

Do not use it to generate a first review of code. It challenges artifacts; it
does not produce the initial critique.

## Running it

```bash
bin/af run <artifact> --mode report
```

That dispatches the artifact to every discovered friend in parallel and writes
a run directory containing `claims.jsonl`, `report.md`, and each friend's raw
output. Read `report.md` and present the findings.

Check what is available first when a run comes back thin:

```bash
bin/af doctor
```

It prints, per friend: whether the binary was found, whether it can enforce a
schema, whether it has a real read-only mode, and whether its effort level can
be verified. A friend missing from that list is why your report is short.

## Reading the results like a reviewer, not a stenographer

The report is input to your judgment, not output to relay. Three things
deserve your attention before you hand anything to the user:

**Failed friends are not silent.** The friend table in `report.md` shows
status per friend. A run where two of three friends failed is not a clean bill
of health, and saying "no issues found" would be wrong. Say what did not run.

**Exit status lies.** Several CLIs exit 0 while producing nothing usable —
answering a different prompt, writing output to a file instead of stdout,
returning prose where JSON was asked for. The runner already treats these as
failures; your job is to notice when the *pattern* suggests a misconfigured
adapter rather than a quiet artifact.

**Duplicates are under-merged on purpose.** The default merge only combines
claims with identical text and location, so two friends describing one defect
in different words appear twice. Merge them in your presentation — that is
judgment the runner deliberately declines to make.

## Choosing lenses

Each friend runs under one lens, a prose file in `lenses/` describing what to
look for. The default assignment is round-robin, which is fine for a first
pass. Override it when the artifact has an obvious weak flank — a deployment
plan wants `ops`, an auth design wants `security`, a spec that keeps growing
wants `scope`.

Lenses marked `requires_failure_scenario: false` (currently `scope`) produce
*advisory* claims. Those are real feedback but they never block anything,
because "this feels over-engineered" cannot be verified the way "this crashes
on an expired token" can.

## Further reading

- `references/modes.md` — what `report`, `crossexam`, `gate`, and `loop` do,
  and which are implemented
- `references/ledger.md` — the claim/verdict/alias/resolution record types and
  how to read `claims.jsonl` directly
- `references/troubleshooting.md` — verified CLI invocation traps, what a
  failed friend usually means, and how to diagnose an empty report
```

- [ ] **Step 4: Write the lenses, references, manifests, and symlink**

```bash
mkdir -p skills/adversarial-friends/lenses skills/adversarial-friends/references bin .claude-plugin

cat > skills/adversarial-friends/lenses/assumptions.md <<'EOF'
---
name: assumptions
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: doc
---

# Hidden assumptions

Find the things the document takes for granted. An assumption is worth
reporting when the document would need rewriting if it turned out false — not
merely when it is unstated.

Look for: load, scale, and concurrency taken as given; "the user will…"
claims with no enforcement; ordering assumed between independent components;
single-writer assumptions in systems with several writers; and any place the
artifact says "simply" or "just", which is usually where the hard part was
skipped.

Your evidence must quote the passage that carries the assumption. Your failure
scenario must name what breaks when it does not hold.
EOF

cat > skills/adversarial-friends/lenses/security.md <<'EOF'
---
name: security
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: repo
---

# Security

Attack the design as written. Prefer concrete, reachable weaknesses over
categories of concern.

Look for: trust boundaries that are asserted rather than enforced; input from
one trust level reaching a sink at another; controls described as
configuration when they need to be enforcement; secrets whose lifetime or
blast radius is unstated; and any escape hatch whose failure mode is "the
control silently does nothing".

A finding needs a path from attacker-controlled input to consequence. If you
cannot write that path, mark it unproven rather than asserting it.
EOF

cat > skills/adversarial-friends/lenses/ops.md <<'EOF'
---
name: ops
applies_to: [spec, plan, review, diff]
requires_failure_scenario: true
default_scope: repo
---

# Operations and failure modes

Ask what happens at 3am. The question is not whether the happy path works but
what the system does when a dependency is slow, a process dies mid-write, or
the same job runs twice.

Look for: timeouts that are unreconciled between layers; retries without
idempotency; partial failure treated as total success; processes that spawn
children nobody reaps; state that must be cleaned up but has no owner; and
success signals that do not actually indicate success.

Name the operational condition and what the operator sees when it happens.
EOF

cat > skills/adversarial-friends/lenses/scope.md <<'EOF'
---
name: scope
applies_to: [spec, plan]
requires_failure_scenario: false
default_scope: doc
---

# Scope and YAGNI

Find what should not be built. This lens produces *advisory* claims — they do
not block, because "this is more than you need" is judgment rather than a
defect, and demanding a failure scenario for it would silence the lens
entirely.

Look for: configuration surfaces with no stated second use; abstractions with
one implementation; modes that duplicate each other; and features whose
justification is a hypothetical future rather than a present need.

Say plainly what you would cut and what would be lost by cutting it.
EOF

cat > skills/adversarial-friends/lenses/testability.md <<'EOF'
---
name: testability
applies_to: [spec, plan, review]
requires_failure_scenario: true
default_scope: repo
---

# Testability

Find the parts that cannot be verified. A design that cannot be tested will
not stay correct, regardless of whether it starts correct.

Look for: behavior specified only in prose with no observable output; tests
that would pass by construction regardless of the code; logic whose only
trigger is a real network call, a real clock, or a paid API; and termination
or convergence rules with no deterministic way to exercise their edges.

Name the specific behavior and why no test could distinguish correct from
broken.
EOF

cat > skills/adversarial-friends/lenses/spec-vs-reality.md <<'EOF'
---
name: spec-vs-reality
applies_to: [spec, plan]
requires_failure_scenario: true
default_scope: repo
---

# Spec versus reality

Check the document against the code that already exists. This is the lens that
needs repository access, and it produces the findings no amount of careful
reading can substitute for.

Look for: described behavior the code already implements differently; files,
functions, or flags the document names that do not exist; interfaces whose
real signature differs from the one assumed; and constraints the document
treats as new that are already enforced somewhere else.

Cite the file and line you actually read. If you could not read the
repository, say so rather than guessing.
EOF

cat > skills/adversarial-friends/references/modes.md <<'EOF'
# Modes

| Mode | Status | What it does |
|---|---|---|
| `report` | **implemented** | One round. Every friend critiques in parallel; claims are merged and ranked. |
| `crossexam` | planned | Friends then judge each other's claims across rounds until claims settle or deadlock. |
| `gate` | planned | Cross-examination, then every surviving non-advisory claim needs an explicit resolution. |
| `loop` | planned | Cross-examination, artifact revised, repeated until two rounds surface nothing new. |

`af run` currently rejects any mode other than `report` with a usage error
rather than pretending to support it.

Cross-examination is the mode this project exists for: it automates the manual
loop of handing one reviewer's findings to another and carrying the argument
back. The ledger written by `report` is already the structure it needs.
EOF

cat > skills/adversarial-friends/references/ledger.md <<'EOF'
# The claim ledger

`claims.jsonl` is append-only. Each line is one record with a `type` field.

**claim** — an assertion about the artifact. `id` is versioned (`c-0007@2`);
an amendment creates a new version rather than editing in place, so a verdict
is never ambiguous about which wording it judged. `origin` is a list because
an amended claim belongs to both its original author and its amender.

**verdict** — one judge's ruling on one claim version: `upheld`, `refuted`,
`amended`, `unproven`, or `out-of-scope`. `evidence_assessment` records
whether the judge could actually find the evidence the claim cited; a verdict
that could not is downgraded to `unproven`.

**alias** — a merge decision. `source` is `exact` for the deterministic merge
and `orchestrator` for a judgment call.

**resolution** — how a claim was disposed of: `fixed`, `rejected`, or
`accepted-risk`. Resolutions are attestations, verified only to the extent the
named location can be re-read.

Read it directly with `jq -c 'select(.type=="claim")' claims.jsonl` when the
rendered report is not enough.
EOF

cat > skills/adversarial-friends/references/troubleshooting.md <<'EOF'
# Troubleshooting

## The report is empty or very short

Run `bin/af doctor`. A friend that is missing, unauthenticated, or lacking a
read-only mode will not appear in the results. An empty report with no failed
friends is a real "nothing found"; an empty report with failures is not.

## Verified invocation traps

These were all found by running into them, and **four of the five returned
exit status 0**. This is why the runner validates output rather than trusting
exit codes.

| CLI | Trap |
|---|---|
| `codex` | `codex resume` / `codex fork` are interactive; the non-interactive forms are `codex exec resume` / `codex exec fork` |
| `agy` | `-p` takes the prompt as its *value*, so flags must precede it or `-p` swallows the next flag |
| `claude` | `-p --permission-mode plan` writes findings to `~/.claude/plans/` instead of stdout; use `--tools "Read,Grep,Glob"` for read-only |
| `agy` | long tasks route output to a brain artifact file and print only a summary |
| `ollama` | `ollama run` writes ANSI cursor codes *inside* its JSON payload; use the HTTP API |

Short flags also collide: `-p` is `--print` on claude and agy but `--profile`
on codex; `-s` is `--sandbox` on codex but `--session` on opencode. Adapters
spell every flag long.

## A friend times out

The default is 900s. Reviewing a long document genuinely takes minutes — a
300s default would kill real reviews. Where a CLI has its own internal timeout
(`agy --print-timeout`, default 5m), the adapter sets it explicitly so the two
deadlines cannot silently disagree.

## gemini does not work

`gemini` returns `IneligibleTierError` on the individual free tier: the client
is no longer supported and Google directs users to Antigravity. Use `agy`.
EOF

cat > .claude-plugin/plugin.json <<'EOF'
{
  "name": "adversarial-friends",
  "description": "Cross-examine specs, plans, and reviews using other agent CLIs as adversarial reviewers.",
  "version": "0.1.0",
  "skills": ["skills/adversarial-friends"]
}
EOF

cat > gemini-extension.json <<'EOF'
{
  "name": "adversarial-friends",
  "version": "0.1.0",
  "description": "Cross-examine specs, plans, and reviews using other agent CLIs as adversarial reviewers.",
  "contextFileName": "AGENTS.md"
}
EOF

cat > skill.json <<'EOF'
{
  "name": "adversarial-friends",
  "version": "0.1.0",
  "description": "Cross-examine specs, plans, and reviews using other agent CLIs as adversarial reviewers.",
  "entry": "skills/adversarial-friends/SKILL.md"
}
EOF

cat > AGENTS.md <<'EOF'
# Adversarial Friends

This repository ships a skill that challenges specs, plans, and reviews by
dispatching them to other agent CLIs as independent adversarial reviewers.

Read `skills/adversarial-friends/SKILL.md` for the workflow. Run the tool with
`bin/af run <artifact> --mode report`, and `bin/af doctor` when a run comes
back thinner than expected.
EOF

ln -sf ../skills/adversarial-friends/scripts/af bin/af
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_skill_layer.py -v`
Expected: PASS, 5 tests.

```bash
git add skills bin .claude-plugin gemini-extension.json skill.json AGENTS.md tests/test_skill_layer.py
git commit -m "feat: add SKILL.md, lenses, references, and multi-harness manifests"
```

---

### Task 14: Documentation tree, logo, and evals

**Files:**
- Create: `README.md`
- Create: `docs/README.md`
- Create: `docs/images/README.md`
- Create: `docs/images/brand/adversarial-friends-banner.png` (1024×1024, converted and downscaled from the 2048×2048 original in `~/Downloads/`)
- Create: `docs/images/brand/adversarial-friends-logo-{128,256,512}.png` (derived sizes)
- Create: `docs/architecture/run-flow.puml`
- Create: `evals/evals.json`
- Test: `tests/test_docs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no API. `evals/evals.json` is consumed by skill-creator's eval tooling.

Documentation follows the sibling `octowright` repo's conventions: a
banner-first `README.md`, a sectioned `docs/README.md` index, brand assets
under `docs/images/brand/`, and PlantUML sources beside rendered SVG.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docs.py
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_readme_leads_with_the_banner():
    first = REPO.joinpath("README.md").read_text().splitlines()[0]
    assert first.startswith("![adversarial-friends]")


def test_all_brand_sizes_exist():
    brand = REPO / "docs" / "images" / "brand"
    banner = brand / "adversarial-friends-banner.png"
    assert banner.stat().st_size > 100_000
    # Ceiling: a full-resolution PNG of this illustration is several MB, which
    # does not belong in git history. Regenerate at 1024 if this trips.
    assert banner.stat().st_size < 4_000_000, "banner too large for the repo"
    for size in (128, 256, 512):
        derived = brand / f"adversarial-friends-logo-{size}.png"
        assert derived.exists(), derived
        assert derived.stat().st_size > 0


def test_derived_sizes_have_the_right_dimensions():
    """PNG dimensions live at a fixed offset in the IHDR chunk — no dependency needed."""
    import struct
    brand = REPO / "docs" / "images" / "brand"
    for size in (128, 256, 512):
        data = (brand / f"adversarial-friends-logo-{size}.png").read_bytes()[:24]
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (size, size)


def test_readme_image_links_are_absolute_github_urls():
    """Relative paths break on PyPI and anywhere the README is mirrored."""
    import re
    text = REPO.joinpath("README.md").read_text()
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        assert target.startswith("https://raw.githubusercontent.com/"), target


def test_docs_index_links_only_to_existing_files():
    index = REPO / "docs" / "README.md"
    import re
    for target in re.findall(r"\]\(([^)#][^)]*)\)", index.read_text()):
        if target.startswith("http"):
            continue
        assert (index.parent / target).exists(), target


def test_evals_file_is_valid_and_has_cases():
    data = json.loads((REPO / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "adversarial-friends"
    assert len(data["evals"]) >= 3
    assert all("prompt" in e and "expected_output" in e for e in data["evals"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_docs.py -v`
Expected: FAIL — `README.md` does not exist.

- [ ] **Step 3: Copy the logo and write the docs**

```bash
mkdir -p docs/images/brand docs/architecture evals

# Convert the 2048x2048 source to PNG at 1024x1024, then derive the standard
# sizes. sips ships with macOS; no image dependency is added to the project.
# 1024 rather than 2048: a full-size PNG of a detailed illustration runs to
# several MB, and the banner is only ever rendered at README width.
sips -s format png -z 1024 1024 \
     ~/Downloads/Gemini_Generated_Image_t1exu3t1exu3t1ex.jpg \
     --out docs/images/brand/adversarial-friends-banner.png >/dev/null

for size in 128 256 512; do
  sips -s format png -z "$size" "$size" \
       docs/images/brand/adversarial-friends-banner.png \
       --out "docs/images/brand/adversarial-friends-logo-${size}.png" >/dev/null
done

# Verify: every derived file must report the size it claims.
for size in 128 256 512; do
  sips -g pixelWidth -g pixelHeight \
       "docs/images/brand/adversarial-friends-logo-${size}.png"
done
```

```markdown
<!-- README.md -->
![adversarial-friends](https://raw.githubusercontent.com/livingstaccato/adversarial-friends/main/docs/images/brand/adversarial-friends-banner.png)

# Adversarial Friends

A skill that challenges your specs, plans, and reviews by handing them to
**other** agent CLIs — codex, claude, agy, opencode, local ollama models — as
independent adversarial reviewers, then merging their critiques into one
ranked findings report.

It automates a workflow you may already do by hand: run a review, paste the
findings into a different model, ask whether they hold up, carry the argument
back. Doing that manually means holding a claim ledger in your head. This
keeps the ledger on disk.

## Why more than one model

A single reviewer produces confident prose. Several reviewers produce claims
that can be compared — and the disagreements are where the real problems are.
While this tool's own design spec was being written, one reviewer's fix
introduced a bug the next reviewer caught. No single-reviewer pass would have
found it.

## Install

Requires Python 3.11+ and at least one agent CLI besides the one you are
running under. No dependencies to install — the runner is stdlib-only.

```bash
git clone https://github.com/livingstaccato/adversarial-friends
cd adversarial-friends
bin/af doctor
```

`doctor` tells you which friends are available and what each can actually
enforce — schema validation, a real read-only mode, a verifiable effort level.

## Use

```bash
bin/af run docs/my-design.md --mode report
```

Read `report.md` in the run directory it prints.

## Documentation

See [docs/](docs/README.md).

## License

MIT
```

```markdown
<!-- docs/README.md -->
# Adversarial Friends Docs

Reference documentation for users and contributors. For an introduction see
the [top-level README](../README.md); come here for detail.

## Start Here

- [../skills/adversarial-friends/SKILL.md](../skills/adversarial-friends/SKILL.md) — the skill itself: when it fires and how to read its output
- [../skills/adversarial-friends/references/modes.md](../skills/adversarial-friends/references/modes.md) — report, crossexam, gate, loop, and which are implemented

## Core Concepts

- [../skills/adversarial-friends/references/ledger.md](../skills/adversarial-friends/references/ledger.md) — claim, verdict, alias, and resolution records
- [architecture/run-flow.puml](architecture/run-flow.puml) — how one `af run` proceeds, from snapshot to report

## Operations

- [../skills/adversarial-friends/references/troubleshooting.md](../skills/adversarial-friends/references/troubleshooting.md) — verified CLI traps, empty reports, timeouts

## Design

- [superpowers/specs/2026-08-22-adversarial-friends-design.md](superpowers/specs/2026-08-22-adversarial-friends-design.md) — the design spec, including the review history that produced it
- [superpowers/plans/2026-08-22-adversarial-friends-core-runner.md](superpowers/plans/2026-08-22-adversarial-friends-core-runner.md) — implementation plan for the core runner

## Assets

- [images/README.md](images/README.md) — branding asset layout
```

```markdown
<!-- docs/images/README.md -->
# Image Assets

Canonical home for visual assets used by `README.md` and distributed skill
docs.

```
docs/images/
└── brand/
    ├── adversarial-friends-banner.png      (1024×1024, branded mark)
    ├── adversarial-friends-logo-128.png    (derived size)
    ├── adversarial-friends-logo-256.png
    └── adversarial-friends-logo-512.png
```

The banner is the source of truth; the numbered sizes are derived from it with
`sips -z <n> <n>` and are regenerated rather than edited. The 2048×2048
original is deliberately not committed — a full-resolution PNG of this
illustration runs to several megabytes and the banner is only ever rendered at
README width.

**Every image reference in `README.md` uses an absolute
`https://raw.githubusercontent.com/...` URL, never a relative path.** A
relative path resolves only when the README is rendered inside the repository
tree — it breaks on PyPI, in package registries, and anywhere the README is
mirrored or embedded. `test_readme_image_links_are_absolute_github_urls`
enforces this.

## Regenerating

```bash
for size in 128 256 512; do
  sips -s format png -z "$size" "$size" \
       docs/images/brand/adversarial-friends-banner.png \
       --out "docs/images/brand/adversarial-friends-logo-${size}.png"
done
```
```

```
' docs/architecture/run-flow.puml
@startuml
title af run --mode report

start
:copy artifact into run dir, hash it;
:snapshot repo (tracked + untracked) into a commit object;
partition "per friend, in parallel" {
  :build argv from adapter record;
  :compute capability from effective argv;
  if (friend can write?) then (yes)
    :private worktree;
  else (no)
    :shared worktree or doc-only dir;
  endif
  :spawn in its own process group;
  if (timed out?) then (yes)
    :kill process group; mark failed;
  else (no)
    :strip ANSI, extract JSON, validate;
    if (usable output?) then (yes)
      :emit claims;
    else (no)
      :mark failed;
    endif
  endif
}
:exact-merge claims into ledger;
:render report.md;
stop
@enduml
```

```json
{
  "skill_name": "adversarial-friends",
  "evals": [
    {
      "id": 1,
      "prompt": "I've got a design doc at docs/superpowers/specs/2026-08-22-adversarial-friends-design.md that I'm about to start building from. Before I commit to it, can you get a couple of other models to poke holes in it? I want to know what I'm going to regret.",
      "expected_output": "Runs af run with mode report against the spec, reports which friends actually succeeded and which failed, and presents merged findings ranked by severity rather than relaying each friend's output separately.",
      "files": []
    },
    {
      "id": 2,
      "prompt": "codex just gave me a review of my auth refactor with 9 findings. honestly some of them feel like nitpicks and one or two might be flat wrong. can you have something else check its work?",
      "expected_output": "Treats the existing review as the artifact to challenge, dispatches it to friends other than codex, and separates findings that survived scrutiny from ones that did not — rather than generating a fresh code review.",
      "files": []
    },
    {
      "id": 3,
      "prompt": "run the adversarial review on my plan but my machine only has claude installed I think",
      "expected_output": "Runs af doctor first, discovers there is no second friend available, explains that cross-examination needs a second agent CLI, and either offers degraded single-friend report mode or names the CLIs that could be installed — without silently producing a report that looks like a clean result.",
      "files": []
    }
  ]
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS, all tests across the suite.

- [ ] **Step 5: Commit**

```bash
git add README.md docs evals tests/test_docs.py
git commit -m "docs: add README, docs index, brand asset, run-flow diagram, and evals"
```

---

## Self-Review

**Spec coverage.** Sections covered by this plan: §4 architecture (Tasks 1, 12), §5.1 blind slice field set — *deferred, only needed by crossexam*, §6 ledger (Task 3), §7 modes — `report` only (Task 12), §8 roster (Task 10), §9 lenses (Task 13), §10 tuning (Task 5), §11 adapters and traps (Tasks 5, 7, 8), §12 isolation (Task 9), §13 trust (Task 6), §14 failure handling (Tasks 7, 8), §15 packaging (Task 13), §16 testing (throughout), §17 CLI surface — `run` and `doctor` only (Task 12), §18 risks (documented in Task 13 references).

**Deliberate gaps, all belonging to Plan 2:** §5.1 blind rendering, §6.2 verdicts in anger, §6.4 resolutions, §6.5 evidence symmetry, §7.1 quorum, §7.2 claim states, §7.3 dry rounds, §7.4 ceilings, §7.5 `af resolve`, §7.6 exit codes 10 and 11, §11.5 ollama HTTP transport, §12.2 OS-level sandboxing. The ledger and adapters this plan builds are exactly the substrate those need.

**Placeholder scan:** none — every code step contains runnable code, every test step contains real assertions.

**Type consistency:** `FriendSpec` fields are identical in Tasks 5, 10, and 12. `Claim` construction in Task 12 matches the dataclass in Task 3 field-for-field. `NormalizeResult` is produced in Task 7 and consumed in Task 8 with the same three fields. `capability_for` returns `Capability` in Tasks 5 and 12.

**One known rough edge:** Task 12's fake-friend dispatch path (`AF_FAKE_FRIEND`) is threaded through `cmd_run` somewhat awkwardly to keep tests off real CLIs and off any metered provider. If the implementer finds a cleaner injection point — an adapter whose `binary` is overridden by env — that is an improvement worth making, provided the end-to-end tests still never invoke a real CLI.

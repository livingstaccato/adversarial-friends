# Adversarial Friends Staged Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make interruption, resolution, persistence, and replay deterministic and auditable, then tighten verdict and confinement semantics without combining those policy changes with the reducer migration.

**Architecture:** Phase 1 adds behavioral contracts and isolated repairs at existing boundaries. Phase 2 introduces a pure `ReviewState` reducer and migrates ledger consumers behind replay-equivalence tests. Phase 3 changes review policy and release metadata only after live/replayed state share one implementation.

**Tech Stack:** Python 3.11+ standard library, pytest, mypy strict, Ruff, setuptools/uv, JSONL ledger, POSIX process and filesystem primitives.

**Adversarial review:** Two independent repo-scoped friends reviewed the first complete draft under `spec-vs-reality` and `assumptions` lenses. The adjudication and resulting changes are recorded in `docs/superpowers/reviews/2026-08-29-staged-hardening-plan-review.md`.

---

## File map

- `src/adversarial_friends/merge.py` — retain exact-merge behavior while fixing transitive replay origins; later delegate canonical reconstruction to `ReviewState`.
- `src/adversarial_friends/resolutions.py` — classify evidence paths against recorded repository and artifact context.
- `src/adversarial_friends/commands/resolve.py` — supply stable artifact context and reject unverifiable `fixed` attestations.
- `src/adversarial_friends/commands/runmeta.py` — record the stable artifact path and validate every global invocation limit/model.
- `src/adversarial_friends/http_transport.py` — replace the non-cancellable executor worker with a killable helper process.
- `src/adversarial_friends/ledger.py` — durable append and line-numbered corruption diagnostics.
- `src/adversarial_friends/reviewstate.py` — new pure incremental/replay reducer for ledger-backed state.
- `src/adversarial_friends/commands/resume.py`, `commands/crossexam.py`, `commands/haltstate.py`, `commands/resolve.py`, `commands/run.py`, and `commands/runmeta.py` — consume `ReviewState` instead of reconstructing ledger state independently.
- `src/adversarial_friends/verdicts.py` — content-aware amendment consensus and discard equivalence.
- `src/adversarial_friends/spawn.py`, `rounds.py`, `commands/confinement.py`, and `report.py` — record and render write protection separately from OS confinement.
- `pyproject.toml`, `Makefile`, `AGENTS.md`, and `.github/workflows/ci.yml` — platform and quality-gate accuracy.
- Existing focused test modules plus new `tests/test_reviewstate.py` and `tests/test_reviewstate_properties.py` — regression and replay contracts.

## Phase 1 — correctness contracts and targeted repairs

### Task 1: Preserve transitive origins during ledger reconstruction

**Files:**
- Modify: `src/adversarial_friends/merge.py:121-162`
- Modify: `tests/test_merge.py`
- Modify: `tests/test_verdicts.py`

- [ ] **Step 1: Add the chained-alias regression test**

Add a test using the existing `claim(...)` helper shape in `tests/test_merge.py`:

```python
def test_canonical_reconstruction_preserves_transitive_origins():
    a = claim("c-0001@1", origin=["friend-a"])
    b = claim("c-0002@1", origin=["friend-b"])
    c = claim("c-0003@1", origin=["friend-c"])
    records = [
        a,
        b,
        Alias("c-0001@1", "c-0002@1", 1, "exact", "same"),
        c,
        Alias("c-0003@1", "c-0001@1", 2, "orchestrator", "same defect"),
    ]

    rebuilt = canonical_claims(records)

    assert [(item.id, item.origin) for item in rebuilt] == [
        ("c-0003@1", ["friend-c", "friend-a", "friend-b"])
    ]
```

Add a second assertion proving the transitive contributor is excluded:

```python
def test_reconstructed_transitive_origin_cannot_judge():
    rebuilt = canonical_claims(chained_alias_records())[0]
    roster = ["friend-a", "friend-b", "friend-c", "friend-d"]
    assert verdicts.judges_for(rebuilt, roster) == ["friend-d"]
```

Extend the same fixture with a second duplicate branch, using one `source="exact"` alias and one `source="orchestrator"` alias, and assert both accumulated origin sets reach the final canonical claim. Add a live/replay equivalence test which compares `merge_claims(...)`'s updated canonical claim with `canonical_claims([*claims, *aliases])`.

- [ ] **Step 2: Run the focused tests and verify the failure**

Run:

```bash
uv run pytest tests/test_merge.py tests/test_verdicts.py -q
```

Expected: the chained alias test fails because `friend-b` is absent from the rebuilt origin.

- [ ] **Step 3: Merge the accumulated duplicate origin**

Replace the replay merge expression in `canonical_claims`:

```python
duplicate_origin = origins.get(alias.duplicate)
if duplicate_origin is None or alias.canonical not in origins:
    continue
origins[alias.canonical] = _merge_origin(origins[alias.canonical], duplicate_origin)
```

Keep aliases processed in ledger order and keep the current dangling-alias behavior for Phase 1.

- [ ] **Step 4: Run focused and resume tests**

Run:

```bash
uv run pytest tests/test_merge.py tests/test_verdicts.py tests/test_resume_findings.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the repair**

```bash
git add src/adversarial_friends/merge.py tests/test_merge.py tests/test_verdicts.py
git commit -m "fix: preserve transitive origins across resume"
```

### Task 2: Make resolution evidence independent of invocation cwd

**Files:**
- Modify: `src/adversarial_friends/resolutions.py:98-156`
- Modify: `src/adversarial_friends/commands/resolve.py:86-118`
- Modify: `src/adversarial_friends/commands/runmeta.py:88-132`
- Modify: `tests/test_resolutions.py`
- Modify: `tests/test_run_end_to_end_gate.py`

- [ ] **Step 1: Add cwd-independence and refusal tests**

Add to `tests/test_resolutions.py`:

```python
def test_repo_relative_location_ignores_invocation_cwd(repo, monkeypatch, tmp_path):
    root, sha = repo
    monkeypatch.chdir(tmp_path)

    verified = resolutions.verify_location(
        resolutions.Location("auth.py"),
        root,
        sha,
        artifact_path=None,
    )

    assert verified == resolutions.LOCATION_UNCHANGED


def test_artifact_location_uses_recorded_absolute_path(tmp_path, monkeypatch):
    live = tmp_path / "project" / "spec.md"
    live.parent.mkdir()
    live.write_text("changed\n")
    frozen = tmp_path / "run" / "artifact" / "spec.md"
    frozen.parent.mkdir(parents=True)
    frozen.write_text("original\n")
    monkeypatch.chdir(tmp_path / "run")

    verified = resolutions.verify_location(
        resolutions.Location("spec.md"),
        None,
        None,
        frozen_artifact=frozen,
        artifact_path=live,
    )

    assert verified == resolutions.LOCATION_CHANGED


def test_fixed_with_unverifiable_evidence_is_rejected():
    reason = resolutions.rejection_reason("fixed", resolutions.UNVERIFIABLE)
    assert reason is not None
    assert "accepted-risk" in reason
```

Change the existing test that accepts unverifiable `fixed` evidence to the refusal expectation above. Add an end-to-end resolve test that invokes the command with `cwd=tmp_path` outside the reviewed repository and expects the same verification result as a repository invocation.

- [ ] **Step 2: Run the focused tests and verify failures**

```bash
uv run pytest tests/test_resolutions.py tests/test_run_end_to_end_gate.py -q
```

Expected: relative paths become `unverifiable`, the artifact-path keyword is unknown, and unverifiable `fixed` remains accepted.

- [ ] **Step 3: Record a stable artifact path in run metadata**

Add this helper to `commands/runmeta.py`:

```python
def stable_artifact_path(artifact: Path) -> Path:
    """Absolute invocation path without following the final symlink."""
    return artifact.parent.resolve() / artifact.name
```

Add this field in `_base_meta`:

```python
"artifact_path": str(stable_artifact_path(artifact)),
```

- [ ] **Step 4: Anchor locations inside `verify_location`**

Change its final parameter from `artifact_name` to `artifact_path: Path | None` and resolve the live target with:

```python
named = Path(location.path)
artifact = artifact_path.absolute() if artifact_path is not None else None

if named.is_absolute() and artifact is not None and named == artifact:
    current_path = named
elif artifact is not None and named == Path(artifact.name):
    current_path = artifact
elif repo_root is not None:
    candidate = named if named.is_absolute() else repo_root / named
    resolved_root = repo_root.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return UNVERIFIABLE
    current_path = resolved_candidate
else:
    return UNVERIFIABLE

is_artifact = artifact is not None and current_path.absolute() == artifact
if is_artifact and frozen_artifact is not None:
    if not frozen_artifact.is_file() or not current_path.is_file():
        return UNVERIFIABLE
    before = _slice_lines(
        frozen_artifact.read_text(encoding="utf-8", errors="replace"), location
    )
    after = _slice_lines(
        current_path.read_text(encoding="utf-8", errors="replace"), location
    )
    return LOCATION_CHANGED if before != after else LOCATION_UNCHANGED
```

For repository comparison, calculate the Git path from `current_path` relative to `resolved_root`, never from `Path.cwd()`. Add a macOS-style symlink-root test (`tmp` path versus its resolved target) and an in-repository symlink pointing outside the repository; the former must verify normally and the latter must be `unverifiable`.

- [ ] **Step 5: Supply new and legacy artifact context from `cmd_resolve`**

Use the new field when available. For old runs, fall back only when the old invocation path can be reconstructed without cwd:

```python
artifact_path = Path(meta["artifact_path"]) if meta.get("artifact_path") else None
if artifact_path is None:
    old = Path((meta.get("invocation") or {}).get("artifact") or "")
    if old.is_absolute():
        artifact_path = old
    elif repo_root is not None and old:
        candidate = repo_root / old
        if candidate.is_file():
            artifact_path = candidate

verified = verify_location(
    location,
    repo_root,
    meta.get("snapshot_sha"),
    frozen_artifact=frozen,
    artifact_path=artifact_path,
)
```

Change `rejection_reason`:

```python
if disposition == "fixed" and verified == UNVERIFIABLE:
    return (
        "a fixed resolution must name evidence this run can verify; "
        "use accepted-risk when verification is intentionally unavailable"
    )
```

- [ ] **Step 6: Run focused and gate tests**

```bash
uv run pytest tests/test_resolutions.py tests/test_run_end_to_end_gate.py -q
```

Expected: all selected tests pass, including invocations from outside the repository.

- [ ] **Step 7: Commit the repair**

```bash
git add src/adversarial_friends/resolutions.py src/adversarial_friends/commands/resolve.py src/adversarial_friends/commands/runmeta.py tests/test_resolutions.py tests/test_run_end_to_end_gate.py
git commit -m "fix: anchor resolution evidence to run context"
```

### Task 3: Replace HTTP request threads with a killable helper process

**Files:**
- Modify: `src/adversarial_friends/http_transport.py:25-260`
- Modify: `tests/test_http_transport.py`

- [ ] **Step 1: Add a bounded-cancellation regression test**

Use `ThreadingHTTPServer` for the blocking handler so fixture teardown is not serialized behind the request:

```python
class _BlockingStub(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()

    def do_POST(self):
        type(self).started.set()
        type(self).release.wait(10)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_abort_terminates_the_http_worker(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockingStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    abort = threading.Event()
    timer = threading.Timer(0.1, abort.set)
    timer.start()
    started = time.monotonic()
    try:
        result = http_transport.run_request(
            _adapter(_endpoint(server)),
            _spec(),
            _prompt(tmp_path),
            timeout_s=10,
            abort_event=abort,
        )
    finally:
        _BlockingStub.release.set()
        server.shutdown()
        server.server_close()
        timer.cancel()

    assert result.failure_reason == "aborted"
    assert time.monotonic() - started < 1.0
    assert all(child.name != "afriend-http" for child in multiprocessing.active_children())
```

- [ ] **Step 2: Run the test and verify the executor blocks**

```bash
uv run pytest tests/test_http_transport.py::test_abort_terminates_the_http_worker -q
```

Expected: failure because elapsed time follows the blocking request rather than the cancellation event.

- [ ] **Step 3: Add a top-level serializable worker**

Remove `concurrent.futures` and import `multiprocessing` plus `Connection`:

```python
from multiprocessing.connection import Connection
import multiprocessing
```

Add a top-level worker so the `spawn` start method can import it:

```python
_WireResult = tuple[str, int | None, bytes | str]


def _request_worker(
    endpoint: str,
    payload: bytes,
    timeout_s: int,
    max_response_bytes: int,
    send: Connection,
) -> None:
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            send.send(("ok", response.status, response.read(max_response_bytes + 1)))
    except TimeoutError:
        send.send(("timeout", None, "timeout"))
    except urllib.error.HTTPError as exc:
        body = exc.read(501).decode("utf-8", errors="replace")[:500]
        send.send(("http-error", exc.code, body))
    except (urllib.error.URLError, OSError) as exc:
        send.send(("unreachable", None, str(exc)))
    finally:
        send.close()
```

- [ ] **Step 4: Add the parent lifecycle function**

```python
def _run_worker(
    endpoint: str,
    payload: bytes,
    timeout_s: int,
    max_response_bytes: int,
    abort_event: threading.Event | None,
) -> _WireResult:
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    worker = ctx.Process(
        target=_request_worker,
        args=(endpoint, payload, timeout_s, max_response_bytes, send),
        name="afriend-http",
        daemon=True,
    )
    worker.start()
    send.close()
    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                _stop_worker(worker)
                return ("aborted", None, "aborted")
            if receive.poll(_ABORT_POLL_S):
                try:
                    return receive.recv()
                except EOFError:
                    return ("unreachable", None, "HTTP helper closed without a result")
            if not worker.is_alive():
                return ("unreachable", None, "HTTP helper exited without a result")
    finally:
        receive.close()
        if worker.is_alive():
            _stop_worker(worker)
```

Define `_stop_worker` immediately above `_run_worker`:

```python
def _stop_worker(worker: multiprocessing.Process) -> None:
    worker.terminate()
    worker.join(0.25)
    if worker.is_alive():
        worker.kill()
        worker.join(0.25)
    if worker.is_alive():
        raise RuntimeError("HTTP helper survived terminate and kill")
```

The two bounded joins preserve the one-second cancellation contract while making a surviving helper an explicit failure instead of returning a false `aborted` result.

- [ ] **Step 5: Map worker results back to `SpawnResult`**

Replace the nested `_issue` and executor block in `run_request` with `_run_worker`. Preserve all existing messages:

```python
kind, status, payload_or_reason = _run_worker(
    endpoint, payload, timeout_s, max_response_bytes, abort_event
)
if kind == "aborted":
    return _failure(argv, time.monotonic() - started, "aborted")
if kind == "timeout":
    return _failure(argv, time.monotonic() - started, "timeout")
if kind == "http-error":
    return _failure(
        argv,
        time.monotonic() - started,
        f"http {status}: {str(payload_or_reason).strip()}",
        status=status,
    )
if kind == "unreachable":
    return _failure(
        argv,
        time.monotonic() - started,
        f"endpoint unreachable: {endpoint} ({payload_or_reason})",
    )
raw = cast(bytes, payload_or_reason)
```

Import `cast` from `typing`. Retain the size check, decoding, envelope handling, and normalization below this block.

- [ ] **Step 6: Run all HTTP and signal tests**

```bash
uv run pytest tests/test_http_transport.py tests/test_run_end_to_end_signals.py tests/test_abort_reentry.py tests/test_spawn.py -q
```

Expected: all selected tests pass and the abort test completes in under one second.

- [ ] **Step 7: Commit the repair**

```bash
git add src/adversarial_friends/http_transport.py tests/test_http_transport.py
git commit -m "fix: make HTTP friend cancellation bounded"
```

### Task 4: Validate all global invocation limits and model names

**Files:**
- Modify: `src/adversarial_friends/commands/runmeta.py:183-215`
- Modify: `src/adversarial_friends/commands/friends.py:127-138`
- Modify: `tests/test_run_end_to_end_flags.py`

- [ ] **Step 1: Add invalid-input end-to-end cases**

```python
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "0"),
        ("--max-friends", "0"),
        ("--max-calls", "0"),
        ("--max-wall-clock", "0"),
        ("--max-loop-iterations", "0"),
        ("--require-friends", "0"),
    ],
)
def test_positive_run_limits_are_validated_before_dispatch(tmp_path, flag, value):
    result = run_af(tmp_path, _artifact(tmp_path), "--friend", "fake:good", flag, value)
    assert result.returncode == 2
    assert "positive integer" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_global_model_uses_the_roster_model_allowlist(tmp_path):
    result = run_af(
        tmp_path,
        _artifact(tmp_path),
        "--friend",
        "fake:good",
        "--model=--settings",
    )
    assert result.returncode == 2
    assert "invalid model" in result.stderr
```

- [ ] **Step 2: Run the new tests and verify the empty-roster traceback**

```bash
uv run pytest tests/test_run_end_to_end_flags.py -q
```

Expected: new cases fail; `--max-friends 0` currently exits 1 with `IndexError`.

- [ ] **Step 3: Add one validation helper**

Import `MODEL_RE` into `commands/runmeta.py` and add:

```python
def _validate_positive(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UsageError(f"--{name.replace('_', '-')}={value!r}: expected a positive integer")
```

Call it from `validate_run_args`:

```python
for name in (
    "timeout",
    "max_friends",
    "max_calls",
    "max_wall_clock",
    "max_loop_iterations",
    "require_friends",
):
    value = getattr(args, name, None)
    if value is not None:
        _validate_positive(name, value)

if args.max_rounds < 1:
    raise UsageError("--max-rounds must be at least 1")
if args.model is not None and MODEL_RE.fullmatch(args.model) is None:
    raise UsageError(f"invalid model {args.model!r}: must match {MODEL_RE.pattern!r}")
```

Keep the existing judging-mode requirement of at least two rounds. Remove no final-argv denylist checks.

- [ ] **Step 4: Run focused tests**

```bash
uv run pytest tests/test_run_end_to_end_flags.py tests/test_roster.py tests/test_dispatch_findings.py -q
```

Expected: all selected tests pass and invalid values dispatch nothing.

- [ ] **Step 5: Commit the validation boundary**

```bash
git add src/adversarial_friends/commands/runmeta.py src/adversarial_friends/commands/friends.py tests/test_run_end_to_end_flags.py
git commit -m "fix: validate global run limits before dispatch"
```

### Task 5: Make ledger appends durable and corruption actionable

**Files:**
- Modify: `src/adversarial_friends/ledger.py:1-130`
- Modify: `tests/test_ledger.py`
- Modify: `src/adversarial_friends/assets/references/ledger.md`
- Sync: `plugins/adversarial-friends/skills/adversarial-friends/references/ledger.md`

- [ ] **Step 1: Add synchronization and diagnostic tests**

```python
def test_append_synchronizes_the_record(monkeypatch, tmp_path):
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    Ledger(tmp_path / "claims.jsonl").append(make_claim())
    assert len(synced) >= 1


def test_fsync_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    def fail(_fd):
        raise OSError("disk refused sync")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk refused sync"):
        Ledger(tmp_path / "claims.jsonl").append(make_claim())


def test_corrupt_tail_names_file_and_line(tmp_path):
    path = tmp_path / "claims.jsonl"
    path.write_text(json.dumps(record_to_dict(make_claim())) + "\n{broken\n")
    with pytest.raises(UsageError, match=r"claims\.jsonl:2: malformed JSON"):
        list(Ledger(path).records())


def test_append_retries_a_short_write(monkeypatch, tmp_path):
    real_write = os.write
    calls = []

    def short_write(fd, data):
        calls.append(len(data))
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "write", short_write)
    expected = make_claim()
    ledger = Ledger(tmp_path / "claims.jsonl")
    ledger.append(expected)
    assert len(calls) > 1
    assert list(ledger.records()) == [expected]


def test_corrupt_middle_record_names_its_line(tmp_path):
    path = tmp_path / "claims.jsonl"
    valid = json.dumps(record_to_dict(make_claim()))
    path.write_text(f"{valid}\n{{broken\n{valid}\n")
    with pytest.raises(UsageError, match=r"claims\.jsonl:2: malformed JSON"):
        list(Ledger(path).records())


def test_malformed_record_names_its_line(tmp_path):
    path = tmp_path / "claims.jsonl"
    path.write_text('{"type": "claim", "id": "c-0001@1"}\n')
    with pytest.raises(UsageError, match=r"claims\.jsonl:1: malformed 'claim' record"):
        list(Ledger(path).records())
```

Update the existing malformed-line test to expect `UsageError`, not a raw `JSONDecodeError`.

- [ ] **Step 2: Run the ledger tests and verify failures**

```bash
uv run pytest tests/test_ledger.py -q
```

Expected: synchronization and contextual-diagnostic tests fail.

- [ ] **Step 3: Implement a complete POSIX append**

Import `os` and replace `append`:

```python
def append(self, record: Record) -> None:
    encoded = (json.dumps(record_to_dict(record), sort_keys=True) + "\n").encode("utf-8")
    created = not self.path.exists()
    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("ledger append made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        parent_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
```

The run-directory lock remains the single-writer contract; do not add advisory locking inside `Ledger`.

- [ ] **Step 4: Wrap read errors with ledger location**

```python
def records(self) -> Iterator[Record]:
    if not self.path.exists():
        return
    with self.path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise UsageError(
                    f"{self.path}:{line_no}: malformed JSON: {exc.msg}"
                ) from exc
            try:
                yield record_from_dict(payload)
            except UsageError as exc:
                raise UsageError(f"{self.path}:{line_no}: {exc}") from exc
```

- [ ] **Step 5: Document the exact durability contract and sync the plugin payload**

Add to the canonical ledger reference:

```markdown
## Durability and recovery

The runner has one ledger writer per locked run. `Ledger.append` writes one
complete UTF-8 JSON record, synchronizes the file, and, on first creation,
synchronizes the parent directory before returning. This is a POSIX local-
filesystem guarantee. A malformed record is never skipped automatically;
the error names its file and line so an operator can preserve the run and
repair it explicitly without silently dropping a verdict or resolution.
```

Then run:

```bash
make plugin-sync-copy
```

- [ ] **Step 6: Run ledger, resume, and sync tests**

```bash
uv run pytest tests/test_ledger.py tests/test_resume_findings.py -q
make plugin-sync
```

Expected: all tests and the mirror gate pass.

- [ ] **Step 7: Commit durability work**

```bash
git add src/adversarial_friends/ledger.py tests/test_ledger.py src/adversarial_friends/assets/references/ledger.md plugins/adversarial-friends/skills/adversarial-friends/references/ledger.md
git commit -m "fix: make ledger appends durable and diagnosable"
```

## Phase 2 — one ledger-backed review-state reducer

### Task 6: Introduce `ReviewState` with incremental/replay equivalence

**Files:**
- Create: `src/adversarial_friends/reviewstate.py`
- Create: `tests/test_reviewstate.py`
- Create: `tests/test_reviewstate_properties.py`

- [ ] **Step 1: Write core transition tests**

`tests/test_reviewstate.py` must cover duplicate claim ids, aliases, transitive origins, verdicts, successors, and resolutions:

```python
def test_replay_equals_incremental_apply():
    records = chained_review_records()
    replayed = ReviewState.replay(records)
    incremental = ReviewState()
    for record in records:
        incremental.apply(record)
    assert incremental == replayed


def test_alias_chain_preserves_every_origin():
    state = ReviewState.replay(chained_review_records())
    assert state.claims[0].origin == ["friend-c", "friend-a", "friend-b"]


def test_duplicate_claim_id_with_different_content_is_rejected():
    state = ReviewState()
    state.apply(claim("c-0001@1", text="first"))
    with pytest.raises(UsageError, match="duplicate claim id"):
        state.apply(claim("c-0001@1", text="different"))


def test_dangling_alias_is_recorded_as_a_compatibility_warning():
    state = ReviewState()
    duplicate = claim("c-0002@1")
    state.apply(duplicate)
    alias = Alias("c-0001@1", "c-0002@1", 1, "exact", "same")
    state.apply(alias)
    assert state.aliases == [alias]
    assert state.claims == []
    assert state.transition_warnings == [
        "alias 'c-0002@1' -> 'c-0001@1' has a missing endpoint"
    ]


def test_successor_cycle_is_rejected_even_for_a_preloaded_invalid_graph():
    first = claim("c-0001@1", supersedes="c-0002@1")
    state = ReviewState(claims_by_id={first.id: first})
    with pytest.raises(UsageError, match="successor cycle"):
        state.apply(claim("c-0002@1", supersedes="c-0001@1"))
```

- [ ] **Step 2: Add deterministic generated-sequence coverage**

Use stdlib `random.Random(0)` rather than adding a test dependency:

```python
def test_generated_valid_sequences_replay_every_prefix():
    rng = random.Random(0)
    for _case in range(200):
        records = generated_valid_records(rng)
        incremental = ReviewState()
        prefix = []
        for record in records:
            prefix.append(record)
            incremental.apply(record)
            assert incremental == ReviewState.replay(prefix)
```

Use these concrete helpers so the property test is executable rather than illustrative:

```python
def make_generated_claim(number: int, origin: list[str]) -> Claim:
    return Claim(
        id=f"c-{number:04d}@1",
        supersedes=None,
        origin=origin,
        lens="generated",
        round=1,
        advisory=False,
        severity="medium",
        claim=f"generated claim {number}",
        location=f"src/generated_{number}.py:1",
        evidence=f"generated evidence {number}",
        failure_scenario=f"generated failure {number}",
        suggested_fix=f"generated fix {number}",
    )


def generated_valid_records(rng: random.Random) -> list[Record]:
    count = rng.randint(1, 8)
    claims = [
        make_generated_claim(index + 1, [f"friend-{index + 1}"])
        for index in range(count)
    ]
    records: list[Record] = list(claims)
    active = [claim.id for claim in claims]
    while len(active) > 1 and rng.random() < 0.8:
        duplicate_index = rng.randrange(len(active) - 1)
        duplicate = active[duplicate_index]
        canonical = active[duplicate_index + 1]
        records.append(
            Alias(
                duplicate=duplicate,
                canonical=canonical,
                round=1,
                source="generated",
                rationale="generated alias",
            )
        )
        active.pop(duplicate_index)
    by_id = {claim.id: claim for claim in claims}
    for index, claim_id in enumerate(list(active)):
        if rng.random() < 0.4:
            successor_id = f"{claim_id.rsplit('@', 1)[0]}@2"
            successor = dataclasses.replace(
                by_id[claim_id],
                id=successor_id,
                supersedes=claim_id,
                round=2,
                claim=f"amended {by_id[claim_id].claim}",
            )
            records.append(successor)
            by_id[successor_id] = successor
            active[index] = successor_id
    for claim_id in active:
        if rng.random() < 0.7:
            records.append(
                Verdict(
                    claim_id=claim_id,
                    judge="generated-judge",
                    round=2,
                    verdict="unproven",
                    evidence_assessment="generated assessment",
                    reasoning="generated reasoning",
                    confidence="medium",
                    counter_evidence=None,
                    amended_claim=None,
                )
            )
        if rng.random() < 0.4:
            records.append(
                Resolution(
                    claim_id=claim_id,
                    disposition="accepted-risk",
                    author="generated-operator",
                    evidence="generated acceptance",
                    round=2,
                    verified="not-applicable",
                )
            )
    return records
```

The generator produces 1–8 unique claims, causally ordered aliases and successors, verdicts only for existing claim versions, and resolutions only for existing claims. The fixed seed and bounded sizes make failures reproduce exactly.

- [ ] **Step 3: Run tests and verify the module is absent**

```bash
uv run pytest tests/test_reviewstate.py tests/test_reviewstate_properties.py -q
```

Expected: collection fails because `adversarial_friends.reviewstate` does not exist.

- [ ] **Step 4: Implement the reducer**

Create `reviewstate.py` with this public shape and transition logic:

```python
from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Iterable

from .errors import UsageError
from .ledger import Alias, Claim, Record, Resolution, Verdict


def _union(left: list[str], right: list[str]) -> list[str]:
    out = list(left)
    for value in right:
        if value not in out:
            out.append(value)
    return out


@dataclass
class ReviewState:
    claims_by_id: dict[str, Claim] = field(default_factory=dict)
    origins_by_id: dict[str, list[str]] = field(default_factory=dict)
    aliased_ids: set[str] = field(default_factory=set)
    aliases: list[Alias] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    transition_warnings: list[str] = field(default_factory=list)

    @classmethod
    def replay(cls, records: Iterable[Record]) -> ReviewState:
        state = cls()
        for record in records:
            state.apply(record)
        return state

    def apply(self, record: Record) -> None:
        if isinstance(record, Claim):
            prior = self.claims_by_id.get(record.id)
            if prior is not None and prior != record:
                raise UsageError(f"duplicate claim id {record.id!r} has different content")
            if prior is not None:
                return
            if record.supersedes is not None and record.supersedes not in self.claims_by_id:
                raise UsageError(
                    f"successor {record.id!r} supersedes unknown claim {record.supersedes!r}"
                )
            ancestor = record.supersedes
            while ancestor is not None:
                if ancestor == record.id:
                    raise UsageError(f"successor cycle reaches {record.id!r}")
                predecessor = self.claims_by_id.get(ancestor)
                ancestor = predecessor.supersedes if predecessor is not None else None
            self.claims_by_id[record.id] = record
            self.origins_by_id[record.id] = list(record.origin)
            return
        if isinstance(record, Alias):
            self.aliases.append(record)
            self.aliased_ids.add(record.duplicate)
            if (
                record.canonical not in self.claims_by_id
                or record.duplicate not in self.claims_by_id
            ):
                self.transition_warnings.append(
                    f"alias {record.duplicate!r} -> {record.canonical!r} "
                    "has a missing endpoint"
                )
                return
            if record.canonical == record.duplicate or record.canonical in self.aliased_ids:
                self.transition_warnings.append(
                    f"alias {record.duplicate!r} -> {record.canonical!r} "
                    "is self-referential or non-topological"
                )
                return
            self.origins_by_id[record.canonical] = _union(
                self.origins_by_id[record.canonical],
                self.origins_by_id[record.duplicate],
            )
            return
        claim_id = record.claim_id
        if claim_id not in self.claims_by_id:
            raise UsageError(f"{type(record).__name__.lower()} names unknown claim {claim_id!r}")
        if isinstance(record, Verdict):
            self.verdicts.append(record)
        else:
            self.resolutions.append(record)

    @property
    def claims(self) -> list[Claim]:
        return [
            replace(claim, origin=self.origins_by_id[claim.id])
            for claim in self.claims_by_id.values()
            if claim.id not in self.aliased_ids
        ]

    def verdicts_for(self, claim_id: str) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.claim_id == claim_id]

    def latest_verdicts_for(self, claim_id: str) -> list[Verdict]:
        from .verdicts import latest_per_judge

        return latest_per_judge(self.verdicts_for(claim_id))

    def claim_state(
        self,
        claim: Claim,
        roster: list[str],
        round_no: int,
        max_rounds: int,
        *,
        required_missing: bool = False,
    ) -> str:
        from .verdicts import state_for

        return state_for(
            claim,
            self.verdicts_for(claim.id),
            roster,
            round_no,
            max_rounds,
            required_missing=required_missing,
        )

    def blocking(self, states: dict[str, str]) -> list[Claim]:
        from .resolutions import blocking_claims

        return blocking_claims(self.claims, states, self.resolutions)
```

Keep insertion order from `claims_by_id`; Python 3.11 guarantees dict insertion order.
Alias warnings deliberately preserve the existing tolerant replay contract; live merge/orchestrator validation still prevents new dangling, duplicate, chained, or self aliases from being written.

- [ ] **Step 5: Run reducer tests and strict typing**

```bash
uv run pytest tests/test_reviewstate.py tests/test_reviewstate_properties.py -q
uv run mypy src/adversarial_friends/reviewstate.py
```

Expected: tests and strict typing pass.

- [ ] **Step 6: Commit the reducer core**

```bash
git add src/adversarial_friends/reviewstate.py tests/test_reviewstate.py tests/test_reviewstate_properties.py
git commit -m "feat: add deterministic ledger review-state reducer"
```

### Task 7: Migrate live and resumed consumers to `ReviewState`

**Files:**
- Modify: `src/adversarial_friends/merge.py`
- Modify: `src/adversarial_friends/commands/resume.py`
- Modify: `src/adversarial_friends/commands/haltstate.py`
- Modify: `src/adversarial_friends/commands/resolve.py`
- Modify: `src/adversarial_friends/commands/run.py`
- Modify: `src/adversarial_friends/commands/runmeta.py`
- Modify: `src/adversarial_friends/commands/critique.py`
- Modify: `src/adversarial_friends/commands/crossexam.py`
- Modify: `src/adversarial_friends/report.py`
- Modify: `tests/test_resume_findings.py`
- Modify: `tests/test_resume_crash_safety.py`
- Modify: `tests/test_run_end_to_end_gate.py`
- Modify: `tests/test_report.py`

- [ ] **Step 1: Add fixture-based live/replay equivalence assertions**

For every existing resume fixture, load all records and assert the reducer produces the same observable state used by the report:

```python
def assert_reducer_matches_existing_reconstruction(run_dir):
    records = list(Ledger(run_dir / "claims.jsonl").records())
    state = ReviewState.replay(records)
    assert state.claims == canonical_claims(records)
    assert state.verdicts == [record for record in records if isinstance(record, Verdict)]
    assert state.aliases == [record for record in records if isinstance(record, Alias)]
    assert state.resolutions == [
        record for record in records if isinstance(record, Resolution)
    ]
```

Add an end-to-end halt/resume test with the chained aliases from Task 1 and assert the post-resume judge slice excludes every transitive origin.
Run `assert_reducer_matches_existing_reconstruction` over the partially applied merge fixture in `tests/test_resume_crash_safety.py`; assert replay completes, retains the already-applied alias, and the retry applies only the remaining merge.

- [ ] **Step 2: Run resume/gate tests before migration**

```bash
uv run pytest tests/test_resume_findings.py tests/test_resume_crash_safety.py tests/test_run_end_to_end_gate.py -q
```

Expected: fixture assertions pass against valid ledgers; the chained-alias test is protected by Task 1.

- [ ] **Step 3: Delegate canonical reconstruction**

Keep `canonical_claims` as a compatibility wrapper while removing its duplicate algorithm:

```python
def canonical_claims(records: Sequence[object]) -> list[Claim]:
    typed = [record for record in records if isinstance(record, (Claim, Verdict, Alias, Resolution))]
    return ReviewState.replay(typed).claims
```

Import all `Record` variants and `ReviewState`. Existing callers remain green while migration proceeds.

- [ ] **Step 4: Replace direct ledger filtering in commands**

At each ledger read boundary use exactly one replay:

```python
records = list(store.ledger.records())
review = ReviewState.replay(records)
claims = review.claims
```

Use `review.verdicts`, `review.aliases`, and `review.resolutions` in place of repeated list comprehensions in `resume.py`, `haltstate.py`, `resolve.py`, `run.py`, and `runmeta.py`. Do not reread the ledger again within the same command operation.

Replace calls to the free `state_for` and `blocking_claims` reconstruction paths with `review.claim_state(...)` and `review.blocking(...)`. Create the `ReviewState` once at each command boundary and pass that object into helper functions which need derived state; do not construct a second reducer inside those helpers.

Copy each `review.transition_warnings` entry into the run downgrade list using the prefix `ledger compatibility warning:`. Deduplicate the rendered strings so repeated command reads do not multiply the warning. This makes tolerated historical aliases visible without turning replay into an unrecoverable failure.

Change the internal report API so it consumes the reducer directly:

```python
def render(
    review: ReviewState,
    run_meta: dict[str, Any],
    states: dict[str, str] | None = None,
) -> str:
    claims = review.claims
    aliases = review.aliases
    verdicts = review.verdicts
```

Update `commands/runmeta.py`, `commands/haltstate.py`, and `tests/test_report.py` to pass one replayed/live `ReviewState`; remove the separate `claims`, `aliases`, and `verdicts` parameters after all call sites are migrated. In finalization, replay once from the ledger and use that same `review` for gate blocking and report rendering, making the durable ledger—not the process-local accumulator—the final observable state.

- [ ] **Step 5: Apply newly appended records to live state**

Where a command already holds `ReviewState`, make append and apply adjacent:

```python
store.ledger.append(record)
review.apply(record)
```

Do not update in-memory state before the durable append succeeds. A synchronization failure must leave memory no further ahead than the ledger.

- [ ] **Step 6: Run all state-machine and end-to-end tests**

```bash
uv run pytest tests/test_merge.py tests/test_reviewstate.py tests/test_reviewstate_properties.py tests/test_resume_findings.py tests/test_resume_crash_safety.py tests/test_run_end_to_end_crossexam.py tests/test_run_end_to_end_gate.py tests/test_report.py -q
```

Expected: all selected tests pass with one reconstruction implementation.

- [ ] **Step 7: Run strict typing and the line-count gate**

```bash
uv run mypy src
python3 scripts/check_max_loc.py
```

Expected: both gates pass; split a command helper only if the 777-line limit requires it.

- [ ] **Step 8: Commit consumer migration**

```bash
git add src/adversarial_friends/merge.py src/adversarial_friends/report.py src/adversarial_friends/commands/resume.py src/adversarial_friends/commands/haltstate.py src/adversarial_friends/commands/resolve.py src/adversarial_friends/commands/run.py src/adversarial_friends/commands/runmeta.py src/adversarial_friends/commands/critique.py src/adversarial_friends/commands/crossexam.py tests/test_resume_findings.py tests/test_resume_crash_safety.py tests/test_run_end_to_end_gate.py tests/test_report.py
git commit -m "refactor: derive run state through ledger reducer"
```

## Phase 3 — policy and release hardening

### Task 8: Require content consensus for amendments and meaningful discard equality

**Files:**
- Modify: `src/adversarial_friends/verdicts.py:27-35,130-217,287-352`
- Modify: `src/adversarial_friends/report.py:224-250`
- Modify: `tests/test_verdicts.py`
- Modify: `tests/test_verdicts_lifecycle.py`
- Modify: `tests/test_report.py`
- Modify: `src/adversarial_friends/assets/references/ledger.md`
- Sync: `plugins/adversarial-friends/skills/adversarial-friends/references/ledger.md`

- [ ] **Step 1: Add conflicting-amendment state tests**

```python
def test_conflicting_amendment_texts_are_contested():
    cast = [
        verdict("claude-security", "amended", amended="first wording"),
        verdict("agy-assumptions", "amended", amended="second wording"),
    ]
    assert verdicts.state_for(claim(), cast, ROSTER, 2, 3) == verdicts.CONTESTED
    assert verdicts.state_for(claim(), cast, ROSTER, 3, 3) == verdicts.DEADLOCKED


def test_amendment_consensus_normalizes_only_edges_and_newlines():
    cast = [
        verdict("claude-security", "amended", amended="  same\r\nwording  "),
        verdict("agy-assumptions", "amended", amended="same\nwording"),
    ]
    assert verdicts.state_for(claim(), cast, ROSTER, 2, 3) == verdicts.SUPERSEDED


def test_report_shows_every_conflicting_amendment():
    cast = [
        verdict("claude-security", "amended", amended="first wording"),
        verdict("agy-assumptions", "amended", amended="second wording"),
    ]
    item = claim()
    review = ReviewState.replay([item, *cast])
    out = render(
        review,
        meta(claim_states={item.id: "contested"}),
        {item.id: "contested"},
    )
    assert "proposed amendment: first wording" in out
    assert "proposed amendment: second wording" in out
```

- [ ] **Step 2: Add evidence-sensitive discard tests**

```python
def test_new_counter_evidence_prevents_discard():
    first = verdict("claude-security", "unproven")
    second = dataclasses.replace(first, round=3, counter_evidence="src/auth.py:38")
    assert verdicts.verdict_set_signature([first], first.claim_id) != (
        verdicts.verdict_set_signature([first, second], first.claim_id)
    )


def test_reasoning_and_confidence_alone_do_not_prevent_discard():
    first = verdict("claude-security", "unproven", reasoning="could not find it")
    second = dataclasses.replace(
        first,
        round=3,
        reasoning="looked twice and still could not find it",
        confidence="low",
    )
    assert verdicts.verdict_set_signature([first], first.claim_id) == (
        verdicts.verdict_set_signature([first, second], first.claim_id)
    )
```

- [ ] **Step 3: Run verdict tests and verify current semantics fail**

```bash
uv run pytest tests/test_verdicts.py tests/test_verdicts_lifecycle.py -q
```

Expected: conflicting amendments currently supersede, and counter-evidence currently does not change the discard signature.

- [ ] **Step 4: Make amendment unanimity content-aware**

Add:

```python
def _normalized_amendment(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _amendments_agree(verdicts: Iterable[Verdict]) -> bool:
    amendments = {
        _normalized_amendment(verdict.amended_claim)
        for verdict in verdicts
        if verdict.verdict == "amended"
    }
    return len(amendments) == 1 and "" not in amendments
```

In both the one-judge and general unanimous `amended` branches, return `SUPERSEDED` only when `_amendments_agree(dispositive)`; otherwise return `CONTESTED` or `DEADLOCKED` according to `round_no`.

Change `build_successor` to reject conflicting direct calls:

```python
if not _amendments_agree(ordered):
    raise ValueError(f"amenders supplied conflicting wording for {claim.id}")
```

Remove first-judge selection and alternate-wording notes. Keep the return type for compatibility, returning `(successor, None)`.
Construct the successor from `_normalized_amendment(ordered[0].amended_claim)` so newline- and edge-whitespace-equivalent inputs produce a deterministic stored claim.

In `_render_verdict_sections`, render `verdict.amended_claim` immediately below each amended verdict:

```python
if verdict.amended_claim:
    lines.append(f"  - proposed amendment: {_escape_block(verdict.amended_claim)}")
```

This is required even when amendments conflict: the state remains contested, and the report must preserve every alternative instead of relying on the removed single-winner amendment note.

- [ ] **Step 5: Expand the verdict signature**

Change `_Signature` and its builder:

```python
_Signature = tuple[tuple[str, str, str, str, str], ...]


def _normalized_optional(value: str | None) -> str:
    return " ".join((value or "").split())


def verdict_set_signature(verdicts: Iterable[Verdict], claim_id: str) -> _Signature:
    relevant = latest_per_judge(verdict for verdict in verdicts if verdict.claim_id == claim_id)
    return tuple(
        sorted(
            (
                verdict.judge,
                verdict.verdict,
                verdict.evidence_assessment,
                _normalized_optional(verdict.counter_evidence),
                _normalized_amendment(verdict.amended_claim),
            )
            for verdict in relevant
        )
    )
```

- [ ] **Step 6: Update docs, sync assets, and run tests**

Document that conflicting rewrites remain contested and define the discard signature fields. Then:

```bash
make plugin-sync-copy
uv run pytest tests/test_verdicts.py tests/test_verdicts_lifecycle.py tests/test_run_end_to_end_crossexam.py tests/test_report.py -q
make plugin-sync
```

Expected: all selected tests and sync gate pass.

- [ ] **Step 7: Commit policy semantics**

```bash
git add src/adversarial_friends/verdicts.py src/adversarial_friends/report.py tests/test_verdicts.py tests/test_verdicts_lifecycle.py tests/test_report.py src/adversarial_friends/assets/references/ledger.md plugins/adversarial-friends/skills/adversarial-friends/references/ledger.md
git commit -m "fix: require substantive verdict-set consensus"
```

### Task 9: Report write protection and OS confinement separately

**Files:**
- Modify: `src/adversarial_friends/spawn.py:92-115`
- Modify: `src/adversarial_friends/http_transport.py:42-65`
- Modify: `src/adversarial_friends/dispatch.py`
- Modify: `src/adversarial_friends/rounds.py:311-364`
- Modify: `src/adversarial_friends/report.py:260-280`
- Modify: `src/adversarial_friends/commands/confinement.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_sandbox.py`

- [ ] **Step 1: Add report-contract tests**

Update report metadata fixtures and assert the three guarantees independently:

```python
def test_friend_table_separates_write_protection_from_os_confinement():
    run = meta(
        friends=[
            {
                "name": "claude-security-0",
                "model": None,
                "effort": None,
                "write_protected": True,
                "declared_scope": "repo",
                "os_confined": False,
                "status": "ok",
            }
        ]
    )
    out = render([claim("c-0001@1")], [], run)
    assert "| write-protected | declared scope | OS-confined |" in out
    assert "| True | repo | False |" in out
    assert "same-user filesystem read access" in out
```

Add sandbox-path tests that expect `os_confined=True` only when `sandbox.wrap(...)` actually produced a confinement command.

- [ ] **Step 2: Run report and sandbox tests and verify failure**

```bash
uv run pytest tests/test_report.py tests/test_sandbox.py tests/test_sandbox_findings.py -q
```

Expected: metadata has only `readonly`/`scope`, and the new columns are absent.

- [ ] **Step 3: Carry confinement outcome on `SpawnResult`**

Add this field immediately after `output_truncated` so existing constructors remain valid:

```python
os_confined: bool = False
```

Initialize `os_confined = False` beside `child_env`. In the existing `mechanism is not None` branch, set it only after `sandbox.wrap(...)` returns; after `run_process`, copy it to the result:

```python
os_confined = False

argv = sandbox.wrap(
    argv,
    mechanism,
    policy,
    prompt_file.with_suffix(".sandbox"),
)
os_confined = True

outcome.os_confined = os_confined
```

The first line belongs beside the existing `child_env` initialization, the wrapped assignment replaces the current one in the `mechanism is not None` branch, and the final assignment goes immediately after the existing `run_process(...)` call.

HTTP results remain `False`; they receive only prompt text and never execute with local filesystem access. Preserve `transport="http"` in friend metadata so the report can distinguish not-applicable confinement from an unconfined executable.

- [ ] **Step 4: Emit explicit metadata in `persist_result`**

Add required `transport: str` to `persist_result`. Pass `registry[spec.cli].transport` from real adapter call sites and `"fake"` from the fake/test dispatcher. Return:

```python
"transport": transport,
"write_protected": capability.readonly,
"declared_scope": spec.scope,
"os_confined": outcome.os_confined,
```

For one compatibility release also retain:

```python
"readonly": capability.readonly,
"scope": spec.scope,
```

- [ ] **Step 5: Render the new friend table and residual warning**

Change the header to:

```python
lines.append(
    "| friend | model | effort | transport | write-protected | "
    "declared scope | OS-confined | status |"
)
```

After the table, report executable friends with `write_protected=True` and `os_confined=False`:

```python
read_exposed = [
    friend["name"]
    for friend in run_meta["friends"]
    if friend.get("transport") != "http"
    and friend.get("write_protected")
    and not friend.get("os_confined")
]
if read_exposed:
    lines.extend(
        [
            "",
            "**Filesystem read scope:** "
            + ", ".join(read_exposed)
            + " were write-protected but not OS-confined; each retained "
            "same-user filesystem read access outside the declared prompt scope.",
        ]
    )
```

- [ ] **Step 6: Run report, sandbox, run-metadata, and doctor tests**

```bash
uv run pytest tests/test_report.py tests/test_sandbox.py tests/test_sandbox_findings.py tests/test_run_end_to_end_basics.py tests/test_cli_entry.py -q
```

Expected: all selected tests pass and existing metadata consumers retain their compatibility keys.

- [ ] **Step 7: Commit confinement reporting**

```bash
git add src/adversarial_friends/spawn.py src/adversarial_friends/http_transport.py src/adversarial_friends/dispatch.py src/adversarial_friends/rounds.py src/adversarial_friends/report.py src/adversarial_friends/commands/confinement.py tests/test_report.py tests/test_sandbox.py
git commit -m "feat: distinguish write protection from OS confinement"
```

### Task 10: Correct platform metadata and local quality-gate claims

**Files:**
- Modify: `pyproject.toml:19-30`
- Modify: `Makefile:1-46`
- Modify: `AGENTS.md`
- Modify: `README.md`

- [ ] **Step 1: Add portable wheel gates to `make quality`**

Add explicit targets:

```make
.PHONY: wheel-assets wheel-install

wheel-assets: ## Build the wheel and verify bundled assets
\tci/verify_wheel_assets.sh

wheel-install: ## Install the wheel outside the checkout and smoke-test afriend
\tci/verify_wheel_install.sh
```

Update:

```make
quality: lint type-check max-loc plugin-sync version-sync wheel-assets wheel-install test
```

- [ ] **Step 2: Narrow operating-system classifiers**

Replace the OS-independent classifier with:

```toml
"Operating System :: MacOS",
"Operating System :: POSIX :: Linux",
```

Do not add Windows compatibility shims in this hardening scope.

- [ ] **Step 3: Make local/CI wording exact**

Update `AGENTS.md` and the relevant README development section to state:

```markdown
`make quality` runs every portable CI gate, including wheel construction and
isolated installation. Linux CI additionally installs bubblewrap and requires
the real OS-confinement tests to execute; macOS cannot reproduce that Linux-
specific assertion locally. Use `make act-ci` for the closest local Linux run.
```

- [ ] **Step 4: Exercise the updated Makefile targets**

```bash
make wheel-assets
make wheel-install
make quality
```

Expected: wheel asset verification, isolated entry-point installation, and all existing quality gates pass.

- [ ] **Step 5: Commit release-engineering corrections**

```bash
git add pyproject.toml Makefile AGENTS.md README.md
git commit -m "build: align platform and local quality guarantees"
```

## Final verification and release preparation

### Task 11: Verify all phases together and prepare release notes

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `VERSION`
- Modify: `plugins/adversarial-friends/.codex-plugin/plugin.json`
- Modify: `plugins/adversarial-friends/.claude-plugin/plugin.json`

- [ ] **Step 1: Run focused regression groups**

```bash
uv run pytest tests/test_merge.py tests/test_resolutions.py tests/test_run_end_to_end_gate.py tests/test_http_transport.py tests/test_ledger.py -q
uv run pytest tests/test_reviewstate.py tests/test_reviewstate_properties.py tests/test_resume_findings.py tests/test_resume_crash_safety.py -q
uv run pytest tests/test_verdicts.py tests/test_verdicts_lifecycle.py tests/test_report.py tests/test_sandbox.py -q
```

Expected: every group passes.

- [ ] **Step 2: Run complete repository verification**

```bash
make quality
bash ci/verify_wheel_assets.sh
bash ci/verify_wheel_install.sh
```

Expected: formatting, lint, mypy strict, max-LOC, plugin sync, version sync, all tests, wheel assets, and isolated wheel installation pass.

- [ ] **Step 3: Run the Linux confinement gate in CI or `act`**

```bash
make act-ci
```

Expected: the Python 3.13 Linux job passes and `ci/assert_sandbox_tested.sh` confirms real bubblewrap tests executed. If Docker/act is unavailable, push a branch and require the GitHub Actions matrix before release.

- [ ] **Step 4: Document behavior changes**

Add changelog entries that explicitly name:

```markdown
- Fixed transitive origin loss after resumed alias chains.
- Made resolution verification independent of invocation directory and
  stopped unverifiable evidence from supporting `fixed`.
- Made HTTP cancellation bounded by terminating a helper process.
- Made ledger appends POSIX-durable and corruption diagnostics actionable.
- Unified live and resumed ledger state through `ReviewState`.
- Kept conflicting amendments contested instead of choosing one rewrite.
- Included evidence changes in consecutive-round discard equivalence.
- Separated write protection, declared scope, and OS confinement in reports.
```

- [ ] **Step 5: Set the hardening release version**

Set `VERSION` and both plugin manifests to `0.2.0`. The reducer boundary and report metadata are intentional semantic changes, so this plan treats the completed three-phase set as a minor release. Then run:

```bash
make version-sync
make plugin-sync
```

Expected: both sync gates pass with all three version declarations equal to `0.2.0`.

- [ ] **Step 6: Commit release documentation**

```bash
git add CHANGELOG.md VERSION plugins/adversarial-friends/.codex-plugin/plugin.json plugins/adversarial-friends/.claude-plugin/plugin.json
git commit -m "release: prepare adversarial friends 0.2.0"
```

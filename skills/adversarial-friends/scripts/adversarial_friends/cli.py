"""Command line entry point.

Wires the eleven modules under this package into two working subcommands:
`af run --mode report` and `af doctor`. See the module-level notes below each
function for the integration decisions this file makes on top of what those
modules already guarantee individually.
"""
import argparse
import concurrent.futures
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import __version__, isolation
from .adapters import Adapter, Capability, FriendSpec, build_argv, load_adapters
from .claimschema import schema_path
from .errors import AfError, NoFriendsError, UsageError
from .ids import format_claim_id, validate_friend_name
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

# A synthetic capability for the test-only "fake" cli (see _dispatch): it
# never touches adapters.py/build_argv at all, so there is no real
# Capability to surface. Always doc-scope, no schema enforcement, no
# verifiable effort -- reported honestly rather than guessed.
_FAKE_CAPABILITY = Capability(schema=False, readonly=False, effort="none")


def available_lenses() -> list[str]:
    names = sorted(p.stem for p in LENS_DIR.glob("*.md"))
    return names or ["assumptions"]


def _load_lens(lens_name: str) -> tuple[dict[str, str], str] | None:
    """Return (frontmatter, body) for lenses/<lens_name>.md, or None if no
    such file exists.

    `body` has the YAML-ish frontmatter block stripped -- a friend needs the
    lens's prose, not its `applies_to:`/`default_scope:` metadata. A
    friend's lens is free text (from --friend cli:lens, or a round-robin
    assignment over available_lenses()); neither path validates it against
    the filesystem at spec-resolution time, so "no such file" is an
    expected, handled case here, not a bug -- see _build_friend_prompt.
    """
    path = LENS_DIR / f"{lens_name}.md"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    meta: dict[str, str] = {}
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                body = "\n".join(lines[i + 1:]).strip("\n")
                return meta, body
            key, sep, value = lines[i].partition(":")
            if sep:
                meta[key.strip()] = value.strip()
        # Opened with "---" but never closed: fall back to treating the
        # whole file as prose rather than silently losing it.
        return {}, "\n".join(lines).strip("\n")
    return {}, "\n".join(lines).strip("\n")


def _build_friend_prompt(spec: FriendSpec, artifact_text: str) -> tuple[str, bool, str | None]:
    """Return (prompt_text, advisory, downgrade_note).

    Each friend's prompt is built individually and carries its own lens's
    prose -- this is the whole point of assigning a lens (see
    SKILL.md's "Choosing lenses" and lenses/*.md): it should shape what the
    friend looks for, not merely label its output after the fact. Before
    this function existed, cmd_run built exactly one prompt.txt shared
    byte-for-byte by every friend regardless of --friend cli:lens, which
    made the lens name pure bookkeeping.

    `advisory` comes from that same lens file's `requires_failure_scenario`
    field: an explicit `false` means claims from this friend should be
    treated as advisory (currently only lenses/scope.md sets this); a
    missing field, any other value, or a missing lens file all default to
    non-advisory.

    A missing lens file is handled, not fatal: fall back to the generic
    contract header alone, report non-advisory, and hand back a downgrade
    note for the caller to record in run.json rather than silently
    pretending the friend had lens guidance.
    """
    loaded = _load_lens(spec.lens)
    if loaded is None:
        prompt = PROMPT_HEADER + "\n--- ARTIFACT ---\n" + artifact_text
        note = (f"{spec.name}: no lens file found for lens {spec.lens!r}; ran "
                "with the generic prompt only, with no lens-specific guidance.")
        return prompt, False, note
    meta, body = loaded
    advisory = meta.get("requires_failure_scenario", "true").strip().lower() == "false"
    prompt = (PROMPT_HEADER + "\n--- LENS ---\n" + body +
              "\n\n--- ARTIFACT ---\n" + artifact_text)
    return prompt, advisory, None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="af")
    parser.add_argument("--version", action="version", version=f"af {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run")
    run_p.add_argument("artifact")
    run_p.add_argument("--mode", default="report",
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


def _specs_from_flags(values: list[str], timeout: int, registry: dict[str, Adapter],
                      fake_enabled: bool) -> list[FriendSpec]:
    """Build FriendSpecs directly from repeated --friend cli:lens flags.

    Deliberately bypasses roster.resolve(overrides=...) entirely, rather
    than converting each flag into an override dict and routing it through
    resolve(). Two reasons:

    1. roster.resolve(..., overrides=[]) treats an *explicit* empty list the
       same as "no overrides given" and falls through to full
       auto-discovery (a landmine inherited from Task 10, documented in
       test_roster.py's neighbors). --friend's own default is `[]`
       (argparse action="append", default=[]) precisely when the caller
       requests auto-discovery, so the two meanings of "empty" would
       collide if this function routed through resolve() at all: an
       explicit but empty override intent would be indistinguishable from
       "no --friend flags given". Building specs directly here means empty
       is only ever the "not given" case (handled by cmd_run choosing the
       discovery branch instead of calling this function), never something
       that can silently expand into every discovered friend.
    2. It is the cleanest available seam for the "fake" test-only cli (see
       the module docstring on tests/test_run_end_to_end.py): "fake" has no
       adapter in the registry at all, so routing it through
       roster.resolve's overrides validation (which requires
       registry[entry["cli"]] to exist) would need either a fabricated
       adapter or a special case inside roster.py -- a Task 10 file this
       task does not own.

    An unknown `cli` therefore raises UsageError (exit 2) here directly,
    the same fix Task 10 needed for roster.resolve's own overrides path
    (a config typo is a usage error, not "no friends available" -- see
    errors.NoFriendsError's exit code 3 vs UsageError's exit 2).
    """
    specs = []
    for index, value in enumerate(values):
        cli, sep, lens = value.partition(":")
        if not sep or not cli or not lens:
            raise UsageError(f"--friend must be formatted as cli:lens, got {value!r}")
        if cli == "fake":
            if not fake_enabled:
                raise UsageError(
                    "cli 'fake' is only available when AF_FAKE_FRIEND is set "
                    "(it exists for tests, not real runs)"
                )
            # fake:<mode> defaults to doc scope, same as always. A test that
            # specifically needs to exercise the repo-scope worktree path
            # (which no real adapter can reach in a test environment with
            # no agent CLI on PATH -- the whole point of that PATH
            # restriction) may instead write fake:<mode>:repo to request
            # it explicitly. This suffix is only recognized for the
            # test-only "fake" cli; it has no effect on any real adapter.
            lens, _, scope_suffix = lens.partition(":")
            if scope_suffix and scope_suffix not in ("repo", "doc"):
                raise UsageError(
                    f"fake friend scope suffix must be 'repo' or 'doc', got {scope_suffix!r}"
                )
            scope = scope_suffix or "doc"
        else:
            adapter = registry.get(cli)
            if adapter is None:
                raise UsageError(
                    f"unknown cli: {cli!r} (known: {sorted(registry) or 'none'})"
                )
            scope = "repo" if adapter.readonly_argv else "doc"
        name = f"{cli}-{lens}-{index}"
        validate_friend_name(name)
        specs.append(FriendSpec(name=name, cli=cli, lens=lens, model=None,
                                effort=None, scope=scope, timeout=timeout))
    return specs


def _resolve_repo_root(artifact: Path) -> Path | None:
    """Return the git repository root enclosing `artifact`, or None if it
    is not inside a git repository at all.

    isolation.snapshot_commit requires a repository ROOT and raises AfError
    for a nested subdirectory (naming the real root). Resolving the root
    here -- via the artifact's own enclosing directory, not Path.cwd() --
    means snapshot_commit is only ever called with a value it will accept,
    regardless of how deeply nested the artifact is inside the repo, and
    regardless of what directory `af` itself happens to be invoked from.
    """
    result = subprocess.run(
        ["git", "-C", str(artifact.resolve().parent), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _dispatch(spec: FriendSpec, cwd: Path, registry: dict[str, Adapter],
              fake_cmd: list[str] | None, prompt_file: Path, schema_file: Path,
              abort_event: threading.Event | None = None):
    """Build argv for one friend and run it. Returns (spec, capability, outcome).

    Capability is always the value build_argv computed and returned for
    THIS call -- never re-derived from the finished argv or from
    spec.scope. Re-deriving it (e.g. `readonly = spec.scope == "repo"`)
    would silently drift from reality for an adapter like opencode, whose
    readonly_argv is empty: even with scope="repo" requested, build_argv
    never emits a readonly flag for it, so its real capability.readonly is
    False. See adapters.build_argv's docstring for why the prompt itself
    (untrusted document text) must never be allowed to influence this
    value either -- another reason to trust only what build_argv reports.

    `abort_event`, if given, is passed straight through to
    spawn.run_process so a signal handler in cmd_run can stop this friend
    (and reap its whole process group) without waiting out its full
    --timeout -- see cmd_run's signal handling for why this matters: a
    cancelled run must not leave a metered agent CLI process running
    unbounded.
    """
    if spec.cli == "fake":
        argv = [*fake_cmd, spec.lens]
        stdin_text = None
        capability = _FAKE_CAPABILITY
    else:
        adapter = registry[spec.cli]
        argv, stdin_text, capability = build_argv(adapter, spec, prompt_file, schema_file)
        check_denied_values(argv)
    outcome = run_process(argv, stdin_text, spec.timeout, cwd, abort_event=abort_event)
    return spec, capability, outcome


def cmd_run(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact)
    if not artifact.is_file():
        raise UsageError(f"artifact not found: {artifact}")
    if args.mode != "report":
        raise UsageError(
            f"mode {args.mode!r} is not implemented yet; only 'report' is available"
        )
    # Deliberately NOT resolved here: resolving would follow a symlinked
    # artifact to its target's own name, so a review of `link_spec.md ->
    # real_spec.md` would report and store the artifact as "real_spec.md"
    # -- surprising given the user passed the link's name. `artifact` is
    # used as-is everywhere below (shutil.copy2 and doc_scope_dir both
    # follow symlinks transparently when reading its content);
    # _resolve_repo_root resolves its own local copy internally, so
    # nothing here needs an absolute/resolved path to work correctly.

    registry = load_adapters(ADAPTER_DIR)
    # AF_FAKE_FRIEND keeps the end-to-end tests off real CLIs and, critically,
    # off any metered provider. `--friend fake:<mode>` runs
    # `$AF_FAKE_FRIEND <mode>` directly (see _dispatch); the mode travels in
    # the lens slot of the cli:lens flag syntax.
    fake_env = os.environ.get("AF_FAKE_FRIEND")
    fake_cmd = fake_env.split() if fake_env else None

    specs = (_specs_from_flags(args.friend, args.timeout, registry, bool(fake_cmd))
             if args.friend else
             resolve(registry, available_lenses(), os.environ, shutil.which,
                     include_self=args.include_self, timeout=args.timeout))
    if not specs:
        raise NoFriendsError("no usable friends for mode 'report'")
    for spec in specs:
        validate_friend_name(spec.name)

    # Signal handling: a cancelled or CI-killed run must not leave a
    # metered agent CLI process running unbounded, nor a stale
    # `git worktree` registration behind in the repo under review. Neither
    # SIGTERM nor (reliably, once the main thread is blocked deep inside a
    # C-level wait) SIGINT would otherwise give this function's own
    # `finally` blocks a chance to run at all: SIGTERM's default
    # disposition kills the process immediately, with no Python-level
    # unwinding whatsoever; SIGINT's default handler does raise
    # KeyboardInterrupt, but that exception, once it propagates out of the
    # blocked `pool.map()` call below, immediately re-blocks inside
    # `ThreadPoolExecutor.__exit__`'s own `shutdown(wait=True)`, which
    # waits for the same still-hung worker -- so cleanup never actually
    # runs within any reasonable time either way. Installing explicit
    # handlers for both signals, which only ever set `abort_event` and
    # shut the active pool down without waiting, is what makes the
    # `finally` blocks below reachable promptly. Handlers are restored
    # unconditionally in the outer `finally` -- a library-ish function
    # should not permanently hijack process-wide signal disposition.
    downgrades: list[str] = []
    if len(specs) == 1:
        # --friend REPLACES the roster rather than augmenting discovery (see
        # _specs_from_flags above), so a single --friend flag -- or, per
        # design doc §8.3, discovery itself resolving to just one friend --
        # produces a run that cannot cross-examine anything: it is one
        # reviewer's opinion, not disagreement between several. That
        # reduced guarantee must be visible in run.json/report.md rather
        # than a single-reviewer report quietly looking like the real
        # thing -- the same rule already applied to every other downgrade
        # this function records.
        downgrades.append(
            f"only one friend ({specs[0].name}) resolved for this run; "
            "cross-examination needs at least two independent friends, so "
            "this report reflects a single reviewer's opinion, not "
            "disagreement between several."
        )
    abort_event = threading.Event()
    abort_signum: dict[str, int | None] = {"value": None}
    active_pool: list[concurrent.futures.ThreadPoolExecutor | None] = [None]

    def _handle_abort(signum: int, frame) -> None:
        abort_signum["value"] = signum
        abort_event.set()
        # spawn.run_process (via _dispatch's abort_event) already notices
        # this on its own next poll and terminates its process group
        # promptly -- but the main thread here may be blocked inside
        # pool.map()'s wait for that same worker future. Shutting the pool
        # down without waiting means this handler itself never blocks, and
        # the main thread's wait resolves as soon as the worker's own
        # abort-triggered return lands, not whenever `with pool:`'s
        # implicit wait=True would otherwise have unblocked it.
        pool = active_pool[0]
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # signal.signal() only works from the main thread of the main
    # interpreter -- called from anywhere else (a caller's own
    # threading.Thread invoking cmd_run directly, e.g.) it raises
    # ValueError. cmd_run is "library-ish" (the same premise behind
    # restoring handlers unconditionally below), so a non-main-thread
    # caller is a real, contemplated audience, not a hypothetical one --
    # this must degrade, not crash before the try/finally below even
    # starts. installed_handlers records exactly which signals were
    # actually captured so the finally below restores only those, and the
    # degradation itself is recorded in `downgrades` (the same place an
    # artifact-outside-a-repo downgrade goes) so a run that cannot be
    # signal-aborted is visible in run.json rather than looking identical
    # to one that can be.
    installed_handlers: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            installed_handlers[sig] = signal.signal(sig, _handle_abort)
        except ValueError:
            pass
    if len(installed_handlers) < 2:
        downgrades.append(
            "signal-based abort handling is unavailable in this context "
            "(cmd_run was not called from the main thread); Ctrl-C/SIGTERM "
            "cannot cleanly abort this run -- isolation teardown on a kill "
            "signal is not guaranteed."
        )
    try:
        repo_root = _resolve_repo_root(artifact)
        if repo_root is None:
            downgrades.append(
                f"{artifact.name} is not inside a git repository; every friend was "
                "downgraded to doc scope (no repository to snapshot or read)."
            )
            specs = [dataclasses.replace(s, scope="doc") for s in specs]

        run_id = f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        store = RunStore(Path(args.out) if args.out else default_root(), run_id)
        frozen, digest = store.artifact_copy(artifact)
        schema_file = schema_path(store.run_dir)
        artifact_text = frozen.read_text(encoding="utf-8")

        # Every friend gets its OWN prompt, built from its own lens -- not a
        # single prompt.txt shared byte-for-byte across every friend
        # regardless of --friend cli:lens (that was the bug: the lens name
        # was recorded for bookkeeping but its prose never reached the
        # friend, so the only diversity in a run was model diversity).
        # Written to round-1/<name>.prompt next to that friend's .raw/.meta
        # so a human can see exactly what each friend was asked. A missing
        # lens file downgrades that one friend to the generic prompt rather
        # than failing the run -- see _build_friend_prompt.
        prompt_for: dict[str, Path] = {}
        advisory_for: dict[str, bool] = {}
        for spec in specs:
            prompt_text, advisory, lens_downgrade = _build_friend_prompt(spec, artifact_text)
            if lens_downgrade:
                downgrades.append(lens_downgrade)
            prompt_path = store.friend_prompt_path(1, spec.name)
            prompt_path.write_text(prompt_text, encoding="utf-8")
            prompt_for[spec.name] = prompt_path
            advisory_for[spec.name] = advisory

        # Isolation: every friend gets its own private working directory, torn
        # down at the end regardless of how dispatch finishes (including on a
        # raised exception, or an abort mid-setup -- see the `if
        # abort_event.is_set(): break` below). Repo-scope friends -- those
        # whose adapter declared readonly_argv and were not downgraded above
        # -- run inside their own git worktree checked out from one shared
        # snapshot commit; every other friend runs inside its own bare
        # doc_scope_dir holding only a copy of the artifact. Giving every
        # friend (not just non-readonly ones) a private worktree is a
        # deliberately stricter simplification of "every friend that lacks a
        # readonly capability gets its own private worktree": it trivially
        # satisfies that bar and removes any question of whether two friends
        # sharing one worktree could race each other, at the cost of one
        # `git worktree add` per repo-scope friend instead of one shared
        # checkout. The run directory itself (`store.run_dir`) is never
        # nested inside any of these -- it always lives under `--out` or
        # default_root(), never under the isolation tempdir below.
        snapshot_sha = None
        if repo_root is not None and any(s.scope == "repo" for s in specs):
            snapshot_sha = isolation.snapshot_commit(repo_root)

        results = []
        with tempfile.TemporaryDirectory(prefix="af-isolation-") as iso_root_str:
            iso_root = Path(iso_root_str)
            cwd_for: dict[str, Path] = {}
            try:
                for spec in specs:
                    if abort_event.is_set():
                        break
                    dest = iso_root / spec.name
                    if spec.scope == "repo":
                        isolation.add_worktree(repo_root, snapshot_sha, dest)
                    else:
                        isolation.doc_scope_dir(dest, artifact)
                    cwd_for[spec.name] = dest

                def _run_one(spec: FriendSpec):
                    return _dispatch(spec, cwd_for[spec.name], registry, fake_cmd,
                                     prompt_for[spec.name], schema_file, abort_event)

                # Only specs that actually got an isolation directory (i.e.
                # every one of them, unless the loop above broke early on
                # abort) are dispatched -- _run_one would otherwise KeyError
                # looking up cwd_for for a spec whose setup never happened.
                dispatch_specs = [s for s in specs if s.name in cwd_for]
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, len(dispatch_specs))
                ) as pool:
                    active_pool[0] = pool
                    try:
                        results = list(pool.map(_run_one, dispatch_specs))
                    finally:
                        active_pool[0] = None
            finally:
                for spec in specs:
                    if spec.scope == "repo" and spec.name in cwd_for:
                        isolation.remove_worktree(repo_root, cwd_for[spec.name])
                # doc_scope_dir entries are cleaned up automatically: they all
                # live under iso_root, and the TemporaryDirectory context
                # manager removes it (and everything under it) on exit below,
                # independent of whether dispatch raised.

        counter = 0
        all_claims: list[Claim] = []
        all_aliases = []
        friends_meta = []
        any_success = False
        for spec, capability, outcome in results:
            raw_path, json_path, meta_path = store.friend_paths(1, spec.name)
            raw_path.write_text(outcome.stdout, encoding="utf-8")
            meta_path.write_text(
                f"argv={outcome.argv}\nexit={outcome.exit_code}\n"
                f"duration_s={outcome.duration_s:.2f}\ntimed_out={outcome.timed_out}\n"
                f"orphans_suspected={outcome.orphans_suspected}\n",
                encoding="utf-8",
            )
            status = "ok" if outcome.failure_reason is None else f"failed: {outcome.failure_reason}"
            if outcome.orphans_suspected:
                # A leaked descendant must not look identical to a clean run --
                # surfaced in the same status column readers already check for
                # "failed", rather than a silent field only run.json carries.
                status += " [orphans suspected]"
            friends_meta.append({
                "name": spec.name, "model": spec.model, "effort": spec.effort,
                "readonly": capability.readonly, "scope": spec.scope, "status": status,
            })
            if outcome.failure_reason is not None:
                continue
            any_success = True
            incoming = []
            for finding in (outcome.result.payload or {}).get("findings", []):
                counter += 1
                incoming.append(Claim(
                    id=format_claim_id(counter), supersedes=None,
                    origin=[f"{spec.cli}/{spec.lens}"], lens=spec.lens, round=1,
                    advisory=advisory_for[spec.name], severity=finding["severity"],
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
                "artifact_hash": digest, "friends": friends_meta, "downgrades": downgrades}
        store.write_run_json(meta)
        store.write_report(render(all_claims, all_aliases, meta))
        print(store.run_dir)

        if abort_signum["value"] is not None:
            # Distinct from both branches below: a run cancelled by signal
            # is neither "succeeded" (0) nor merely "incomplete because
            # every friend failed on its own" (1) -- it never got the
            # chance to finish at all. 128+signum is the conventional
            # shell convention for "killed by signal N" and does not
            # collide with any of this tool's other exit codes (2, 3, 10,
            # 11, 1, 0).
            print(f"af: aborted by signal {abort_signum['value']}", file=sys.stderr)
            return 128 + abort_signum["value"]
        # "report" mode never gates on individual claims, but a run where not
        # one friend produced a usable result (every round failed/timed out) is
        # not a success either -- exit 1 ("gate blocked or incomplete") rather
        # than 0, so a caller cannot mistake "we ran the mechanism" for "we got
        # a trustworthy critique". Distinct from NoFriendsError's exit 3, which
        # fires before any friend is even dispatched.
        return 0 if any_success else 1
    finally:
        for sig, previous in installed_handlers.items():
            signal.signal(sig, previous)


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_adapters(ADAPTER_DIR)
    found = discover_clis(registry, shutil.which)
    with tempfile.TemporaryDirectory(prefix="af-doctor-") as tmp_str:
        tmp = Path(tmp_str)
        prompt_file = tmp / "prompt.txt"
        prompt_file.write_text("", encoding="utf-8")
        schema_file = schema_path(tmp)
        for name, adapter in sorted(registry.items()):
            if adapter.transport == "http":
                print(f"{name:10} {'n/a':8} http endpoint={adapter.endpoint} "
                      "(reachability not probed by doctor)")
                continue
            binary = shutil.which(adapter.binary) if adapter.binary else None
            # capability is always what build_argv reports for a
            # repo-scoped probe spec, never re-derived by hand -- this is
            # the same rule cmd_run follows for real dispatch (see
            # _dispatch's docstring): doctor's whole point is to tell the
            # operator what a friend would actually receive.
            probe = FriendSpec(name=f"doctor-{name}", cli=name, lens="doctor",
                               model=None, effort=None, scope="repo", timeout=1)
            _, _, cap = build_argv(adapter, probe, prompt_file, schema_file)
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

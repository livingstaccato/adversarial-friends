"""Build argv for one friend and run it.

Split out of cli.py: this is the single place a friend's adapter-derived
argv meets spawn.run_process, and the place capability is trusted rather
than re-derived (see _dispatch's own docstring below).
"""

import dataclasses
from pathlib import Path
import shutil
import threading

from . import childenv, http_transport, sandbox
from .adapters import Adapter, Capability, FriendSpec, build_argv
from .claimschema import CLAIM_CONTRACT
from .contracts import PayloadContract
from .normalize import NormalizeResult
from .spawn import SpawnResult, run_process
from .trust import check_denied_values

_DispatchResult = tuple[FriendSpec, Capability, SpawnResult]

# Spec §11.3: the runner's own kill deadline must be strictly greater than a
# friend's configured --timeout, so a CLI with its own internal timeout
# (agy --print-timeout, set by build_argv to exactly `spec.timeout` -- see
# adapters.build_argv) gets the chance to report its own timeout cleanly and
# exit before this runner would otherwise kill it out from under a
# mid-write. Distinct from spawn.GRACE_SECONDS (the much shorter
# SIGTERM->SIGKILL escalation window used once a kill has already begun).
KILL_GRACE_S = 60

# A conservative threshold for warning that a friend's prompt may trigger
# E2BIG ("Argument list too long") when its adapter places the whole prompt
# in one argv element (prompt_mode != "stdin"). Linux caps a single argv
# element near 128KiB; other POSIX platforms this runner may run on (e.g.
# macOS) size the limit differently, so this threshold is deliberately well
# under the tightest of those rather than tuned to any one OS -- the
# downgrade message itself names Linux specifically as the platform this
# figure is verified against. Comfortably under that limit so the downgrade
# is visible before a real dispatch would fail, not only after.
PROMPT_ARGV_WARN_BYTES = 100_000

# A synthetic capability for the test-only "fake" cli (see _dispatch): it
# never touches adapters.py/build_argv at all, so there is no real
# Capability to surface. Always doc-scope, no schema enforcement, no
# verifiable effort -- reported honestly rather than guessed.
_FAKE_CAPABILITY = Capability(schema=False, readonly=False, effort="none")

# A synthetic capability for a friend whose dispatch raised an unexpected
# exception (see commands.run.cmd_run's _run_one, defined inline in its
# dispatch section) before -- or instead of -- ever reaching a real
# Capability. Same values as _FAKE_CAPABILITY, but a separate name/docstring:
# this one means "unknown, because dispatch never got far enough to know,"
# not "this is the test-only cli."
_UNKNOWN_CAPABILITY = Capability(schema=False, readonly=False, effort="none")

# Whole-branch re-review, Regression 3: the stderr tail is untrusted text
# (a friend's own stderr) on a path into report.md's friend table that
# report._escape_cell alone does not fully cover -- _escape_cell neutralizes
# only `\`, `|`, and newlines (enough to keep the TABLE STRUCTURE intact),
# not the inline Markdown/HTML constructs (`**bold**`, `[text](url)`,
# `` `code` ``, a raw `<script>`/autolink) that still render as real
# emphasis, a real clickable link, or raw HTML once inside a cell. Milder
# than C2 (the table can't be broken and no finding can be forged or
# hidden), but the same class of hole one file over. Stripped outright
# rather than backslash-escaped: this string is folded into `status` and
# THEN passed through _escape_cell, which itself backslash-escapes `\` --
# escaping here first would double-escape and could reintroduce exactly the
# construct being neutralized; a short diagnostic snippet loses nothing
# essential by simply not containing these characters.
_INLINE_MARKDOWN_STRIP = str.maketrans("", "", "`*_[]<>")


def _exception_outcome(argv: list[str], exc: BaseException) -> SpawnResult:
    """Build a SpawnResult for a friend whose dispatch raised something
    spawn.run_process's own OSError handling did not already turn into a
    clean result -- e.g. a bug in adapter wiring, or an OSError that still
    somehow escaped Popen(). Mirrors spawn._early_failure's shape so
    commands.run.cmd_run's single per-friend result-processing loop needs no
    special case for "this friend never actually ran a process at all."""
    return SpawnResult(
        argv=argv,
        exit_code=None,
        stdout="",
        stderr="",
        duration_s=0.0,
        timed_out=False,
        result=NormalizeResult(None, [str(exc)], False),
        failure_reason=f"unexpected error: {exc}",
        orphans_suspected=False,
    )


def _refused_unsandboxed(argv: list[str], spec: FriendSpec, adapter: Adapter) -> SpawnResult:
    """§12.2's refusal: this friend cannot confine itself and the OS offers
    no way to confine it.

    Refused as a FAILED FRIEND rather than a raised error, deliberately. One
    unconfinable friend must not end a run that has three usable ones -- the
    same rule every other per-friend problem follows. The security property
    is unchanged either way: the process is never started. The report shows
    it as failed, with the reason and the override.
    """
    return SpawnResult(
        argv=argv,
        exit_code=None,
        stdout="",
        stderr="",
        duration_s=0.0,
        timed_out=False,
        result=NormalizeResult(None, [], False),
        failure_reason=(
            f"refused: {adapter.name} has no read-only mode, and no OS sandbox "
            f"({sandbox.SANDBOX_EXEC} on macOS, {sandbox.BWRAP} on Linux) is "
            "available to confine it. An artifact under review is untrusted "
            "text and could tell it to read anything this user can. Install "
            "one, or pass --allow-unsandboxed-friend to accept the risk."
        ),
        orphans_suspected=False,
    )


def _stderr_tail(stderr: str, max_lines: int = 2, max_chars: int = 200) -> str:
    """A short, status-column-sized excerpt of a friend's stderr -- not the
    whole capture, which lives in `round-1/<friend>.err` (see
    commands.run.cmd_run). Takes the LAST non-empty lines: the actionable
    diagnostic (an auth error, a missing env var) is usually near the end of
    a CLI's stderr, after any banner/progress noise, not the first line.
    Inline Markdown/HTML-significant characters are stripped (see
    _INLINE_MARKDOWN_STRIP above) before the length cap is applied, so
    `max_chars` bounds what a reader actually sees."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    tail = " | ".join(lines[-max_lines:])
    tail = tail.translate(_INLINE_MARKDOWN_STRIP)
    if len(tail) > max_chars:
        tail = tail[: max_chars - 1].rstrip() + "…"
    return tail


def _dispatch(
    spec: FriendSpec,
    cwd: Path,
    registry: dict[str, Adapter],
    fake_cmd: list[str] | None,
    prompt_file: Path,
    schema_file: Path,
    abort_event: threading.Event | None = None,
    contract: PayloadContract = CLAIM_CONTRACT,
    allow_unsandboxed: bool = False,
    extra_args: list[str] | None = None,
    pass_env: tuple[str, ...] = (),
) -> _DispatchResult:
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
    spawn.run_process so a signal handler in commands.run.cmd_run can stop
    this friend (and reap its whole process group) without waiting out its
    full --timeout -- see cmd_run's signal handling for why this matters: a
    cancelled run must not leave a metered agent CLI process running
    unbounded.

    The kill deadline handed to run_process is `spec.timeout + KILL_GRACE_S`,
    strictly greater than `spec.timeout` itself -- spec §11.3. Adapters with
    an internal_timeout_flag (agy) have that CLI's OWN timeout set to
    exactly `spec.timeout` by build_argv (see adapters.build_argv), so the
    two deadlines can never collide: the CLI gets the chance to report its
    own timeout cleanly and exit before this runner would otherwise kill it
    out from under a mid-write.

    `envelope`/`structured_output` come from the adapter (None/False for the
    test-only "fake" cli, which never touches adapters.py at all -- see
    _FAKE_CAPABILITY's own docstring) and are passed straight through to
    spawn.run_process/normalize; see normalize.normalize's docstring.

    `contract` selects which payload kind this friend's output is read as.
    It defaults to claims, so a critique round needs no argument; a
    cross-examination round passes the verdict contract, and the choice
    reaches both transports identically.
    """
    # None means "inherit", which is what every friend gets unless it is
    # being confined. Initialised before the branches because the fake and
    # http paths never reach the exec branch that sets it.
    child_env: dict[str, str] | None = None
    if spec.cli == "fake":
        # A spec with cli == "fake" only ever comes from
        # cliargs._specs_from_flags, which refuses to build one unless
        # AF_FAKE_FRIEND (and therefore fake_cmd) is set -- see its
        # fake_enabled check. fake_cmd is None here only if that invariant
        # was broken by a caller constructing a FriendSpec directly.
        assert fake_cmd is not None
        # The prompt file is passed so a fake friend can actually READ what
        # it was asked. Most modes ignore it and print a canned payload, but
        # a judging round's fake has to respond to the real claim ids the
        # runner generated -- ids it cannot know in advance. Without this the
        # crossexam path could only be tested against hard-coded ids, which
        # tests the fixture rather than the runner.
        #
        # Passed as a NAMED flag, not a positional: fake_friend.py's other
        # modes already take positional pidfile arguments when a test invokes
        # them directly (see tests/test_spawn.py), and appending a positional
        # here would silently turn the prompt path into one of those.
        argv = [*fake_cmd, spec.lens, f"--prompt={prompt_file}"]
        stdin_text = None
        capability = _FAKE_CAPABILITY
        envelope = None
        structured_output = False
    elif registry[spec.cli].transport == "http":
        # No process to spawn, so none of spawn.py's machinery applies --
        # see http_transport's module docstring. It returns the same
        # SpawnResult shape, so everything downstream stays
        # transport-agnostic.
        adapter = registry[spec.cli]
        return (
            spec,
            http_transport.capability_for(adapter),
            http_transport.run_request(adapter, spec, prompt_file, spec.timeout, contract),
        )
    else:
        adapter = registry[spec.cli]
        argv, stdin_text, capability = build_argv(adapter, spec, prompt_file, schema_file)
        check_denied_values(argv)
        envelope = adapter.envelope
        structured_output = adapter.structured_output
        # A friend whose binary is not installed is not sandboxed. Wrapping
        # a command that does not exist confines nothing, and it destroys the
        # diagnosis: spawn.run_process reports "binary not found" from
        # Popen's own FileNotFoundError, but once the argv starts with
        # `sandbox-exec` Popen succeeds and the real error becomes an opaque
        # exit 71 from the wrapper. A missing agent CLI is the single most
        # common setup problem this tool has; its message must not degrade
        # because the friend happened to need confinement.
        # §12.2, for EVERY exec friend and not only the confined ones. A
        # read-only flag stops a CLI writing files; it does nothing about
        # what it can read out of its own environment, and an artifact that
        # talks a friend into echoing `env` exfiltrates every token the
        # operator exported. Filtering was gated on the same condition as
        # filesystem confinement, so codex, claude and agy -- the three that
        # confine themselves -- inherited the whole environment, and the
        # allowlist 0.1.1 introduced only ever applied to opencode.
        #
        # Verified against all three before the coupling was cut: each
        # authenticates under this allowlist, because their credentials are
        # files under HOME rather than variables.
        child_env = childenv.build(adapter.env_pass, pass_env)
        binary_present = bool(adapter.binary and shutil.which(adapter.binary))
        if not adapter.readonly_argv and binary_present:
            # §12.2. This CLI enforces nothing on its own, and cwd is not
            # containment -- an artifact telling it to read
            # ~/.ssh/id_ed25519 would simply work. Confined by the OS, or
            # refused.
            #
            # **Deliberately narrower than §12.2's letter**, which keys on
            # the capability rather than the adapter. `build_argv` emits a
            # readonly flag only for repo scope, so a doc-scope claude also
            # reports `readonly=False` -- and every friend is downgraded to
            # doc scope whenever the artifact is not inside a git repository.
            # Keying on the capability would therefore refuse every friend
            # for any artifact outside a repo, and would put CLIs whose
            # credential paths this project has NOT verified under a sandbox
            # that silently breaks their authentication.
            #
            # So the rule here is "this CLI has no read-only mode at all",
            # which is the case §12.2's own example is about. The residual
            # gap -- a doc-scope friend of a readonly-capable CLI is not
            # OS-confined -- is real and recorded in the spec's divergences
            # section rather than left implied.
            mechanism = sandbox.detect()
            if mechanism is None:
                if not allow_unsandboxed:
                    return spec, capability, _refused_unsandboxed(argv, spec, adapter)
            else:
                policy = sandbox.policy_for(
                    cwd, adapter.binary, adapter.sandbox_read, adapter.sandbox_write
                )
                argv = sandbox.wrap(argv, mechanism, policy, prompt_file.with_suffix(".sandbox"))
                # Confining the filesystem while handing over every exported
                # secret would leave the boundary open straight through the
                # middle: a friend could read another service's token
                # without touching a forbidden path.
                # Scratch and state inside the isolation directory, not the
                # user's. Without this opencode needed a read grant over the
                # whole of $TMPDIR -- which holds every other friend's
                # isolation tree -- and a write grant over its own home
                # state directory, which outlives the run. Only for confined
                # friends: a self-confining CLI keeps its real config, which
                # is where its credentials live.
                child_env.update(childenv.private_dirs(cwd))
    if extra_args and spec.cli != "fake":
        # §13: their presence forces readonly False in the header regardless
        # of what the argv appears to say. The runner cannot know what an
        # unvalidated flag does -- it may well have re-enabled writes -- so
        # the honest report is that read-only was not verified, not that the
        # flag the adapter emitted is still in force.
        argv = [*argv, *extra_args]
        capability = dataclasses.replace(capability, readonly=False)
    outcome = run_process(
        argv,
        stdin_text,
        spec.timeout + KILL_GRACE_S,
        cwd,
        abort_event=abort_event,
        envelope=envelope,
        structured_output=structured_output,
        contract=contract,
        env=child_env,
    )
    return spec, capability, outcome

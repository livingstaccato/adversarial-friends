"""Build argv for one friend and run it.

Split out of cli.py: this is the single place a friend's adapter-derived
argv meets spawn.run_process, and the place capability is trusted rather
than re-derived (see _dispatch's own docstring below).
"""

from pathlib import Path
import threading

from . import http_transport
from .adapters import Adapter, Capability, FriendSpec, build_argv
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
    """
    if spec.cli == "fake":
        # A spec with cli == "fake" only ever comes from
        # cliargs._specs_from_flags, which refuses to build one unless
        # AF_FAKE_FRIEND (and therefore fake_cmd) is set -- see its
        # fake_enabled check. fake_cmd is None here only if that invariant
        # was broken by a caller constructing a FriendSpec directly.
        assert fake_cmd is not None
        argv = [*fake_cmd, spec.lens]
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
            http_transport.run_request(adapter, spec, prompt_file, spec.timeout),
        )
    else:
        adapter = registry[spec.cli]
        argv, stdin_text, capability = build_argv(adapter, spec, prompt_file, schema_file)
        check_denied_values(argv)
        envelope = adapter.envelope
        structured_output = adapter.structured_output
    outcome = run_process(
        argv,
        stdin_text,
        spec.timeout + KILL_GRACE_S,
        cwd,
        abort_event=abort_event,
        envelope=envelope,
        structured_output=structured_output,
    )
    return spec, capability, outcome

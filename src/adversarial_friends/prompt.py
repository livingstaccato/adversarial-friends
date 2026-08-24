"""Build each friend's prompt from the generic contract header plus its lens.

Split out of cli.py: every friend gets its OWN prompt, built from its own
lens -- not a single prompt.txt shared byte-for-byte across every friend
regardless of --friend cli:lens (that was the bug this file's functions
exist to prevent: the lens name was recorded for bookkeeping but its prose
never reached the friend, so the only diversity in a run was model
diversity).
"""

from .adapters import FriendSpec
from .paths import LENS_DIR

# Both keys appear in every reply, one of them null. That is not stylistic:
# strict structured output (codex) requires `required` to name every property,
# so a schema-enforcing friend must emit both -- and a prompt telling it to
# return only one contradicts the schema it was handed. Found when agy
# rejected its own model's output with "at '/no_findings': got object".
#
# The two-key shape also keeps §7.3's distinction intact: "found nothing" is
# an explicit marker, not an empty array, because a friend that returns
# nothing and does not say so is failed rather than clean.
PROMPT_HEADER = (
    "You are an adversarial reviewer. Read the artifact below and challenge it.\n"
    "Return ONLY a JSON object with BOTH of these keys, exactly one of them null:\n"
    '{"no_findings": null, "findings":[{"severity":"high|medium|low",'
    '"claim":"...","location":"...","evidence":"...",'
    '"failure_scenario":"...","suggested_fix":"..."}]}\n'
    'If you find nothing, return exactly {"no_findings": true, "findings": null}.\n'
    "Never omit either key.\n"
)


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
                body = "\n".join(lines[i + 1 :]).strip("\n")
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
    friend looks for, not merely label its output after the fact.

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
        note = (
            f"{spec.name}: no lens file found for lens {spec.lens!r}; ran "
            "with the generic prompt only, with no lens-specific guidance."
        )
        return prompt, False, note
    meta, body = loaded
    advisory = meta.get("requires_failure_scenario", "true").strip().lower() == "false"
    prompt = PROMPT_HEADER + "\n--- LENS ---\n" + body + "\n\n--- ARTIFACT ---\n" + artifact_text
    return prompt, advisory, None

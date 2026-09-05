import pytest

from afriend import ids
from afriend.errors import UsageError


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


@pytest.mark.parametrize(
    "name",
    [
        "../../../../tmp/owned",  # path traversal
        "Codex",  # uppercase
        "-leading-hyphen",
        "has space",
        "a" * 33,  # too long
        "",
    ],
)
def test_invalid_friend_names_rejected(name):
    with pytest.raises(UsageError):
        ids.validate_friend_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "codex-ops\n",  # trailing newline: Python's $ matches before it
        "codex-ops\n.raw",
        "codex\tops",
        "codex\x00ops",
    ],
)
def test_control_characters_are_rejected(name):
    with pytest.raises(UsageError):
        ids.validate_friend_name(name)


@pytest.mark.parametrize("cid", ["c-0007@1\n", "c-٠٠٠٧@1"])  # noqa: RUF001 -- deliberate non-ASCII digits, this is the thing under test
def test_claim_id_rejects_trailing_newline_and_non_ascii_digits(cid):
    with pytest.raises(UsageError):
        ids.parse_claim_id(cid)

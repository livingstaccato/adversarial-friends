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
])
def test_invalid_friend_names_rejected(name):
    with pytest.raises(UsageError):
        ids.validate_friend_name(name)

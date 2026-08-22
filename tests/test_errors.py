from adversarial_friends.errors import AfError, CeilingError, NoFriendsError, UsageError


def test_default_exit_code_is_1():
    assert AfError("boom").exit_code == 1


def test_subclass_exit_codes():
    assert UsageError("bad flag").exit_code == 2
    assert NoFriendsError("none found").exit_code == 3
    assert CeilingError("budget").exit_code == 11


def test_constructor_can_override_exit_code():
    assert AfError("custom", exit_code=10).exit_code == 10


def test_message_is_preserved():
    assert str(UsageError("bad flag")) == "bad flag"

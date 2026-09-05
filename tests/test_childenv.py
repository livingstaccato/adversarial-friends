"""Tests for executable friend environment filtering (spec §12.2, §12.3).

Found by running this tool against its own sandbox: the filesystem policy was
careful and the environment was not filtered at all, so a friend inherited
every secret exported in the runner's shell. On the machine this was found
on, 61 variables were exposed, including four API tokens for services that
have nothing to do with reviewing code.
"""

from afriend import childenv

SHELL = {
    "PATH": "/usr/bin",
    "HOME": "/home/t",
    "LANG": "en_US.UTF-8",
    "AWS_SECRET_ACCESS_KEY": "shhh",
    "GITHUB_TOKEN": "ghp_xxx",
    "DATABASE_URL": "postgres://user:pw@host/db",
    "OPENAI_API_KEY": "sk-xxx",
}


def test_a_secret_the_friend_has_no_business_with_is_withheld():
    kept = childenv.build(environ=SHELL)
    assert "AWS_SECRET_ACCESS_KEY" not in kept
    assert "GITHUB_TOKEN" not in kept
    assert "DATABASE_URL" not in kept


def test_the_basics_survive():
    """A filter that dropped PATH would not be secure, it would be broken."""
    kept = childenv.build(environ=SHELL)
    assert kept["PATH"] == "/usr/bin"
    assert kept["HOME"] == "/home/t"
    assert kept["LANG"] == "en_US.UTF-8"


def test_an_adapter_can_declare_what_its_cli_needs():
    """§12.3 already accepts that a friend can exfiltrate its OWN
    credentials. Passing those and withholding everyone else's is the whole
    distinction this makes."""
    kept = childenv.build(adapter_pass=("OPENAI_API_KEY",), environ=SHELL)
    assert kept["OPENAI_API_KEY"] == "sk-xxx"
    assert "GITHUB_TOKEN" not in kept


def test_the_operator_can_add_one_the_adapter_did_not_declare():
    """The escape hatch: a filter that guesses wrong breaks authentication
    with no useful error, so the operator who knows better can say so."""
    kept = childenv.build(operator_pass=("DATABASE_URL",), environ=SHELL)
    assert "DATABASE_URL" in kept


def test_an_unset_variable_is_not_invented():
    """Exporting an empty value would tell a CLI a setting exists when it
    does not -- its own source of confusing failures."""
    kept = childenv.build(adapter_pass=("NOT_SET_ANYWHERE",), environ=SHELL)
    assert "NOT_SET_ANYWHERE" not in kept


def test_withheld_reports_names_only():
    """This list reaches run.json and report.md. Recording the values to
    prove they were protected would be the leak it exists to prevent."""
    names = childenv.withheld(environ=SHELL)
    assert "AWS_SECRET_ACCESS_KEY" in names
    assert "shhh" not in names
    assert not any("sk-xxx" in n or "ghp_xxx" in n for n in names)


def test_withheld_and_kept_partition_the_environment():
    kept = set(childenv.build(environ=SHELL))
    dropped = set(childenv.withheld(environ=SHELL))
    assert kept | dropped == set(SHELL)
    assert not (kept & dropped)


def test_nothing_in_the_base_list_looks_like_a_credential():
    """The base list is passed to every executable friend, so a credential
    hiding in it would be passed to all of them unconditionally."""
    import re

    for name in childenv.BASE_PASS:
        assert not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CRED", name, re.I), name

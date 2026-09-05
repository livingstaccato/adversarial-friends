"""Doctor projections of the normal run's host-inclusion default."""

import argparse
import json

import pytest

from afriend import adapters, readiness as readiness_module
from afriend.commands import doctor as doctor_module
from afriend.paths import ADAPTER_DIR
from afriend.providerconfig import ProviderPolicy, ProviderSetting


@pytest.mark.parametrize(
    ("provider", "marker", "expected_state", "expected_usable"),
    [
        ("codex", "CODEX_SESSION_ID", "ready", 1),
        ("claude", "CLAUDECODE", "host-excluded", 0),
    ],
)
def test_doctor_reports_normal_run_host_inclusion_default(
    monkeypatch, capsys, provider, marker, expected_state, expected_usable
):
    adapter = adapters.load_adapters(ADAPTER_DIR)[provider]
    monkeypatch.setattr(doctor_module, "load_adapters", lambda _path: {provider: adapter})
    monkeypatch.setattr(
        doctor_module.providerconfig,
        "load",
        lambda *_args: ProviderPolicy({provider: ProviderSetting(enabled=True)}),
    )
    for host_marker in readiness_module.HOST_ENV_MARKERS:
        monkeypatch.delenv(host_marker, raising=False)
    monkeypatch.setenv(marker, "test-session")
    monkeypatch.setattr(doctor_module.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        readiness_module,
        "probe_deny_argv",
        lambda *_args: readiness_module.DenyProbeResult(True, "verified test shim"),
    )

    code = doctor_module.cmd_doctor(argparse.Namespace(json=True, gc=False, out=None))
    payload = json.loads(capsys.readouterr().out)

    assert payload["friends"][0]["state"] == expected_state
    assert payload["usable"] == expected_usable
    assert code == (0 if expected_usable else 3)


def test_doctor_projects_excluded_uncontrolled_host_without_enforcement(monkeypatch, capsys):
    provider = "opencode"
    adapter = adapters.load_adapters(ADAPTER_DIR)[provider]
    monkeypatch.setattr(doctor_module, "load_adapters", lambda _path: {provider: adapter})
    monkeypatch.setattr(
        doctor_module.providerconfig,
        "load",
        lambda *_args: ProviderPolicy({provider: ProviderSetting(enabled=True)}),
    )
    for host_marker in readiness_module.HOST_ENV_MARKERS:
        monkeypatch.delenv(host_marker, raising=False)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-session")
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _binary: "/bin/opencode")
    monkeypatch.setattr(
        doctor_module,
        "enforce",
        lambda *_args: pytest.fail("excluded host must not reach authority enforcement"),
    )
    monkeypatch.setattr(
        doctor_module,
        "build_argv",
        lambda *_args: pytest.fail("excluded host must not reach argv construction"),
    )

    code = doctor_module.cmd_doctor(argparse.Namespace(json=True, gc=False, out=None))
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["usable"] == 0
    assert payload["friends"] == [
        {
            "auth_classifiable": adapter.auth.declared(),
            "effort": adapter.effort_kind,
            "external_tools": "uncontrolled",
            "model": None,
            "name": provider,
            "readonly": False,
            "reason": "excluded because it is the detected host provider",
            "schema": False,
            "state": "host-excluded",
            "status": "host-excluded",
            "where": "/bin/opencode",
        }
    ]

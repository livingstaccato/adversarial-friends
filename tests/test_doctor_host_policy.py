"""Doctor projections of the normal run's host-inclusion default."""

import argparse
import json

import pytest

from adversarial_friends import adapters, readiness as readiness_module
from adversarial_friends.commands import doctor as doctor_module
from adversarial_friends.paths import ADAPTER_DIR
from adversarial_friends.providerconfig import ProviderPolicy, ProviderSetting


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

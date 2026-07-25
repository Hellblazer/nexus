# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ambient-service-env scrub actually scrubs (nexus-dvom6).

``_isolate_service_endpoint_env`` (tests/conftest.py) removes the four
service-endpoint env vars from every unit test. Asserting their absence from
inside a test would be vacuous -- they are absent on any machine that never
sourced the credentials, which is every CI runner. So the proof runs a
previously-polluted test in a SUBPROCESS with the env deliberately set.

Delete the fixture and this fails. That is the whole point.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Exactly what a developer shell looks like after
#: ``source ~/.config/nexus/activate.sh``, plus the two env-first tier vars.
_POLLUTED = {
    "NX_SERVICE_URL": "https://ambient.example.invalid",
    "NX_SERVICE_TOKEN": "ambient-token-that-must-not-leak",
    "NX_SERVICE_HOST": "ambient.example.invalid",
    "NX_SERVICE_PORT": "9999",
}

#: Representatives of the two failure shapes the leak produced: an assertion
#: on the "nothing resolvable" error text, and a lease-tier test that env-first
#: resolution short-circuits before it can run.
_CANARIES = [
    "tests/db/test_http_telemetry_store.py::TestConfigErrors::test_missing_port_raises",
    "tests/db/test_om64x_stale_port_recovery.py::TestRecoverEndpointFromLease",
    "tests/db/test_om64x_stale_port_recovery.py::TestTokenStoreRecovery",
    "tests/db/test_om64x_stale_port_recovery.py::TestScratchStoreRecovery",
]


@pytest.mark.parametrize("target", _CANARIES)
def test_polluted_env_does_not_break_endpoint_tests(target: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header"],
        env={**os.environ, **_POLLUTED},
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"{target} failed with ambient service env set — the "
        "_isolate_service_endpoint_env scrub is not covering it.\n"
        f"{proc.stdout[-3000:]}"
    )


def test_scrub_is_visible_to_the_running_test() -> None:
    """Companion to the subprocess proof: inside a test, the four vars are
    gone regardless of what the developer's shell exported.

    Vacuous on a clean machine by itself — it earns its keep only next to the
    parametrized subprocess test above, which supplies the pollution. Kept
    because it names the invariant at the point of use.
    """
    for var in _POLLUTED:
        assert os.environ.get(var) is None, f"{var} leaked into the test process"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0ite2: this repo's real ``tests/conftest.py`` decides the test substrate, pinned
in a child pytest for the two values nothing else runs through it.

``tests/test_storage_touch_guard.py`` proves the guard against a stand-in conftest, and
``tests/test_optin_tenant_end_to_end.py`` runs the real one only for an explicit ``optin``
and only where the service jar is built. Neither pins what an UNSET
``NX_TEST_T2_SUBSTRATE`` does (the default every contributor and CI shard runs) or that
the retired ``per-test`` hatch is refused (nexus-llo7w), so flipping the default or
reviving the hatch changed nothing red. The child here loads the real conftest as a plugin and runs a tiny generated module;
two things differ from a plain run, both stated in the child's ``conftest.py``: the
session-start gates (built jar, box lease, build lease) are no-ops and the two tenant
fixtures are stubs that log the request instead of booting an engine. The decision under
test, ``_pin_t2_substrate`` and ``_pin_local_posture_optin``, runs unchanged, so the log
says which tests the real conftest asked a tenant for.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_CHILD_CONFTEST = '''
import os
from pathlib import Path

import pytest

import tests.conftest as repo

# the session-start gates look for a built engine jar and a free box lease, and the T1 mint
# needs a service: none of that is the subject, so they do nothing here
repo._gate_on_build_lease = repo._take_suite_lease = repo._preflight_engine_substrate = lambda: None
repo._mint_t1_session_env = lambda monkeypatch: None

LOG = Path(os.environ["PROBE_LOG"])


def note(line):
    with LOG.open("a") as fh:
        fh.write(line + "\\n")


@pytest.fixture
def t2_tenant(request):
    note("t2_tenant:" + request.node.name)
    yield "probe-tenant"


@pytest.fixture
def t2_service_env(request):
    note("t2_service_env:" + request.node.name)
    yield "probe-tenant"
'''

_CHILD_TESTS = '''
import os

import pytest

from conftest import note


def test_plain():
    note("plain NX_LOCAL=%s NX_SERVICE_URL=%s" % (os.environ.get("NX_LOCAL"), "NX_SERVICE_URL" in os.environ))


@pytest.mark.t2_tenant
def test_marked():
    pass


@pytest.mark.integration
def test_integration_marked():
    pass
'''

#: variables the outer run owns and a child must start without: its tenant, T1 session,
#: posture, and whichever sweep switches the outer run was given
_SCRUBBED = ("NX_LOCAL", "NX_TEST_T2_SUBSTRATE", "NX_STORAGE_TOUCH_SWEEP", "NX_STORAGE_TOUCH_SWEEP_FLOOR")
_SCRUBBED_PREFIXES = ("NX_SERVICE_", "NX_T1_", "NX_STORAGE_BACKEND")


def _child(tmp_path: Path, substrate: str | None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    (tmp_path / "conftest.py").write_text(_CHILD_CONFTEST)
    (tmp_path / "test_probe.py").write_text(_CHILD_TESTS)
    log = tmp_path / "probe.log"
    env = {
        k: v for k, v in os.environ.items()
        if k not in _SCRUBBED and not k.startswith(_SCRUBBED_PREFIXES)
    }
    env["PROBE_LOG"] = str(log)
    # its own build lease, so a Maven run elsewhere on the box cannot reach it (nexus-fam6l)
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / "build-lease")
    if substrate is not None:
        env["NX_TEST_T2_SUBSTRATE"] = substrate
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(tmp_path / "test_probe.py"), "-p", "tests.conftest",
            "-c", str(REPO_ROOT / "pyproject.toml"), "--rootdir", str(REPO_ROOT),
            "-p", "no:cacheprovider", "-p", "no:xdist", "-q",
            # after the addopts' own, so the integration-marked case is selected
            "-m", "not lint",
        ],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return proc, lines


def test_an_unset_substrate_mints_only_for_a_marked_test_and_pins_the_local_posture(tmp_path: Path) -> None:
    proc, log = _child(tmp_path, None)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "3 passed" in out, out[-3000:]
    # the unmarked test asked for nothing and saw the local posture with no service endpoint;
    # the marker and the integration marker (kept per test) asked for t2_tenant, never t2_service_env
    assert sorted(log) == sorted([
        "plain NX_LOCAL=1 NX_SERVICE_URL=False",
        "t2_tenant:test_marked",
        "t2_tenant:test_integration_marked",
    ]), log
    assert "storage-touch guard" in out, "the guard runs under the default substrate"


@pytest.mark.parametrize("substrate", ["per-test", "engine"])
def test_the_retired_per_test_hatch_is_refused_by_name(tmp_path: Path, substrate: str) -> None:
    """The escape hatch is retired (nexus-llo7w): every test errors at setup naming the
    variable and the bead, and none asks for a tenant on the way."""
    proc, log = _child(tmp_path, substrate)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "3 error" in out, out[-3000:]
    assert f"NX_TEST_T2_SUBSTRATE={substrate}: the per-test escape hatch" in out, out[-3000:]
    assert "nexus-llo7w" in out and "pytest.mark.t2_tenant" in out, out[-3000:]
    assert [line for line in log if line.startswith("t2_")] == [], log


def test_none_asks_no_test_for_a_tenant(tmp_path: Path) -> None:
    proc, log = _child(tmp_path, "none")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "3 passed" in out, out[-3000:]
    # `none` provisions nothing, whatever a test is marked: the marker is a no-op there
    assert [line for line in log if line.startswith("t2_")] == [], log

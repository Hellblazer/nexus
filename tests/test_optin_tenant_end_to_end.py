# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni: this repo's real ``tests/conftest.py`` wires the opt-in tenant.

``tests/test_storage_touch_guard.py`` proves the guard with a stand-in tenant
fixture; it cannot show that the REAL ``t2_tenant`` fixture, the
``pytest.mark.t2_tenant`` marker, the ``NX_LOCAL`` posture pin and the T1 mint
cooperate with it. So this runs a child pytest, real conftest included, under
``NX_TEST_T2_SUBSTRATE=optin`` over ``tests/_optin_tenant_probe.py`` and reads
the guard's report. The child boots its own engine, so the case needs the
service jar, and says so when it cannot run rather than passing empty.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.db._service_fixture import jar_freshness_skip_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = "tests/_optin_tenant_probe.py"


def test_the_real_conftest_provisions_by_marker_and_by_parameter_only(tmp_path: Path) -> None:
    if os.environ.get("NX_TEST_T2_SUBSTRATE") == "none":
        pytest.skip("outer run provisions no engine (NX_TEST_T2_SUBSTRATE=none); the child needs one")
    reason = jar_freshness_skip_reason()
    if reason is not None:
        # The same fail-loud arm as the engine substrate: once a run says the jar is
        # provisioned, an absent one is a failure, not a skip.
        if os.environ.get("NX_T2_SUBSTRATE_EXPECTED"):
            pytest.fail(f"NX_T2_SUBSTRATE_EXPECTED is set but the child cannot boot an engine: {reason}")
        pytest.skip(f"the child boots an engine: {reason}")

    # The outer test has its own tenant in os.environ (service endpoint, T1
    # session, posture); the child must start without it, or the probe's
    # "no tenant" case would be reading the outer test's.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("NX_SERVICE_", "NX_T1_", "NX_STORAGE_BACKEND")) and k != "NX_LOCAL"
    }
    env["NX_TEST_T2_SUBSTRATE"] = "optin"
    env["NX_STORAGE_TOUCH_GUARD_MODE"] = "report"
    # The child is a full session: give it its own build-lease root so a Maven
    # run elsewhere on the box cannot make it exit 75 (nexus-fam6l). It stays
    # inside the outer suite lease through the inherited holder variable.
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / "build-lease")
    env.pop("NX_BUILD_LEASE_WAIT", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _PROBE, "-q", "-p", "no:cacheprovider", "-p", "no:xdist"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-4000:]
    assert re.search(r"\b5 passed\b", out), out[-4000:]

    m = re.search(r"storage-touch report: (\S+)", out)
    assert m, out[-4000:]
    report = json.loads(Path(m.group(1)).read_text())
    c = report["counts"]
    # marker, parameter and cloud_mode+parameter get a tenant; exactly one test
    # touches storage without one; exactly one touches nothing.
    assert (c["tests"], c["provisioned"], c["touched_unprovisioned"], c["clean_unprovisioned"]) == (5, 3, 1, 1), c
    assert [f["nodeid"].split("::")[-1] for f in report["findings"]] == [
        "test_an_unprovisioned_storage_touch_is_recorded"
    ]

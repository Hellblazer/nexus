# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fpwj1: this repo's real ``tests/conftest.py`` wires the tenant pool behind ``NX_TEST_TENANT_POOL``.

``tests/test_tenant_pool.py`` drives the pool object; it cannot show that ``t2_service_env`` takes the pool, that
``@pytest.mark.fresh_tenant`` opts out, or that with the flag unset nothing changes. So this runs a child pytest, real
conftest included, over ``tests/_tenant_pool_probe.py`` twice (flag on, flag off) and reads what each case saw. The
child boots its own engine, so the case needs the service jar and says so when it cannot run rather than passing empty.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.db._service_fixture import jar_freshness_skip_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = "tests/_tenant_pool_probe.py"


def _run_child(tmp_path: Path, label: str, flag: str | None) -> tuple[str, dict[str, str]]:
    out = tmp_path / f"{label}.tenants"
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("NX_SERVICE_", "NX_T1_", "NX_STORAGE_BACKEND", "NX_TEST_TENANT_POOL")) and k != "NX_LOCAL"
    }
    env["NX_TEST_T2_SUBSTRATE"] = "optin"
    env["NX_STORAGE_TOUCH_GUARD_MODE"] = "report"
    env["NX_POOL_PROBE_OUT"] = str(out)
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / f"build-lease-{label}")
    env.pop("NX_BUILD_LEASE_WAIT", None)
    if flag is not None:
        env["NX_TEST_TENANT_POOL"] = flag
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _PROBE, "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-m", "not lint"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text[-4000:]
    assert re.search(r"\b4 passed\b", text), text[-4000:]
    seen = dict(line.split(" ", 1) for line in out.read_text().splitlines())
    assert sorted(seen) == ["test_a_writes_a_note", "test_b_sees_no_note_from_the_test_before",
                            "test_c_opts_out_and_gets_its_own_tenant"], seen
    return text, seen


def test_the_real_conftest_pools_by_flag_and_changes_nothing_without_it(tmp_path: Path) -> None:
    if os.environ.get("NX_TEST_T2_SUBSTRATE") == "none":
        pytest.skip("outer run provisions no engine (NX_TEST_T2_SUBSTRATE=none); the child needs one")
    reason = jar_freshness_skip_reason()
    if reason is not None:
        if os.environ.get("NX_T2_SUBSTRATE_EXPECTED"):
            pytest.fail(f"NX_T2_SUBSTRATE_EXPECTED is set but the child cannot boot an engine: {reason}")
        pytest.skip(f"the child boots an engine: {reason}")

    text, seen = _run_child(tmp_path, "on", "1")
    a, b, c = (seen[k] for k in sorted(seen))
    assert a == b, f"flag on: the two ordinary tests share the worker's one pool tenant ({seen})"
    assert c != a, f"flag on: the fresh_tenant test gets its own ({seen})"
    # the second test found no note the first wrote (asserted inside the probe, so a pass IS the proof), and:
    line = re.search(r"^tenant pool: (.*)$", text, re.MULTILINE)
    assert line, text[-3000:]
    assert re.search(r"leases=2 mints=1 retired=0 fresh_tenant=1 workers=1\b", line.group(1)), line.group(1)
    assert "VIOLATION" not in text

    text, seen = _run_child(tmp_path, "off", None)
    a, b, c = (seen[k] for k in sorted(seen))
    assert len({a, b, c}) == 3, f"flag unset: every storage-touching test mints its own tenant ({seen})"
    assert "tenant pool:" not in text, "flag unset: the plugin is not even registered"

    text, seen = _run_child(tmp_path, "zero", "0")
    assert len(set(seen.values())) == 3 and "tenant pool:" not in text, "NX_TEST_TENANT_POOL=0 is off"

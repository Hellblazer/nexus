# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fpwj1: this repo's real ``tests/conftest.py`` wires the tenant pool behind ``NX_TEST_TENANT_POOL``.

``tests/test_tenant_pool.py`` drives the pool object; it cannot show that ``t2_service_env`` takes the pool, that
``@pytest.mark.fresh_tenant`` opts out, or that ``NX_TEST_TENANT_POOL=0`` changes nothing. So this runs a child pytest,
real conftest included, over ``tests/_tenant_pool_probe.py`` twice (flag unset, which is ON since nexus-7zloy, and
flag ``0``, which is off) and reads what each case saw. The child boots its own engine, so the case needs the service
jar and says so when it cannot run rather than passing empty.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _tenant_pool
from tests.db._service_fixture import jar_freshness_skip_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = "tests/_tenant_pool_probe.py"

#: Engine flags the pool-on substrate sets itself; an ambient value would make the off arm's posture the operator's.
_ENGINE_POSTURE_ENV = (_tenant_pool.ROUTE_ENV, "NX_SEARCH_PCI", "NX_REAPER_ENABLED")


def _child_env(tmp_path: Path, label: str, flag: str | None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("NX_SERVICE_", "NX_T1_", "NX_STORAGE_BACKEND", "NX_TEST_TENANT_POOL"))
        and k != "NX_LOCAL" and k not in _ENGINE_POSTURE_ENV
    }
    env["NX_TEST_T2_SUBSTRATE"] = "optin"
    env["NX_STORAGE_TOUCH_GUARD_MODE"] = "report"
    env.pop("NX_BUILD_LEASE_WAIT", None)
    if flag is not None:
        env["NX_TEST_TENANT_POOL"] = flag
    return env


def _engine_gate() -> None:
    """Skip, or fail when the lane says an engine is expected, if this process cannot run an engine-backed child."""
    if os.environ.get("NX_TEST_T2_SUBSTRATE") == "none":
        pytest.skip("outer run provisions no engine (NX_TEST_T2_SUBSTRATE=none); the child needs one")
    reason = jar_freshness_skip_reason()
    if reason is not None:
        if os.environ.get("NX_T2_SUBSTRATE_EXPECTED"):
            pytest.fail(f"NX_T2_SUBSTRATE_EXPECTED is set but the child cannot boot an engine: {reason}")
        pytest.skip(f"the child boots an engine: {reason}")


def _run_child(tmp_path: Path, label: str, flag: str | None) -> tuple[str, dict[str, str], dict[str, object]]:
    out = tmp_path / f"{label}.tenants"
    env = _child_env(tmp_path, label, flag)
    env["NX_POOL_PROBE_OUT"] = str(out)
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / f"build-lease-{label}")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _PROBE, "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-m", "not lint"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text[-4000:]
    assert re.search(r"\b5 passed\b", text), text[-4000:]
    seen = dict(line.split(" ", 1) for line in out.read_text().splitlines())
    assert sorted(seen) == ["test_a_writes_a_note", "test_b_sees_no_note_from_the_test_before",
                            "test_c_opts_out_and_gets_its_own_tenant"], seen
    posture = json.loads(Path(str(out) + ".posture").read_text())
    return text, seen, posture


@pytest.fixture(scope="module")
def default_child(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, dict[str, str], dict[str, object]]:
    """The probe under the real conftest with ``NX_TEST_TENANT_POOL`` unset: the default, which is the pool on."""
    _engine_gate()
    return _run_child(tmp_path_factory.mktemp("pool-default"), "default", None)


@pytest.fixture(scope="module")
def zero_child(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, dict[str, str], dict[str, object]]:
    """The probe with ``NX_TEST_TENANT_POOL=0``: the pool off, the engine exactly the one develop boots."""
    _engine_gate()
    return _run_child(tmp_path_factory.mktemp("pool-zero"), "zero", "0")


def test_the_real_conftest_pools_by_default(default_child: tuple[str, dict[str, str], dict[str, object]]) -> None:
    text, seen, _posture = default_child
    a, b, c = (seen[k] for k in sorted(seen))
    assert a == b, f"flag unset: the two ordinary tests share the worker's one pool tenant ({seen})"
    assert c != a, f"flag unset: the fresh_tenant test gets its own ({seen})"
    # the second test found no note the first wrote (asserted inside the probe, so a pass IS the proof), and:
    line = re.search(r"^tenant pool: (.*)$", text, re.MULTILINE)
    assert line, text[-3000:]
    assert re.search(r"leases=2 mints=1 retired=0 fresh_tenant=1 workers=1 leaf_dead_tup=\d+ leaf_live_tup=\d+ leaf_bytes=\d+\b", line.group(1)), line.group(1)
    assert "VIOLATION" not in text


def test_the_real_conftest_changes_nothing_with_the_pool_off(zero_child: tuple[str, dict[str, str], dict[str, object]]) -> None:
    text, seen, _posture = zero_child
    assert len(set(seen.values())) == 3, f"NX_TEST_TENANT_POOL=0: every storage-touching test mints its own tenant ({seen})"
    assert "tenant pool:" not in text, "NX_TEST_TENANT_POOL=0: the plugin is not even registered"


def test_the_pool_on_substrate_engine_has_the_reset_route_pci_off_and_the_reaper_off(
    default_child: tuple[str, dict[str, str], dict[str, object]],
) -> None:
    """The pool wipes and then censuses a tenant the engine must not write to in between. The reset route and PCI off
    are what the pool needs; the reaper off (``NX_REAPER_ENABLED=false``) is what keeps its first pass, 60 s after
    boot, from writing a ``gc_audit`` row for a pooled tenant between a wipe and its census (nexus-7zloy A/B)."""
    _text, _seen, posture = default_child
    assert posture == {"route": True, "pci_builder": "off", "reaper_enabled": False}, posture


def test_without_the_pool_the_substrate_engine_has_none_of_the_pool_flags(
    zero_child: tuple[str, dict[str, str], dict[str, object]],
) -> None:
    """The ``NX_TEST_TENANT_POOL=0`` arm is exactly the engine develop boots: no reset route, per-collection index
    builds not forced off, the reaper running."""
    _text, _seen, posture = zero_child
    assert posture["route"] is False, posture
    assert posture["pci_builder"] != "off", posture
    assert posture["reaper_enabled"] is True, posture


def test_the_pools_own_engine_tests_run_in_a_child_with_the_pool_on_and_all_pass(tmp_path: Path) -> None:
    """``tests/test_tenant_pool.py`` needs an engine with the reset route on and PCI off, which the substrate boots
    only while the pool is on; with ``NX_TEST_TENANT_POOL=0`` its engine-backed tests skip. This is that lane's
    coverage of them: a child with the pool on, which must run at least one test and pass every one, with nothing
    skipped (a child whose tests all skipped proves nothing). In a default run the pool is on and the file runs
    directly, so this skips."""
    if _tenant_pool.enabled():
        pytest.skip("this run has the pool on: tests/test_tenant_pool.py runs here directly")
    _engine_gate()
    env = _child_env(tmp_path, "pool-tests", "1")
    env["NX_BUILD_LEASE_ROOT"] = str(tmp_path / "build-lease-pool-tests")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_tenant_pool.py", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-m", "not lint"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=1500,
    )
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text[-4000:]
    summary = re.search(r"^=* ?(\d+) passed[^\n]*$", text, re.MULTILINE)
    assert summary, text[-3000:]
    assert int(summary.group(1)) > 0, summary.group(0)
    assert not re.search(r"\b(skipped|failed|error|errors|deselected)\b", summary.group(0)), summary.group(0)

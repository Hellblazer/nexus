# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni phase 3: the storage-touch guard's report, recorders and
durability (``tests/_storage_touch_guard.py``).

Phase 1-2 cases live in ``tests/test_storage_touch_guard.py`` (whose helpers
this reuses). Each case here runs the real plugin in a nested ``pytester``
session with the same stand-in ``t2_tenant`` fixture.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from tests._storage_touch_guard import CHILD_LOG_ENV, REPORT_DIR_ENV, SHIM_DIR
from tests.test_storage_touch_guard import REPO_ROOT, _CONFTEST, _TOUCH, _report, _run


# ── a report a person can annotate from ──────────────────────────────────────


def test_a_touch_names_the_test_frame_that_started_it(pytester, monkeypatch) -> None:
    """The origin is the OUTERMOST test-side frame; the inner-frame window is the
    innermost six, so a deep call chain pushes the test out of it."""
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def _deep(n):
    return touch_storage_and_swallow() if n == 0 else _deep(n - 1)

def test_origin():
    _deep(9)
''', mode="report")
    result.assert_outcomes(passed=1)
    (finding,) = _report(result)["findings"]
    assert re.fullmatch(r"test_inner\.py:\d+ in test_origin", finding["origin"]), finding["origin"]
    assert not any("test_origin" in line for line in finding["stack"]), finding["stack"]
    assert any("helpers.py" in line for line in finding["stack"])


def test_the_fixture_being_set_up_is_named_and_rolled_up(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.fixture
def inner_seed():
    touch_storage_and_swallow()
    return 1

@pytest.fixture
def outer_seed(inner_seed):
    return inner_seed + 1          # touches nothing itself

def test_a(outer_seed):
    assert outer_seed == 2

def test_b(inner_seed):
    touch_storage_and_swallow()    # call phase: no fixture

def test_c():
    touch_storage_and_swallow()
''', mode="report")
    result.assert_outcomes(passed=3)
    report = _report(result)
    by_id = {f["nodeid"].split("::")[-1]: f for f in report["findings"]}
    assert by_id["test_a"]["fixtures"] == ["inner_seed"]
    assert by_id["test_b"]["fixtures"] == ["inner_seed"]  # the setup touch; the call touch has none
    assert by_id["test_c"]["fixtures"] == []
    by_phase = {(t["phase"], t["fixture"]) for t in by_id["test_b"]["touches"]}
    assert by_phase == {("setup", "inner_seed"), ("call", None)}
    # the rollup counts TESTS whose setup touch a fixture caused
    assert report["modules"]["test_inner.py"]["setup_fixtures"] == {"inner_seed": 2}


def test_the_report_rolls_up_per_module_with_denominators_and_outcomes(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

def test_clean():
    pass

def test_clean_but_fails():
    assert False

def test_touches():
    touch_storage_and_swallow()

def test_provisioned(t2_tenant):
    touch_storage_and_swallow()

@pytest.mark.expects_unresolvable_endpoint
def test_exempt():
    touch_storage_and_swallow()

def test_skipped():
    pytest.skip("not run")
''', mode="report", extra={
        "test_all_skipped": '''
import pytest
pytestmark = pytest.mark.skip(reason="wholesale")

def test_x(): pass
def test_y(): pass
''',
        "test_module_skip": '''
import pytest
pytest.skip("whole module", allow_module_level=True)
''',
    })
    result.assert_outcomes(passed=4, failed=1, skipped=4)
    report = _report(result)
    mod = report["modules"]["test_inner.py"]
    assert mod.pop("setup_fixtures") == {}
    assert mod == {
        "tests": 6, "touched": 1, "clean": 2, "provisioned": 1, "exempt": 1,
        "skipped": 1, "failed": 1,
    }
    assert report["modules"]["test_all_skipped.py"]["skipped"] == 2
    assert report["skipped_modules"] == ["test_all_skipped.py"]
    assert report["collect_skipped"] == ["test_module_skip.py"]
    c = report["counts"]
    assert (c["tests"], c["skipped"], c["failed"], c["clean_unprovisioned"]) == (8, 3, 1, 2)


def test_identical_stacks_are_deduped_and_distinct_ones_capped(pytester, monkeypatch) -> None:
    sites = "\n    ".join(f"fns.append(lambda: touch_storage_and_swallow())  # site {i}" for i in range(12))
    result = _run(pytester, monkeypatch, f'''
from helpers import touch_storage_and_swallow

def test_loop():
    for _ in range(100):
        touch_storage_and_swallow()      # one site, 100 events

def test_many_sites():
    fns = []
    {sites}
    for fn in fns:
        fn()
    for i in range(300):                 # past the per-test detail cap
        touch_storage_and_swallow()
''', mode="report")
    result.assert_outcomes(passed=2)
    report = _report(result)
    by_id = {f["nodeid"].split("::")[-1]: f for f in report["findings"]}
    loop = by_id["test_loop"]
    assert loop["count"] == 100 and len(loop["touches"]) == 1 and loop["touches"][0]["count"] == 100
    many = by_id["test_many_sites"]
    assert many["count"] == 312, many["count"]
    # 12 lambda sites + the loop site = 13 signatures; 8 kept, so 4 + 300 events unkept
    assert len(many["touches"]) == 8 and many["dropped_events"] == 304
    assert report["counts"]["touch_events"] == 412


# ── per-test singleton reset ─────────────────────────────────────────────────

_SINGLETON_BODY = '''
from nexus.db import http_vector_client as hvc

SENTINEL = object()

def test_1_leaves_warm_state_behind():
    hvc._vector_client_instance = SENTINEL
    hvc._probed_embedding_mode = "warm"

def test_2_looks():
    try:
        assert (hvc._vector_client_instance is SENTINEL, hvc._probed_embedding_mode) == EXPECTED
    finally:
        hvc.reset_http_vector_client_for_tests()    # the outer process shares this module
'''


def test_optin_resets_the_vector_client_singleton_per_test(pytester, monkeypatch) -> None:
    """A warm ``embedding_mode`` memo would hide a touch, and a cold failed probe
    would poison the next provisioned test."""
    result = _run(pytester, monkeypatch, _SINGLETON_BODY.replace("EXPECTED", "(False, None)"), mode="report")
    result.assert_outcomes(passed=2)


def test_the_default_mode_keeps_the_singleton(pytester, monkeypatch) -> None:
    """Default mode stays behaviourally identical: only the lease cache resets."""
    result = _run(pytester, monkeypatch, _SINGLETON_BODY.replace("EXPECTED", "(True, 'warm')"), optin=False)
    result.assert_outcomes(passed=2)


# ── soft peeks: reviewed, never failing ──────────────────────────────────────


def test_a_soft_peek_from_src_nexus_while_unprovisioned_is_listed_not_failed(pytester, monkeypatch) -> None:
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)  # the outer test may carry its own tenant
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = _run(pytester, monkeypatch, '''
from nexus.db import service_endpoint as se

def test_peeks_from_src():
    se.is_endpoint_env_pinned()                       # get_credential(service_url) from src/nexus
    se.discover_lease_with_wait(budget_s=0.0)         # discover_lease() from src/nexus

def test_peeks_from_the_test_itself():
    from nexus.config import get_credential
    get_credential("service_url")                     # a tests/ frame: not a src/nexus peek
    se.discover_lease()

def test_peek_with_a_tenant(t2_tenant):
    se.is_endpoint_env_pinned()                       # provisioned: not recorded

def test_unrelated_credential():
    from nexus.config import get_credential
    get_credential("voyage_api_key")
''', mode="fail")
    result.assert_outcomes(passed=4)  # a peek never fails a test
    report = _report(result)
    kinds = {(p["kind"], p["site"].split(" in ")[-1]) for p in report["soft_peeks"]}
    assert kinds == {
        ("get_credential:service_url", "is_endpoint_env_pinned"),
        ("discover_lease", "discover_lease_with_wait"),
    }, report["soft_peeks"]
    assert all(p["tests"] == 1 and p["count"] >= 1 for p in report["soft_peeks"])
    assert all("service_endpoint.py" in p["site"] for p in report["soft_peeks"])
    assert report["findings"] == [] and report["counts"]["touch_events"] == 0


def test_peek_wrappers_are_removed_with_the_session(pytester, monkeypatch) -> None:
    """Live inside the nested session, restored (same objects) after it."""
    from nexus import config
    from nexus.db import service_endpoint

    before = (config.get_credential, service_endpoint.discover_lease)
    was_patched = tuple(getattr(f, "_nx_storage_touch_peek", False) for f in before)
    result = _run(pytester, monkeypatch, '''
from nexus import config
from nexus.db import service_endpoint

def test_wrapped_in_here():
    assert getattr(config.get_credential, "_nx_storage_touch_peek", False)
    assert getattr(service_endpoint.discover_lease, "_nx_storage_touch_peek", False)
''', mode="report")
    result.assert_outcomes(passed=1)
    assert (config.get_credential, service_endpoint.discover_lease) == before
    assert tuple(getattr(f, "_nx_storage_touch_peek", False) for f in before) == was_patched


# ── threads ──────────────────────────────────────────────────────────────────


def test_a_construction_on_another_thread_is_not_the_tests_touch(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import threading
from helpers import touch_storage_and_swallow

def test_background_thread():
    t = threading.Thread(target=touch_storage_and_swallow)
    t.start(); t.join()

def test_own_thread():
    touch_storage_and_swallow()
''', mode="report")
    result.assert_outcomes(passed=2)
    report = _report(result)
    assert [f["nodeid"].split("::")[-1] for f in report["findings"]] == ["test_own_thread"]
    assert report["counts"]["foreign_thread_events"] == 1


# ── child processes ──────────────────────────────────────────────────────────

_CHILD = '''
import subprocess, sys

CODE = (
    "from nexus.db.service_endpoint import ServiceEndpointUnresolvableError as E\\n"
    "from nexus.db.t1 import T1ServerNotFoundError as T\\n"
    "E('child'); T('child')\\n"
)

def run_child():
    subprocess.run([sys.executable, "-c", CODE], check=True, timeout=120)
'''


def test_a_child_process_touch_is_attributed_to_the_test_that_spawned_it(pytester, monkeypatch) -> None:
    pytester.makepyfile(child_helpers=_CHILD)
    result = _run(pytester, monkeypatch, '''
from child_helpers import run_child

def test_spawns_without_a_tenant():
    run_child()

def test_spawns_with_a_tenant(t2_tenant):
    run_child()

def test_spawns_nothing():
    pass
''', mode="fail")
    # fail mode names the unprovisioned spawner at its teardown
    result.assert_outcomes(passed=3, errors=1)
    assert "test_spawns_without_a_tenant: reached storage without a tenant" in result.stdout.str()
    report = _report(result)
    (finding,) = report["findings"]
    assert finding["nodeid"].endswith("test_spawns_without_a_tenant")
    assert {t["source"] for t in finding["touches"]} == {"child"}
    assert sorted(finding["error_types"]) == ["ServiceEndpointUnresolvableError", "T1ServerNotFoundError"]
    assert finding["count"] == 2
    c = report["counts"]
    assert (c["touched_unprovisioned"], c["provisioned"], c["touch_events"]) == (1, 1, 4)


def test_the_child_shim_is_on_pythonpath_only_while_an_optin_session_runs(pytester, monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/outer/path")
    monkeypatch.delenv(CHILD_LOG_ENV, raising=False)
    result = _run(pytester, monkeypatch, f'''
import os

def test_inside():
    parts = os.environ["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == {str(SHIM_DIR)!r} and parts[1:] == ["/outer/path"]
    assert os.environ[{CHILD_LOG_ENV!r}]
''', mode="report")
    result.assert_outcomes(passed=1)
    assert os.environ["PYTHONPATH"] == "/outer/path"
    assert CHILD_LOG_ENV not in os.environ
    # outside optin nothing is added
    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    result = _run(pytester, monkeypatch, f'''
import os

def test_inside():
    assert os.environ["PYTHONPATH"] == "/outer/path" and {CHILD_LOG_ENV!r} not in os.environ
''', optin=False)
    result.assert_outcomes(passed=1)


def test_the_shim_and_the_guard_patch_the_same_classes() -> None:
    """The shim is self-contained (it must run with the repo not importable), so
    its class table is a copy; this keeps the two from drifting."""
    import importlib.util

    from tests._storage_touch_guard import GUARDED

    spec = importlib.util.spec_from_file_location("nx_shim_check", SHIM_DIR / "sitecustomize.py")
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    flat = sorted((mod, name) for mod, names in shim._TARGETS.items() for name in names)
    assert flat == sorted(GUARDED)


def test_the_shim_installs_only_when_the_log_variable_is_set(tmp_path: Path) -> None:
    """Both arms, so an unloaded shim cannot pass the 'inert' arm vacuously."""
    import subprocess

    def has_finder(log: str | None) -> str:
        env = {k: v for k, v in os.environ.items() if k != CHILD_LOG_ENV}
        env["PYTHONPATH"] = str(SHIM_DIR)
        if log is not None:
            env[CHILD_LOG_ENV] = log
        return subprocess.run(
            [sys.executable, "-c", "import sys; print(any(type(f).__name__ == '_TouchFinder' for f in sys.meta_path))"],
            env=env, capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()

    assert has_finder(None) == "False"
    assert has_finder(str(tmp_path / "log.jsonl")) == "True"


# ── durability and non-vacuity ───────────────────────────────────────────────


def test_each_test_is_flushed_to_the_jsonl_before_the_next_one_starts(pytester, monkeypatch, tmp_path) -> None:
    report_dir = tmp_path / "report-dir"
    monkeypatch.setenv(REPORT_DIR_ENV, str(report_dir))
    result = _run(pytester, monkeypatch, f'''
from pathlib import Path
import json
from helpers import touch_storage_and_swallow

def _lines():
    out = []
    for p in Path({str(report_dir)!r}).glob("nx-storage-touch-tests-*.jsonl"):
        out += [json.loads(l) for l in p.read_text().splitlines()]
    return out

def test_1():
    touch_storage_and_swallow()

def test_2_sees_test_1_already_on_disk():
    (rec,) = [l for l in _lines() if l["k"] == "test"]
    assert rec["nodeid"].endswith("test_1") and rec["events"] == 1
''', mode="report")
    result.assert_outcomes(passed=2)
    assert Path(re.search(r"storage-touch report: (\S+)", result.stdout.str()).group(1)).parent == report_dir


def test_a_killed_worker_loses_nothing_and_fail_mode_says_so(pytester, monkeypatch, tmp_path) -> None:
    pytest.importorskip("xdist")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]))
    monkeypatch.setenv(REPORT_DIR_ENV, str(tmp_path / "rep"))
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "optin")
    monkeypatch.setenv("NX_STORAGE_TOUCH_GUARD_MODE", "fail")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(helpers=_TOUCH)
    pytester.makepyfile(test_inner='''
import os
from helpers import touch_storage_and_swallow

def test_a_provisioned(t2_tenant):
    touch_storage_and_swallow()

def test_b_clean():
    pass

def test_c_killed():
    os._exit(1)

def test_d_after():
    pass
''')
    result = pytester.runpytest_subprocess(
        "-p", "tests._storage_touch_guard", "-p", "no:cacheprovider", "-n", "1", "-q",
    )
    out = result.stdout.str()
    report = _report(result)
    assert report["workers_started"] == 2 and report["workers"] == 1, report["workers_started"]
    assert len(report["recovered_files"]) == 1
    c = report["counts"]
    # a_provisioned and b_clean survived the kill on disk; d_after ran on the replacement
    assert c["tests"] == 3 and c["provisioned"] == 1, c
    assert result.ret != 0
    assert "vacuous sweep: workers reported 1 of 2 started" in out, out


def test_fail_mode_refuses_a_vacuous_sweep(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, "def test_nothing():\n    pass\n", mode="fail")
    out = result.stdout.str()
    assert result.ret != 0
    assert "vacuous sweep: provisioned=0" in out
    assert "vacuous sweep: touch_events=0" in out
    assert "vacuous sweep: clean_unprovisioned" not in out  # this one examined a clean test


def test_fail_mode_accepts_a_sweep_that_examined_every_class(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def test_provisioned_toucher(t2_tenant):
    touch_storage_and_swallow()

def test_clean():
    pass
''', mode="fail")
    result.assert_outcomes(passed=2)
    assert result.ret == 0
    assert "vacuous" not in result.stdout.str()


def test_report_mode_never_changes_the_exit_status(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, "def test_nothing():\n    pass\n", mode="report")
    assert result.ret == 0

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
from tests.test_storage_touch_guard import (
    REPO_ROOT, _CONFTEST, _TOUCH, _report, _run, assert_exit_failed_for, assert_session_failed_for,
)


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
    # and names where the fixture is defined
    assert report["modules"]["test_inner.py"]["setup_fixture_files"] == {"inner_seed": ["test_inner.py"]}
    assert {t["fixture_file"] for t in by_id["test_a"]["touches"]} == {"test_inner.py"}


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
    assert mod.pop("setup_fixtures") == {} and mod.pop("setup_fixture_files") == {}
    # the nodeids behind the counts, skips with their reason
    assert mod.pop("failed_tests") == ["test_inner.py::test_clean_but_fails"]
    assert mod.pop("skipped_tests") == [{"nodeid": "test_inner.py::test_skipped", "reason": "not run"}]
    assert mod == {
        "tests": 6, "touched": 1, "thread_touched": 0, "peek_only": 0, "clean": 2, "provisioned": 1,
        "exempt": 1, "skipped": 1, "failed": 1, "late_touches": 0,
    }
    assert report["modules"]["test_all_skipped.py"]["skipped"] == 2
    assert report["modules"]["test_all_skipped.py"]["skipped_tests"] == [
        {"nodeid": "test_all_skipped.py::test_x", "reason": "wholesale"},
        {"nodeid": "test_all_skipped.py::test_y", "reason": "wholesale"},
    ]
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


def test_a_soft_peek_is_attributed_to_the_src_consumer_not_the_endpoint_helper(pytester, monkeypatch) -> None:
    """The site is the first src/nexus frame outside service_endpoint.py,
    http_vector_client.py and config.py (the code that WANTED the endpoint), the
    origin is the tests/ frame, and a test that only peeked is its own bucket."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)  # the outer test may carry its own tenant
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = _run(pytester, monkeypatch, '''
from nexus.db import service_endpoint as se
from nexus.mcp_infra import _service_t2_is_env_pinned
from nexus.db.t2._refreshable_client import _resolve_token_only

def test_peeks_from_src():
    _service_t2_is_env_pinned()                       # get_credential(service_url) via the helper
    try:
        _resolve_token_only()                         # get_credential(service_token) + discover_lease
    except Exception:
        pass

def test_peeks_from_the_test_itself():
    from nexus.config import get_credential
    get_credential("service_url")                     # a tests/ frame: not a src/nexus peek
    se.is_endpoint_env_pinned()                       # helper called by the test: no src consumer
    se.discover_lease()

def test_peek_with_a_tenant(t2_tenant):
    _service_t2_is_env_pinned()                       # provisioned: not recorded

def test_unrelated_credential():
    from nexus.config import get_credential
    get_credential("voyage_api_key")
''', mode="fail")
    result.assert_outcomes(passed=4)  # a peek never fails a test
    report = _report(result)
    by_kind = {(p["kind"], p["site"].split(" in ")[-1]): p for p in report["soft_peeks"]}
    assert set(by_kind) == {
        ("get_credential:service_url", "_service_t2_is_env_pinned"),
        ("get_credential:service_token", "_resolve_token_only"),
        ("discover_lease", "_resolve_token_only"),
    }, report["soft_peeks"]
    # the consumer, not the resolver internals, and the tests/ frame that started it
    assert not any("service_endpoint.py" in p["site"] or "config.py" in p["site"] for p in report["soft_peeks"])
    assert any("mcp_infra.py" in p["site"] for p in report["soft_peeks"])
    for p in report["soft_peeks"]:
        assert p["tests"] == 1 and p["count"] >= 1
        assert p["peek_only_tests"] == ["test_inner.py::test_peeks_from_src"]
        assert len(p["origins"]) == 1 and re.fullmatch(r"test_inner\.py:\d+ in test_peeks_from_src", p["origins"][0])
    # peek-only is a bucket of its own: not clean, not touched
    c = report["counts"]
    assert (c["peek_only"], c["clean_unprovisioned"], c["touched_unprovisioned"]) == (1, 2, 0), c
    assert report["modules"]["test_inner.py"]["peek_only"] == 1 and report["modules"]["test_inner.py"]["clean"] == 2
    assert report["findings"] == [] and c["touch_events"] == 0
    assert "peek_only=1" in result.stdout.str()


def test_a_provisioned_test_costs_no_stack_walk_or_formatting_for_a_peek(pytester, monkeypatch) -> None:
    from tests import _storage_touch_guard as g

    calls: list[str] = []
    for name in ("_fmt", "_frames"):
        original = getattr(g, name)
        monkeypatch.setattr(g, name, lambda *a, _o=original, _n=name, **k: calls.append(_n) or _o(*a, **k))
    result = _run(pytester, monkeypatch, '''
from nexus.mcp_infra import _service_t2_is_env_pinned

def test_provisioned_peek(t2_tenant):
    _service_t2_is_env_pinned()
''', mode="report")
    result.assert_outcomes(passed=1)
    assert calls == [], calls


def test_module_level_aliases_of_the_peeked_functions_are_rebound_and_restored(pytester, monkeypatch) -> None:
    """``from x import f as alias`` copies the function: the wrapper must replace
    every such binding for the session and put the original back after it."""
    import types

    from nexus import config
    from nexus.db import service_endpoint

    probe = types.ModuleType("nx_alias_probe")
    probe.cred_alias = config.get_credential  # type: ignore[attr-defined]
    probe.lease_alias = service_endpoint.discover_lease  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nx_alias_probe", probe)
    originals = (probe.cred_alias, probe.lease_alias)  # type: ignore[attr-defined]
    outer_wrapped = tuple(getattr(f, "_nx_storage_touch_peek", False) for f in originals)
    result = _run(pytester, monkeypatch, '''
import sys

def test_aliases_are_wrappers_in_here():
    probe = sys.modules["nx_alias_probe"]
    assert getattr(probe.cred_alias, "_nx_storage_touch_peek", False)
    assert getattr(probe.lease_alias, "_nx_storage_touch_peek", False)
''', mode="report")
    result.assert_outcomes(passed=1)
    assert (probe.cred_alias, probe.lease_alias) == originals  # type: ignore[attr-defined]
    assert tuple(getattr(f, "_nx_storage_touch_peek", False) for f in originals) == outer_wrapped


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


def test_a_construction_on_another_thread_is_a_finding_and_never_a_clean_test(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import threading
from helpers import touch_storage_and_swallow

def test_background_thread():
    t = threading.Thread(target=touch_storage_and_swallow)
    t.start(); t.join()

def test_background_thread_with_a_tenant(t2_tenant):
    t = threading.Thread(target=touch_storage_and_swallow)
    t.start(); t.join()

def test_own_thread():
    touch_storage_and_swallow()

def test_clean():
    pass
''', mode="report")
    result.assert_outcomes(passed=4)
    report = _report(result)
    by_id = {f["nodeid"].split("::")[-1]: f for f in report["findings"]}
    assert {k: f["kind"] for k, f in by_id.items()} == {
        "test_background_thread": "thread_touch", "test_own_thread": "unprovisioned_touch",
    }
    thread = by_id["test_background_thread"]
    assert thread["error_types"] == ["ServiceEndpointUnresolvableError"] and thread["count"] == 1
    (touch,) = thread["touches"]
    assert touch["source"] == "thread" and touch["stack"] and touch["phase"] == "call"
    assert any("touch_storage_and_swallow" in line for line in touch["stack"])
    mod = report["modules"]["test_inner.py"]
    assert (mod["touched"], mod["thread_touched"], mod["clean"], mod["provisioned"]) == (1, 1, 1, 1), mod
    c = report["counts"]
    assert (c["thread_touched"], c["touched_unprovisioned"], c["clean_unprovisioned"]) == (1, 1, 1)
    assert c["foreign_thread_events"] == 2 and c["touch_events"] == 3  # the provisioned one counts as an event
    assert "thread_touched=1" in result.stdout.str()


def test_fail_mode_fails_a_test_whose_only_touch_was_on_a_thread(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import threading
from helpers import touch_storage_and_swallow

def test_background_thread():
    t = threading.Thread(target=touch_storage_and_swallow)
    t.start(); t.join()
''', mode="fail")
    result.assert_outcomes(passed=1, errors=1)
    out = result.stdout.str()
    assert "test_background_thread: reached storage without a tenant" in out
    assert "on a thread the test did not run on" in out


def test_a_thread_a_finished_test_leaked_is_not_charged_to_the_next_test(pytester, monkeypatch) -> None:
    """The thread records the test running when it was created. When it touches
    storage during a later test, the later test stays clean and the touch is
    listed on its own, naming its owner."""
    pytester.makepyfile(leak='''
import threading
from helpers import touch_storage_and_swallow

GO = threading.Event()
DONE = threading.Event()

def _run():
    GO.wait(30)
    touch_storage_and_swallow()
    DONE.set()

THREADS = []

def start():
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    THREADS.append(t)
''')
    result = _run(pytester, monkeypatch, '''
import leak

def test_1_leaks_a_thread():
    leak.start()

def test_2_lets_it_touch():
    leak.GO.set()
    assert leak.DONE.wait(30)
    leak.THREADS[0].join()
''', mode="report")
    result.assert_outcomes(passed=2)
    report = _report(result)
    assert report["findings"] == []
    (late,) = report["late_thread_touches"]
    assert late["nodeid"] == "test_inner.py::test_1_leaks_a_thread"
    assert late["running"] == "test_inner.py::test_2_lets_it_touch"
    assert late["error_type"] == "ServiceEndpointUnresolvableError" and late["stack"]
    assert report["modules"]["test_inner.py"]["late_touches"] == 1
    c = report["counts"]
    assert (c["clean_unprovisioned"], c["thread_touched"], c["touch_events"]) == (2, 0, 0)
    assert "late_thread_touches=1" in result.stdout.str()


def test_late_touches_are_reported_uncapped() -> None:
    from tests._storage_touch_guard import _rollup

    records = [
        {"k": "late_child", "nodeid": f"test_mod.py::test_{i}", "phase": "call", "error_type": "E", "stack": [], "n": 1}
        for i in range(120)
    ] + [{"k": "late_thread", "nodeid": "test_mod.py::test_x", "running": None, "error_type": "E", "stack": [], "n": 3}]
    report = _rollup(records)
    assert len(report["late_child_touches"]) == 120 and len(report["late_thread_touches"]) == 1
    assert report["modules"]["test_mod.py"]["late_touches"] == 123


# ── a recorder never raises into the code under test ─────────────────────────


def test_a_deleted_cwd_does_not_raise_into_the_code_under_test(pytester, monkeypatch) -> None:
    """``Path.cwd()`` raises ``FileNotFoundError`` once the cwd is gone, and it ran
    inside the guarded error's constructor and the credential wrappers."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = _run(pytester, monkeypatch, '''
import os, shutil, tempfile
from helpers import touch_storage_and_swallow
from nexus.mcp_infra import _service_t2_is_env_pinned

def test_cwd_is_gone():
    here = os.getcwd()
    gone = tempfile.mkdtemp()
    os.chdir(gone)
    shutil.rmtree(gone)
    try:
        touch_storage_and_swallow()      # would raise FileNotFoundError out of the constructor
        _service_t2_is_env_pinned()      # and out of the get_credential wrapper
    finally:
        os.chdir(here)
''', mode="report")
    result.assert_outcomes(passed=1)
    report = _report(result)
    (finding,) = report["findings"]
    assert finding["count"] == 1 and report["counts"]["guard_errors"] == 0
    assert [p["kind"] for p in report["soft_peeks"]] == ["get_credential:service_url"]


def test_an_internal_error_is_counted_and_swallowed(pytester, monkeypatch) -> None:
    from tests import _storage_touch_guard as g

    def boom(*args, **kwargs):
        raise RuntimeError("internal guard failure")

    monkeypatch.setattr(g, "_short_stack", boom)
    monkeypatch.setattr(g, "_peek_consumer", boom)
    # the outer test may carry its own tenant (under per-test), which makes the peek read both variables
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow
from nexus.mcp_infra import _service_t2_is_env_pinned

def test_touches_and_peeks():
    touch_storage_and_swallow()          # must not raise, whatever the recorder does
    _service_t2_is_env_pinned()

def test_next():
    pass
''', mode="report")
    result.assert_outcomes(passed=2)
    report = _report(result)
    assert report["counts"]["guard_errors"] == 2
    assert any("guard failure: 2 internal error(s)" in v for v in report["vacuity"]), report["vacuity"]
    assert "guard_errors=2" in result.stdout.str()


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


def _load_shim_inert(monkeypatch):
    """Execute the shim's source to read its tables WITHOUT running its
    ``_install()``. The shim installs itself on import whenever the log variable
    is set, and under the default flip an outer guard session exports it, so a
    bare ``exec_module`` here would wrap the guarded ``__init__`` s a second time
    and add a ``_TouchFinder`` to this process for the rest of the run."""
    import importlib.util

    monkeypatch.delenv(CHILD_LOG_ENV, raising=False)
    spec = importlib.util.spec_from_file_location("nx_shim_check", SHIM_DIR / "sitecustomize.py")
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    return shim


def test_reading_the_shim_installs_nothing_in_this_process(monkeypatch, tmp_path) -> None:
    """With the log variable SET (as an outer guard session sets it), loading the
    shim for inspection leaves ``sys.meta_path`` and every guarded ``__init__`` as
    it found them."""
    from tests._storage_touch_guard import _guarded_classes

    monkeypatch.setenv(CHILD_LOG_ENV, str(tmp_path / "log.jsonl"))
    classes = _guarded_classes()
    inits = [(cls, cls.__dict__.get("__init__")) for cls in classes]
    meta_path = list(sys.meta_path)
    try:
        shim = _load_shim_inert(monkeypatch)
        assert shim._TARGETS, "the shim was loaded"
        assert sys.meta_path == meta_path, "the shim added a finder to this process"
        assert all(cls.__dict__.get("__init__") is init for cls, init in inits), "the shim re-wrapped a guarded __init__"
    finally:  # a regression must not poison the tests that run after this one
        sys.meta_path[:] = meta_path
        for cls, init in inits:
            if init is not None:
                cls.__init__ = init
            elif "__init__" in cls.__dict__:
                del cls.__init__


def test_the_shim_and_the_guard_patch_the_same_classes(monkeypatch) -> None:
    """The shim is self-contained (it must run with the repo not importable), so
    its class table is a copy; this keeps the two from drifting."""
    from tests._storage_touch_guard import GUARDED

    shim = _load_shim_inert(monkeypatch)
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


def test_every_child_is_counted_and_its_soft_peeks_are_recorded(pytester, monkeypatch) -> None:
    pytester.makepyfile(child_helpers=_CHILD)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    result = _run(pytester, monkeypatch, '''
import subprocess, sys

PEEK = (
    "from nexus.mcp_infra import _service_t2_is_env_pinned\\n"
    "_service_t2_is_env_pinned()\\n"
)

def test_three_quiet_children_and_a_peeking_one():
    for _ in range(3):
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=120)
    subprocess.run([sys.executable, "-c", PEEK], check=True, timeout=120)

def test_no_children():
    pass
''', mode="report")
    result.assert_outcomes(passed=2)
    report = _report(result)
    assert report["counts"]["children_seen"] == 4 and report["findings"] == []
    (peek,) = report["soft_peeks"]
    assert peek["kind"] == "get_credential:service_url" and "mcp_infra.py" in peek["site"]
    assert peek["site"].endswith("in _service_t2_is_env_pinned")
    assert peek["peek_only_tests"] == ["test_inner.py::test_three_quiet_children_and_a_peeking_one"]
    assert report["counts"]["peek_only"] == 1 and "children_seen=4" in result.stdout.str()


def test_child_touches_are_deduped_and_capped_like_the_parents(pytester, monkeypatch) -> None:
    sites = "\\n".join("E('site')" for _ in range(12))  # 12 distinct line numbers: 12 signatures
    result = _run(pytester, monkeypatch, f'''
import subprocess, sys

CODE = (
    "from nexus.db.service_endpoint import ServiceEndpointUnresolvableError as E\\n"
    "{sites}\\n"
    "for _ in range(300):\\n"
    "    E('loop')\\n"
)

def test_noisy_child():
    subprocess.run([sys.executable, "-c", CODE], check=True, timeout=120)
''', mode="report")
    result.assert_outcomes(passed=1)
    (finding,) = _report(result)["findings"]
    assert finding["count"] == 312, finding["count"]  # every event counted ...
    assert len(finding["touches"]) == 8  # ... 13 signatures, 8 kept
    assert finding["dropped_events"] + sum(t["count"] for t in finding["touches"]) == 312
    assert finding["dropped_events"] > 0


def test_a_child_that_outlives_its_test_is_reported_late_with_its_owner(pytester, monkeypatch, tmp_path) -> None:
    trigger = tmp_path / "go"
    pytester.makepyfile(late_child=f'''
import subprocess, sys
from pathlib import Path

CODE = (
    "import os, time\\n"
    "while not os.path.exists({str(trigger)!r}):\\n"
    "    time.sleep(0.05)\\n"
    "from nexus.db.service_endpoint import ServiceEndpointUnresolvableError as E\\n"
    "E('late')\\n"
)
PROCS = []

def spawn():
    PROCS.append(subprocess.Popen([sys.executable, "-c", CODE]))

def release_and_wait():
    Path({str(trigger)!r}).touch()
    assert PROCS[0].wait(timeout=120) == 0
''')
    result = _run(pytester, monkeypatch, '''
import late_child

def test_1_spawns_and_returns():
    late_child.spawn()

def test_2_releases_it():
    late_child.release_and_wait()

def test_3_runs_after():
    pass
''', mode="report")
    result.assert_outcomes(passed=3)
    report = _report(result)
    assert report["findings"] == []  # the owner was already written clean
    (late,) = report["late_child_touches"]
    assert late["nodeid"] == "test_inner.py::test_1_spawns_and_returns" and late["error_type"] == "ServiceEndpointUnresolvableError"
    assert report["modules"]["test_inner.py"]["late_touches"] == 1
    assert "late_child_touches=1" in result.stdout.str()


def test_the_shim_chains_a_real_sitecustomize_further_down_the_path(tmp_path: Path) -> None:
    import subprocess

    other = tmp_path / "other"
    other.mkdir()
    marker = tmp_path / "chained"
    (other / "sitecustomize.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n")
    env = {k: v for k, v in os.environ.items() if k != CHILD_LOG_ENV}
    env.update({"PYTHONPATH": os.pathsep.join([str(SHIM_DIR), str(other)]), CHILD_LOG_ENV: str(tmp_path / "log.jsonl")})
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print(any(type(f).__name__ == '_TouchFinder' for f in sys.meta_path))"],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    assert out == "True", "the shim itself is installed"
    assert marker.read_text() == "ran", "and the sitecustomize it shadows still ran"


def test_a_shim_that_fails_to_install_still_chains(tmp_path: Path) -> None:
    """``_install`` and ``_chain`` are separate try blocks: a recorder that
    cannot install must not take the real sitecustomize down with it."""
    import subprocess

    shim = tmp_path / "shim"
    shim.mkdir()
    source = (SHIM_DIR / "sitecustomize.py").read_text()
    needle = "def _install() -> None:\n"
    assert source.count(needle) == 1
    (shim / "sitecustomize.py").write_text(source.replace(needle, needle + "    raise RuntimeError('install failed')\n"))
    other = tmp_path / "other"
    other.mkdir()
    marker = tmp_path / "chained"
    (other / "sitecustomize.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n")
    env = {k: v for k, v in os.environ.items() if k != CHILD_LOG_ENV}
    env.update({"PYTHONPATH": os.pathsep.join([str(shim), str(other)]), CHILD_LOG_ENV: str(tmp_path / "log.jsonl")})
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=60)
    assert marker.read_text() == "ran"


def test_a_process_that_hosts_a_guard_leaves_no_child_lines_even_at_exit(tmp_path: Path) -> None:
    """An xdist worker inherits the shim but keeps its own books. Its repeat
    counts used to resurface in the PARENT's log when the worker's guard
    restored the environment before the process exited."""
    import subprocess

    log = tmp_path / "log.jsonl"
    env = {k: v for k, v in os.environ.items() if k != CHILD_LOG_ENV}
    env.update({"PYTHONPATH": str(SHIM_DIR), CHILD_LOG_ENV: str(log), "PYTEST_CURRENT_TEST": "t.py::t (call)"})
    code = (
        "import os\n"
        "os.environ['NX_STORAGE_TOUCH_GUARD_PID'] = str(os.getpid())\n"
        "from nexus.db.service_endpoint import ServiceEndpointUnresolvableError as E\n"
        "for _ in range(3):\n"
        "    E('x')\n"
        "os.environ['NX_STORAGE_TOUCH_GUARD_PID'] = '1'\n"  # the guard restoring the parent's environment
    )
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=120)
    kinds = [json.loads(line)["k"] for line in log.read_text().splitlines()]
    assert kinds == ["child_start"], kinds  # started as a plain child; recorded nothing once it hosted the guard


# ── durability and non-vacuity ───────────────────────────────────────────────


def _as_a_top_level_session(monkeypatch) -> None:
    """Drop what an OUTER guarded session exported, so the nested session under test
    reads like an operator's own run and honours the report directory it is given
    (a nested one ignores that variable: it cannot tell the operator's from the outer's)."""
    from tests._storage_touch_guard import GUARD_PID_ENV, RUN_ID_ENV

    for name in (RUN_ID_ENV, GUARD_PID_ENV, CHILD_LOG_ENV):
        monkeypatch.delenv(name, raising=False)


def test_each_test_is_flushed_to_the_jsonl_before_the_next_one_starts(pytester, monkeypatch, tmp_path) -> None:
    report_dir = tmp_path / "report-dir"
    _as_a_top_level_session(monkeypatch)
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
    report = _report(result)
    assert report["workers_started"] == 2 and report["workers"] == 1, report["workers_started"]
    assert len(report["recovered_files"]) == 1
    c = report["counts"]
    # a_provisioned and b_clean survived the kill on disk; d_after ran on the replacement
    assert c["tests"] == 3 and c["provisioned"] == 1, c
    # the vacuity verdict itself, not just any failure (the crash fails a test too)
    assert_session_failed_for(result, "vacuous sweep: workers reported 1 of 2 started")
    assert "workers reported 1 of 2 started" in report["vacuity"][0], report["vacuity"]
    assert [v for v in report["vacuity"] if "workers reported" in v] == [
        "vacuous sweep: workers reported 1 of 2 started"
    ]
    assert report["collected"] == 4 and report["recorded"] == 3


def test_fail_mode_accepts_a_whole_xdist_sweep(pytester, monkeypatch, tmp_path) -> None:
    """The happy path across workers: every worker reports, the merged counts are
    the sum (not doubled), and the controller exits zero."""
    pytest.importorskip("xdist")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]))
    monkeypatch.setenv(REPORT_DIR_ENV, str(tmp_path / "rep"))
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "optin")
    monkeypatch.setenv("NX_STORAGE_TOUCH_GUARD_MODE", "fail")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(helpers=_TOUCH)
    pytester.makepyfile(test_inner='''
import pytest
from helpers import touch_storage_and_swallow

@pytest.mark.parametrize("i", range(5))
def test_provisioned(i, t2_tenant):
    touch_storage_and_swallow()

@pytest.mark.parametrize("i", range(5))
def test_clean(i):
    pass
''')
    result = pytester.runpytest_subprocess(
        "-p", "tests._storage_touch_guard", "-p", "no:cacheprovider", "-n", "2", "-q",
    )
    out = result.stdout.str()
    result.assert_outcomes(passed=10)
    assert result.ret == 0, out
    report = _report(result)
    assert report["workers_started"] == report["workers"] == 2 and report["recovered_files"] == []
    c = report["counts"]
    assert (c["tests"], c["provisioned"], c["clean_unprovisioned"], c["touch_events"]) == (10, 5, 5, 5)
    assert (report["collected"], report["recorded"], report["vacuity"]) == (10, 10, [])
    assert "vacuous" not in out


def test_fail_mode_refuses_a_vacuous_sweep(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, "def test_nothing():\n    pass\n", mode="fail")
    out = result.stdout.str()
    assert_session_failed_for(result, "vacuous sweep: provisioned=0", "vacuous sweep: touch_events=0")
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


def test_report_mode_prints_and_stores_the_vacuity_verdict_without_failing(pytester, monkeypatch) -> None:
    """A census that read a vacuous or short run must say so even when it only
    reports, or a report-mode sweep passes over input it never examined."""
    result = _run(pytester, monkeypatch, "def test_nothing():\n    pass\n", mode="report")
    assert result.ret == 0
    out = result.stdout.str()
    assert "vacuous sweep: provisioned=0" in out and "vacuous sweep: touch_events=0" in out
    report = _report(result)
    assert report["vacuity"] == [
        "vacuous sweep: provisioned=0 (must be > 0)",
        "vacuous sweep: touch_events=0 (must be > 0)",
    ]
    assert (report["collected"], report["recorded"]) == (1, 1)
    assert "collected=1 recorded=1" in out
    # and a sweep that examined every class carries an empty verdict
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def test_provisioned_toucher(t2_tenant):
    touch_storage_and_swallow()

def test_clean():
    pass
''', mode="report")
    assert result.ret == 0 and _report(result)["vacuity"] == []


# ── the default flip (nexus-j23ni phase 4) ───────────────────────────────────


def _outer_guard_env(monkeypatch, outer: Path) -> None:
    """What an outer guarded session has exported into its tests' environment."""
    from tests._storage_touch_guard import GUARD_PID_ENV, RUN_ID_ENV

    outer.mkdir(exist_ok=True)
    monkeypatch.setenv(REPORT_DIR_ENV, str(outer))
    monkeypatch.setenv(RUN_ID_ENV, "outerrun0")
    monkeypatch.setenv(GUARD_PID_ENV, str(os.getpid()))
    monkeypatch.setenv(CHILD_LOG_ENV, str(outer / "children.jsonl"))


def test_a_nested_session_never_writes_into_the_outer_sessions_report_dir(pytester, monkeypatch, tmp_path) -> None:
    """The census of 2026-10-18 lost its merged report to a pytester session that
    inherited the operator's report directory and wrote its own report there."""
    outer = tmp_path / "outer-report"
    _outer_guard_env(monkeypatch, outer)
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def test_touches():
    touch_storage_and_swallow()
''', mode="report")
    result.assert_outcomes(passed=1)
    mine = Path(re.search(r"storage-touch report: (\S+)", result.stdout.str()).group(1))
    assert mine.exists() and mine.parent != outer
    assert list(mine.parent.glob("nx-storage-touch-tests-*.jsonl")), "its own books are where its report is"
    assert [p.name for p in outer.iterdir()] == [], "nothing of the nested session reached the outer directory"
    assert os.environ[REPORT_DIR_ENV] == str(outer), "and the outer environment is put back"


def test_a_nested_xdist_session_keeps_its_workers_and_its_report_together(pytester, monkeypatch, tmp_path) -> None:
    pytest.importorskip("xdist")
    outer = tmp_path / "outer-report"
    _outer_guard_env(monkeypatch, outer)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]))
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "optin")
    monkeypatch.setenv("NX_STORAGE_TOUCH_GUARD_MODE", "report")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(helpers=_TOUCH)
    pytester.makepyfile(test_inner='''
import pytest
from helpers import touch_storage_and_swallow

@pytest.mark.parametrize("i", range(4))
def test_touches(i):
    touch_storage_and_swallow()
''')
    result = pytester.runpytest_subprocess(
        "-p", "tests._storage_touch_guard", "-p", "no:cacheprovider", "-n", "2", "-q",
    )
    result.assert_outcomes(passed=4)
    report = _report(result)
    assert report["workers"] == 2 and report["recovered_files"] == []
    assert report["counts"]["tests"] == 4 and report["counts"]["touched_unprovisioned"] == 4
    # a real outer shim may log that this child started (children.jsonl); no report or per-test file lands there
    assert [p.name for p in outer.iterdir() if p.name.startswith("nx-storage-touch-")] == []


def test_a_scoped_run_is_not_vacuous_for_lacking_a_clean_test(pytester, monkeypatch) -> None:
    """The shape of a WHOLE run is asked only when ``NX_STORAGE_TOUCH_SWEEP=1``: a
    narrow run of provisioned tests has no clean one and still exits zero."""
    body = '''
from helpers import touch_storage_and_swallow

def test_provisioned(t2_tenant):
    touch_storage_and_swallow()
'''
    result = _run(pytester, monkeypatch, body, mode="fail", sweep=False)
    result.assert_outcomes(passed=1)
    assert result.ret == 0 and "vacuous sweep" not in result.stdout.str()
    assert _report(result)["vacuity"] == []
    # the same run, asked for the whole-run shape, is refused: the checks still exist
    result = _run(pytester, monkeypatch, body, mode="fail", sweep=True)
    assert_session_failed_for(result, "vacuous sweep: clean_unprovisioned=0")


def test_the_start_up_canary_refuses_a_session_whose_recorder_is_unreachable(pytester, monkeypatch) -> None:
    """A run that finds nothing must be a run that could have found something."""
    from tests import _storage_touch_guard as g

    monkeypatch.setattr(g, "_record_touch", lambda error_type: None)
    result = _run(pytester, monkeypatch, "def test_x():\n    pass\n", mode="report")
    assert result.ret == 4
    assert "storage-touch guard cannot see itself" in result.stderr.str() + result.stdout.str()


@pytest.mark.parametrize("substrate", ["optin", "per-test"])
def test_thread_and_popen_patches_live_only_while_an_optin_session_runs(pytester, monkeypatch, substrate) -> None:
    """``Thread.__init__`` and ``Popen.__init__`` are patched by the guard and by
    nothing else: present for the session, restored after it, and left alone by
    the escape hatch."""
    import subprocess
    import threading

    from tests import _storage_touch_guard as g

    before = (threading.Thread.__init__, subprocess.Popen.__init__)
    # An outer guard session (the default) already holds the patches and the nested
    # one only takes a reference, so "patched while inside" proves nothing there.
    # What the nested session adds is its reference: compare the count, and the
    # functions themselves, before and inside.
    monkeypatch.setenv("EXPECT_REFS_BEFORE", str(g._patch_refs))
    monkeypatch.setenv("EXPECT_PATCHED", "1" if substrate == "optin" else "")
    result = _run(pytester, monkeypatch, '''
import os, subprocess, threading
from tests import _storage_touch_guard as g

def test_patches():
    refs_before = int(os.environ["EXPECT_REFS_BEFORE"])
    if os.environ["EXPECT_PATCHED"]:
        assert g._patch_refs == refs_before + 1, "the opt-in session took no reference on the patches"
        assert getattr(threading.Thread.__init__, "_nx_storage_touch_thread", False)
        assert getattr(subprocess.Popen.__init__, "_nx_storage_touch_popen", False)
    else:
        assert g._patch_refs == refs_before, "the escape hatch took a reference on the patches"
''', mode="report", substrate=substrate)
    result.assert_outcomes(passed=1)
    assert (threading.Thread.__init__, subprocess.Popen.__init__) == before
    assert g._patch_refs == int(os.environ["EXPECT_REFS_BEFORE"]), "the nested session leaked a reference"


def test_a_late_thread_touch_is_a_warning_in_every_mode_and_names_its_owner(pytester, monkeypatch) -> None:
    pytester.makepyfile(leak='''
import threading
from helpers import touch_storage_and_swallow

GO = threading.Event()
DONE = threading.Event()

def _run():
    GO.wait(30)
    touch_storage_and_swallow()
    DONE.set()

THREADS = []

def start():
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    THREADS.append(t)
''')
    body = '''
import leak

def test_1_leaks_a_thread():
    leak.start()

def test_2_lets_it_touch():
    leak.GO.set()
    assert leak.DONE.wait(30)
    leak.THREADS[0].join()
'''
    # whether the touch lands before the session ends is thread timing, so it is
    # a printed warning in BOTH modes and never the reason a session exits non-zero
    for mode in ("fail", "report"):
        result = _run(pytester, monkeypatch, body, mode=mode, sweep=False)
        result.assert_outcomes(passed=2)
        out = result.stdout.str()
        assert result.ret == 0, mode
        assert "late storage touch: 1 construction(s) after their test finished" in out
        assert "test_inner.py::test_1_leaks_a_thread" in out
        assert _report(result)["warnings"] and _report(result)["vacuity"] == []


def test_a_late_child_touch_is_a_warning_not_a_failure(pytester, monkeypatch, tmp_path) -> None:
    trigger = tmp_path / "go"
    pytester.makepyfile(late_child=f'''
import subprocess, sys
from pathlib import Path

CODE = (
    "import os, time\\n"
    "while not os.path.exists({str(trigger)!r}):\\n"
    "    time.sleep(0.05)\\n"
    "from nexus.db.service_endpoint import ServiceEndpointUnresolvableError as E\\n"
    "E('late')\\n"
)
PROCS = []

def spawn():
    PROCS.append(subprocess.Popen([sys.executable, "-c", CODE]))

def release_and_wait():
    Path({str(trigger)!r}).touch()
    assert PROCS[0].wait(timeout=120) == 0
''')
    result = _run(pytester, monkeypatch, '''
import late_child

def test_1_spawns_and_returns():
    late_child.spawn()

def test_2_releases_it():
    late_child.release_and_wait()
''', mode="fail", sweep=False)
    result.assert_outcomes(passed=2)
    assert result.ret == 0
    assert "late storage touch" in result.stdout.str() and "test_1_spawns_and_returns" in result.stdout.str()


def test_python_children_that_never_reach_the_shim_are_a_vacuity_problem(pytester, monkeypatch) -> None:
    """A child that scrubs PYTHONPATH loads no shim. Spawning Python children
    while seeing none is the signature of an unexamined WHOLE run, so it is asked
    only under ``NX_STORAGE_TOUCH_SWEEP=1``: a scoped run, or one whose children
    scrub PYTHONPATH for their own reasons, is not unclean for it."""
    hidden = '''
import os, subprocess, sys

def test_hides_its_child():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=120)
'''
    result = _run(pytester, monkeypatch, hidden, mode="fail", sweep=False)
    result.assert_outcomes(passed=1)
    assert result.ret == 0, "a scoped run is not unclean for hiding a child"
    assert "were spawned but the shim saw none" not in result.stdout.str()
    c = _report(result)["counts"]
    assert (c["children_spawned"], c["children_seen"]) == (1, 0)
    result = _run(pytester, monkeypatch, hidden, mode="fail", sweep=True)
    assert_session_failed_for(result, "1 Python child(ren) were spawned but the shim saw none")
    # control: the same spawn with the environment intact reaches the shim
    result = _run(pytester, monkeypatch, '''
import subprocess, sys

def test_spawns_openly():
    subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=120)
''', mode="fail", sweep=False)
    assert result.ret == 0
    c = _report(result)["counts"]
    assert (c["children_spawned"], c["children_seen"]) == (1, 1)
    # and a child that is not a Python process is not counted as spawned
    result = _run(pytester, monkeypatch, '''
import subprocess

def test_spawns_a_shell():
    subprocess.run(["sh", "-c", "exit 0"], check=True, timeout=120)
''', mode="fail", sweep=False)
    assert result.ret == 0 and _report(result)["counts"]["children_spawned"] == 0


def test_which_commands_count_as_a_python_child(tmp_path: Path) -> None:
    from tests._storage_touch_guard import _is_python_command

    assert _is_python_command([sys.executable, "-c", "pass"])
    assert _is_python_command(["python3", "-m", "pytest"]) and _is_python_command(["/usr/bin/python3.12"])
    assert _is_python_command(["nx", "store", "put"]) and _is_python_command(["pytest", "-q"])
    for native in ("uv", "pip", "pipx"):
        assert not _is_python_command([native, "run"]), f"{native} is a native launcher: it never loads the shim"
    assert not _is_python_command(["git", "status"])
    assert not _is_python_command("python -c pass"), "a shell string is not parsed"
    assert not _is_python_command([]) and not _is_python_command(None)


def test_uninstall_puts_thread_and_popen_back_in_a_fresh_process() -> None:
    """The nested-session case above cannot show the RESTORE when the outer run already
    holds the patches (the nested install only takes a reference). A fresh interpreter has
    no outer: install, see both patches, uninstall, see the very same originals back."""
    import subprocess

    code = (
        "import subprocess, threading\n"
        "from tests import _storage_touch_guard as g\n"
        "t0, p0 = threading.Thread.__init__, subprocess.Popen.__init__\n"
        "assert not getattr(t0, '_nx_storage_touch_thread', False)\n"
        "assert not getattr(p0, '_nx_storage_touch_popen', False)\n"
        "g._install()\n"
        "assert getattr(threading.Thread.__init__, '_nx_storage_touch_thread', False)\n"
        "assert getattr(subprocess.Popen.__init__, '_nx_storage_touch_popen', False)\n"
        "g._uninstall()\n"
        "assert threading.Thread.__init__ is t0 and subprocess.Popen.__init__ is p0\n"
        "print('restored')\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", CHILD_LOG_ENV}}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0 and out.stdout.strip() == "restored", out.stdout + out.stderr


def test_xfail_cannot_swallow_a_storage_touch(pytester, monkeypatch) -> None:
    """pytest applies a test's xfail to its TEARDOWN failure too, so the guard's own
    failure for an xfailed test that reached storage is turned into an expected one.
    The session verdict names it instead, and fail mode ends non-zero."""
    body = '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.mark.xfail(strict=False, reason="known broken for another reason")
def test_xfails_after_a_touch():
    touch_storage_and_swallow()
    assert False

@pytest.mark.xfail(strict=False, reason="known broken for another reason")
def test_xfails_with_a_tenant(t2_tenant):
    touch_storage_and_swallow()
    assert False

@pytest.mark.xfail(strict=False, reason="known broken for another reason")
def test_xfails_untouched():
    assert False
'''
    result = _run(pytester, monkeypatch, body, mode="fail")
    # three xfails, and a fourth report: the guard's teardown failure for the touching one,
    # which the xfail turned into an expected failure as well
    result.assert_outcomes(xfailed=4)
    out = result.stdout.str()
    assert_session_failed_for(
        result, "storage touch swallowed by xfail: 1 xfailed test(s)", "test_inner.py::test_xfails_after_a_touch",
    )
    assert "test_xfails_with_a_tenant" not in out.split("swallowed by xfail")[1].split("storage-touch report")[0]
    assert _report(result)["xfail_touches"] == ["test_inner.py::test_xfails_after_a_touch"]
    # report mode says the same and keeps the exit status
    result = _run(pytester, monkeypatch, body, mode="report")
    assert result.ret == 0 and "storage touch swallowed by xfail" in result.stdout.str()


# ── the tenant saving is ratcheted over a whole run (nexus-zv8i7) ─────────────

_RATCHET_BODY = '''
from helpers import touch_storage_and_swallow

def test_provisioned(t2_tenant):
    touch_storage_and_swallow()

def test_clean():
    pass
'''  # 1 of 2 tests minted a tenant, and the sweep shape is satisfied


def _shard_report(root: Path, shard: str, *, tests: int, provisioned: int, run_id: str | None = None,
                  sweep: bool = True) -> Path:
    """A session report as one CI shard leaves it: ``<root>/<shard>/nx-storage-touch-report.json``."""
    from tests._storage_touch_fan_in import REPORT_NAME

    path = root / shard / REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "counts": {"tests": tests, "provisioned": provisioned}, "run_id": run_id or f"run-{shard}", "sweep": sweep,
    }))
    return path


def _ceiling_file(tmp_path: Path, ceiling: str, *, tolerance: str = "0", suite: str = "python_unit") -> Path:
    """``ceiling`` and ``tolerance`` are TOML literals."""
    path = tmp_path / "ceiling.toml"
    path.write_text(f"[{suite}]\nprovisioned_tests = {ceiling}\ntolerance_tests = {tolerance}\n")
    return path


def _three_shards(root: Path, counts: tuple[int, int, int] = (100, 300, 50)) -> None:
    for i, provisioned in enumerate(counts, 1):
        _shard_report(root, f"shard{i}", tests=1000, provisioned=provisioned)


def test_the_total_over_every_shard_is_compared_with_the_ceiling(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import fan_in

    reports = tmp_path / "reports"
    _three_shards(reports)  # 450 of 3000 minted a tenant
    entry, lines = fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "450"))
    assert lines == [] and entry["status"] == "ok"
    assert (entry["tests"], entry["provisioned"], len(entry["reports"])) == (3000, 450, 3)
    # one over the ceiling fails, naming the total; a tolerance of exactly the excess admits it
    entry, lines = fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "449"))
    assert entry["status"] == "over" and len(lines) == 1
    assert "provisioned_tests=450 (450 of 3000 tests minted a tenant across 3 report(s)) is above the ceiling 449" in lines[0]
    assert fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "449", tolerance="1"))[1] == []
    assert fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "448", tolerance="1"))[0]["status"] == "over"


def test_a_light_shard_cannot_regress_freely_and_a_rebalance_is_not_a_regression(tmp_path: Path) -> None:
    """The per-shard share against the worst shard's ceiling (the first design) failed both ways:
    shard 1 could triple its mints while staying under shard 2's share, and moving a file across a
    chunk boundary moved the shares with no code change."""
    from tests._storage_touch_fan_in import fan_in

    ceiling = _ceiling_file(tmp_path, "450")
    regress = tmp_path / "regress"
    _three_shards(regress, (220, 300, 50))  # shard 1 went from 100 to 220 mints, still below shard 2's share
    entry, lines = fan_in(regress, shards=3, suite="python_unit", path=ceiling)
    assert entry["status"] == "over" and lines, "a light shard that regresses is caught by the total"
    assert max(r["provisioned_per_test"] for r in entry["reports"]) == 0.3  # the worst share would have passed it
    # the same 450 mints split differently across the shards is the same verdict
    for split in ((450, 0, 0), (150, 150, 150), (0, 200, 250)):
        moved = tmp_path / f"moved-{'-'.join(map(str, split))}"
        _three_shards(moved, split)
        assert fan_in(moved, shards=3, suite="python_unit", path=ceiling)[1] == [], split


def test_the_per_report_share_is_advice_and_gates_nothing(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import fan_in

    reports = tmp_path / "reports"
    _shard_report(reports, "a", tests=10, provisioned=9)  # share 0.9
    _shard_report(reports, "b", tests=990, provisioned=1)
    entry, lines = fan_in(reports, shards=2, suite="python_unit", path=_ceiling_file(tmp_path, "10"))
    assert lines == [] and [r["provisioned_per_test"] for r in entry["reports"]] == [0.9, 0.00101]


def test_the_ceiling_follows_the_count_down(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import fan_in

    reports = tmp_path / "reports"
    _three_shards(reports)
    assert fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "450"))[0]["lower_to"] is None
    entry, lines = fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, "600"))
    assert lines == [] and entry["lower_to"] == 450


def test_an_unset_ceiling_fails_the_fan_in_and_prints_the_number_to_commit(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import fan_in

    reports = tmp_path / "reports"
    _three_shards(reports)
    entry, lines = fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, '"measure"'))
    assert entry["status"] == "unset" and len(lines) == 1
    assert "no ceiling set; measured provisioned_tests=450" in lines[0] and "Commit provisioned_tests = 450" in lines[0]


def test_a_fan_in_over_nothing_fails(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import fan_in

    ceiling = _ceiling_file(tmp_path, "450")
    (tmp_path / "empty").mkdir()
    for where in (tmp_path / "empty", tmp_path / "absent"):
        entry, lines = fan_in(where, shards=1, suite="python_unit", path=ceiling)
        assert entry["status"] == "no_reports" and "no nx-storage-touch-report.json under" in lines[0], where
    zero = tmp_path / "zero"
    _shard_report(zero, "a", tests=0, provisioned=0)
    entry, lines = fan_in(zero, shards=1, suite="python_unit", path=ceiling)
    assert entry["status"] == "empty" and lines


def test_a_report_that_does_not_count_the_whole_run_is_refused(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import REPORT_NAME, fan_in

    ceiling = _ceiling_file(tmp_path, "450")

    def verdict(make) -> tuple[str, list[str]]:
        root = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
        _shard_report(root, "good", tests=1000, provisioned=10)
        make(root)
        entry, lines = fan_in(root, shards=2, suite="python_unit", path=ceiling)
        return entry["status"], lines

    # a scoped run undercounts, so it would loosen the total
    status, lines = verdict(lambda root: _shard_report(root, "scoped", tests=10, provisioned=1, sweep=False))
    assert status == "bad_reports" and "not a whole-run report (sweep=False" in lines[0]
    # the same artifact downloaded twice would double the total
    status, lines = verdict(lambda root: _shard_report(root, "again", tests=1000, provisioned=10, run_id="run-good"))
    assert status == "bad_reports" and "repeats run id run-good" in lines[0]
    status, lines = verdict(lambda root: (root / "junk").mkdir() or (root / "junk" / REPORT_NAME).write_text("{not json"))
    assert status == "bad_reports" and "cannot read the report" in lines[0]
    status, lines = verdict(lambda root: (root / "thin").mkdir() or (root / "thin" / REPORT_NAME).write_text('{"counts": {}}'))
    assert status == "bad_reports" and "KeyError" in lines[0]
    status, lines = verdict(lambda root: _shard_report(root, "odd", tests=5, provisioned=6))
    assert status == "bad_reports" and "0 <= provisioned <= tests" in lines[0]


def test_a_ceiling_that_is_not_a_bounded_count_is_refused(tmp_path: Path) -> None:
    from tests._storage_touch_fan_in import MAX_TOLERANCE_TESTS, fan_in

    reports = tmp_path / "reports"
    _three_shards(reports)

    def status(ceiling: str, tolerance: str = "0") -> str:
        return fan_in(reports, shards=3, suite="python_unit", path=_ceiling_file(tmp_path, ceiling, tolerance=tolerance))[0]["status"]

    assert status("450", str(MAX_TOLERANCE_TESTS)) == "ok"
    for bad in ("0", "-1", "0.5", "nan", "inf", "true", '"later"', "450.0"):
        assert status(bad) == "unreadable", bad  # nan or inf as a ceiling would pass every count
    for bad in ("-1", str(MAX_TOLERANCE_TESTS + 1), "0.5", "nan", "inf", "true", '"5"'):
        assert status("450", bad) == "unreadable", bad  # an unbounded tolerance would pass anything
    # an unset ceiling still validates its tolerance
    assert status('"measure"', "-1") == "unreadable"
    absent = tmp_path / "absent.toml"
    assert fan_in(reports, shards=3, suite="python_unit", path=absent)[0]["status"] == "unreadable"
    assert fan_in(reports, shards=3, suite="docs_readers", path=_ceiling_file(tmp_path, "450"))[0]["status"] == "unreadable"
    (tmp_path / "broken.toml").write_text("[python_unit\n")
    assert fan_in(reports, shards=3, suite="python_unit", path=tmp_path / "broken.toml")[0]["status"] == "unreadable"
    (tmp_path / "notol.toml").write_text("[python_unit]\nprovisioned_tests = 450\n")
    assert fan_in(reports, shards=3, suite="python_unit", path=tmp_path / "notol.toml")[0]["status"] == "unreadable"


def test_a_missing_or_extra_shard_fails_before_any_count_is_read(tmp_path: Path) -> None:
    """A shard whose artifact never arrived under-counts and would pass; a stray second copy over-counts."""
    from tests._storage_touch_fan_in import fan_in

    ceiling = _ceiling_file(tmp_path, "450")
    reports = tmp_path / "reports"
    _three_shards(reports)
    assert fan_in(reports, shards=3, suite="python_unit", path=ceiling)[1] == []
    for expected in (4, 6, 2, 1):
        entry, lines = fan_in(reports, shards=expected, suite="python_unit", path=ceiling)
        assert entry["status"] == "shard_count" and f"expected {expected} report(s)" in lines[0] and "found 3" in lines[0], expected
        assert entry["provisioned"] == 0, "no count is read from a run with the wrong number of reports"
    # the missing shard is the heavy one: its mints are what the total would have caught
    (reports / "shard2" / "nx-storage-touch-report.json").unlink()
    entry, lines = fan_in(reports, shards=3, suite="python_unit", path=ceiling)
    assert entry["status"] == "shard_count" and "found 2" in lines[0]
    for bad in (0, -1, True, 2.0):
        assert fan_in(reports, shards=bad, suite="python_unit", path=ceiling)[0]["status"] == "bad_shards", bad


def test_the_fan_in_command_exits_nonzero_over_the_ceiling(tmp_path: Path) -> None:
    """Run as CI runs it: a child interpreter with pytest unavailable (the gate runner has none)."""
    import subprocess
    import sys

    reports = tmp_path / "reports"
    _three_shards(reports)
    launcher = (
        "import runpy, sys; sys.modules['pytest'] = None; sys.path.insert(0, sys.argv.pop(1)); "
        "runpy.run_module('tests._storage_touch_fan_in', run_name='__main__')"
    )

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-c", launcher, str(REPO_ROOT), *args],
            cwd=tmp_path, capture_output=True, text=True, timeout=120,
        )

    ok = ("--suite", "python_unit", "--shards", "3")
    within = run(str(reports), *ok, "--ceiling-file", str(_ceiling_file(tmp_path, "450")))
    assert within.returncode == 0, within.stdout + within.stderr
    assert "shards=3 reports=3 tests=3000 provisioned=450 ceiling=450" in within.stdout and "(advisory)" in within.stdout
    over = run(str(reports), *ok, "--ceiling-file", str(_ceiling_file(tmp_path, "449")))
    assert_exit_failed_for(over.returncode, over.stdout + over.stderr, "above the ceiling 449")
    nothing = run(str(tmp_path / "absent"), *ok, "--ceiling-file", str(_ceiling_file(tmp_path, "450")))
    assert_exit_failed_for(nothing.returncode, nothing.stdout, "no nx-storage-touch-report.json under")
    short = run(str(reports), "--suite", "python_unit", "--shards", "6", "--ceiling-file", str(_ceiling_file(tmp_path, "450")))
    assert_exit_failed_for(short.returncode, short.stdout, "expected 6 report(s)", "found 3")
    no_suite = run(str(reports), "--shards", "3")  # no default section to pick wrongly
    assert_exit_failed_for(no_suite.returncode, no_suite.stderr, "--suite")
    no_shards = run(str(reports), "--suite", "python_unit")  # no default count to pass a short run
    assert_exit_failed_for(no_shards.returncode, no_shards.stderr, "--shards")


def test_the_fan_in_module_needs_nothing_outside_the_standard_library() -> None:
    """The gate runner has no pytest, and importing ``tests._storage_touch_guard`` for the fan-in
    would pull it in. The module's own imports are checked statically and then by importing it with
    pytest made unavailable; the guard shares its two constants rather than defining them twice."""
    import ast
    import subprocess
    import sys

    path = REPO_ROOT / "tests" / "_storage_touch_fan_in.py"
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import: the module runs as __main__"
            imported.add((node.module or "").split(".")[0])
    assert imported, "the scan saw no import"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported - set(sys.stdlib_module_names))
    probe = (
        "import sys; sys.modules['pytest'] = None; sys.path.insert(0, sys.argv[1]); "
        "import tests._storage_touch_fan_in as m; assert callable(m.fan_in) and callable(m.main); "
        "assert 'pytest' not in [k for k, v in sys.modules.items() if v is not None]"
    )
    done = subprocess.run([sys.executable, "-I", "-c", probe, str(REPO_ROOT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    from tests import _storage_touch_fan_in as fan, _storage_touch_guard as guard

    assert (guard.REPORT_NAME, guard.SWEEP_ENV) == (fan.REPORT_NAME, fan.SWEEP_ENV)


def test_a_session_reports_its_share_and_never_gates_on_it(pytester, monkeypatch) -> None:
    """One session sees its file neighbourhood of the suite, so its share is printed and no
    ceiling, set or unset, can fail it. The report carries what the fan-in needs."""
    result = _run(pytester, monkeypatch, _RATCHET_BODY, mode="fail")
    result.assert_outcomes(passed=2)
    assert result.ret == 0, result.stdout.str()
    out = result.stdout.str()
    assert "provisioned_per_test=0.500000" in out and "ratchet" not in out and "no ceiling" not in out
    report = _report(result)
    assert report["vacuity"] == [] and "provisioned_ratchet" not in report
    assert report["sweep"] is True and isinstance(report["run_id"], str) and report["run_id"]


def test_the_fan_in_reads_the_reports_real_sessions_write(pytester, monkeypatch, tmp_path: Path) -> None:
    """End to end: a sweep session's report is a valid fan-in input, a scoped one is refused."""
    from tests._storage_touch_fan_in import REPORT_NAME, fan_in

    ceiling = _ceiling_file(tmp_path, "1")
    result = _run(pytester, monkeypatch, _RATCHET_BODY, mode="fail")
    path = Path(re.search(r"storage-touch report: (\S+)", result.stdout.str()).group(1))
    assert path.name == REPORT_NAME
    entry, lines = fan_in(path.parent, shards=1, suite="python_unit", path=ceiling)
    assert lines == [] and (entry["tests"], entry["provisioned"]) == (2, 1)
    # one more provisioned test than the ceiling allows
    result = _run(pytester, monkeypatch, _RATCHET_BODY + "\ndef test_provisioned_too(t2_tenant):\n    pass\n", mode="fail")
    path = Path(re.search(r"storage-touch report: (\S+)", result.stdout.str()).group(1))
    assert fan_in(path.parent, shards=1, suite="python_unit", path=ceiling)[0]["status"] == "over"
    # a scoped (non-sweep) run is not a whole run
    result = _run(pytester, monkeypatch, _RATCHET_BODY, mode="fail", sweep=False)
    path = Path(re.search(r"storage-touch report: (\S+)", result.stdout.str()).group(1))
    entry, lines = fan_in(path.parent, shards=1, suite="python_unit", path=ceiling)
    assert entry["status"] == "bad_reports" and "sweep=False" in lines[0]


def test_the_committed_ceiling_is_well_formed_and_still_unset() -> None:
    import tomllib

    from tests._storage_touch_fan_in import CEILING_UNSET, DEFAULT_CEILING_FILE, _ceiling_values

    assert DEFAULT_CEILING_FILE == REPO_ROOT / "tests" / "storage_touch_ceiling.toml"
    sections = tomllib.loads(DEFAULT_CEILING_FILE.read_text())
    assert set(sections) == {"python_unit", "docs_readers"}
    for name in sections:
        ceiling, _tolerance = _ceiling_values(DEFAULT_CEILING_FILE, name)  # raises on a malformed value
        # nobody has run the full sweep to completion since the fan-in replaced the per-shard
        # share: the first number is a reviewed commit, so an int here arrived with its evidence
        assert ceiling == CEILING_UNSET or (isinstance(ceiling, int) and ceiling >= 1), name


# ── nexus-0ite2: the whole-run checks fire on size, warnings gate a sweep, pool threads ──


_WHOLE_RUN_SHAPE_BODY = '''
def test_a():
    pass

def test_b():
    pass

def test_c():
    pass
'''


def test_a_run_at_or_above_the_collected_floor_asks_the_whole_run_checks_but_only_warns(pytester, monkeypatch) -> None:
    """No ``NX_STORAGE_TOUCH_SWEEP``: the count of collected tests decides. Three
    clean tests under a floor of three hold no provisioned one and no touch event, so
    the shape checks have something to say, and an automatic run says it as a yellow
    warning and a ``warnings`` entry: it never changes the exit status and never lands in
    ``vacuity`` (only an explicit ``NX_STORAGE_TOUCH_SWEEP=1`` gates on them). Two tests
    do not reach the floor and hold no warning."""
    result = _run(pytester, monkeypatch, _WHOLE_RUN_SHAPE_BODY, mode="fail", sweep=False, floor=3, args=("--color=yes",))
    assert result.ret == 0
    report = _report(result)
    assert report["sweep"] is True and report["sweep_auto"] is True
    assert report["vacuity"] == []
    for name in ("provisioned", "touch_events"):
        assert f"vacuous sweep: {name}=0" in "".join(report["warnings"])
        line = next(ln for ln in result.stdout.str().splitlines() if f"vacuous sweep: {name}=0" in ln)
        assert "\x1b[33m" in line, line  # yellow, not red
    below = _run(pytester, monkeypatch, _WHOLE_RUN_SHAPE_BODY.rsplit("def test_c", 1)[0], mode="fail", sweep=False, floor=3)
    assert below.ret == 0 and "vacuous sweep" not in below.stdout.str()
    assert (_report(below)["sweep"], _report(below)["sweep_auto"]) == (False, False)
    assert _report(below)["warnings"] == []


def test_the_explicit_switch_still_asks_the_whole_run_checks_of_a_small_run(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, _WHOLE_RUN_SHAPE_BODY, mode="fail", sweep=True, floor=100000)
    assert_session_failed_for(result, "vacuous sweep: provisioned=0")
    report = _report(result)
    assert report["sweep"] is True and report["sweep_auto"] is False
    assert any("vacuous sweep: provisioned=0" in p for p in report["vacuity"])


def test_a_run_pytest_split_is_splitting_is_a_whole_run_report_whatever_its_size(pytester, monkeypatch) -> None:
    """A CI shard is one ``--splits N --group K`` slice of the suite, and a slice can
    collect fewer tests than any size floor (shard 2 of the committed split collects
    3040), so the split option decides, not the count. The default floor stays: the same
    three tests without a split are not a whole run."""
    split = _run(pytester, monkeypatch, _WHOLE_RUN_SHAPE_BODY, mode="fail", sweep=False, args=("--splits", "2", "--group", "1"))
    assert split.ret == 0
    report = _report(split)
    assert report["sweep"] is True and report["sweep_auto"] is True and report["collected"] < 4000
    whole = _run(pytester, monkeypatch, _WHOLE_RUN_SHAPE_BODY, mode="fail", sweep=False)
    assert (_report(whole)["sweep"], _report(whole)["sweep_auto"]) == (False, False)


def test_the_unsplit_floor_stays_above_the_docs_readers_leg() -> None:
    """The docs-readers leg collects 1561 tests, all lint-marked, with nothing
    unprovisioned among them; an unsplit run that large must not ask the whole-run checks."""
    from tests._storage_touch_guard import SWEEP_AUTO_FLOOR

    assert 1561 < SWEEP_AUTO_FLOOR <= 5000


def test_a_malformed_floor_is_a_usage_error(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, "def test_x():\n    pass\n", mode="fail", sweep=False, floor="lots")
    assert_session_failed_for(result, "NX_STORAGE_TOUCH_SWEEP_FLOOR")


def test_children_that_never_reach_the_shim_print_a_yellow_warning_in_a_scoped_run(pytester, monkeypatch) -> None:
    hidden = '''
import os, subprocess, sys

def test_hides_its_child():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=120)
'''
    result = _run(pytester, monkeypatch, hidden, mode="fail", sweep=False, args=("--color=yes",))
    assert result.ret == 0
    out = result.stdout.str()
    line = next(ln for ln in out.splitlines() if "Python child" in ln and "shim" in ln)
    assert "\x1b[33m" in line, line  # yellow
    assert "1 Python child(ren)" in line
    assert any("Python child" in w for w in _report(result)["warnings"])
    # control: a child that reaches the shim prints nothing, and a run with no children neither
    ok = '''
import subprocess, sys

def test_spawns_openly():
    subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=120)
'''
    for body in (ok, "def test_nothing():\n    pass\n"):
        result = _run(pytester, monkeypatch, body, mode="fail", sweep=False)
        assert result.ret == 0 and _report(result)["warnings"] == []


_LATE_THREAD_WITH_A_CLEAN_SHAPE = '''
import leak

def test_provisioned_toucher(t2_tenant):
    from helpers import touch_storage_and_swallow
    touch_storage_and_swallow()

def test_1_leaks_a_thread():
    leak.start()

def test_2_lets_it_touch():
    leak.GO.set()
    assert leak.DONE.wait(30)
    leak.THREADS[0].join()
'''

_LEAK = '''
import threading
from helpers import touch_storage_and_swallow

GO = threading.Event()
DONE = threading.Event()

def _run():
    GO.wait(30)
    touch_storage_and_swallow()
    DONE.set()

THREADS = []

def start():
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    THREADS.append(t)
'''


def test_a_late_touch_fails_a_sweep_and_stays_a_warning_otherwise(pytester, monkeypatch) -> None:
    """Whether a late touch lands is thread timing, so a scoped run only prints it. A
    run that explicitly asked the whole-run checks gates on it: the sweep is the run that
    is meant to leave nothing unexamined. A run that turned the checks on by itself (a
    split shard, a large run) prints it, naming the test, and passes. The body is otherwise a clean
    sweep (a provisioned test, a clean one, a touch event), so the late touch is the reason."""
    pytester.makepyfile(leak=_LEAK)
    scoped = _run(pytester, monkeypatch, _LATE_THREAD_WITH_A_CLEAN_SHAPE, mode="fail", sweep=False)
    scoped.assert_outcomes(passed=3)
    assert scoped.ret == 0 and _report(scoped)["vacuity"] == [] and _report(scoped)["warnings"]
    swept = _run(pytester, monkeypatch, _LATE_THREAD_WITH_A_CLEAN_SHAPE, mode="fail", sweep=True)
    assert_session_failed_for(swept, "late storage touch: 1 construction(s) after their test finished")
    report = _report(swept)
    assert any("late storage touch" in p for p in report["vacuity"])
    assert "vacuous sweep" not in "".join(report["vacuity"])
    # the same body by size, with no switch: the shape checks run, the late touch only prints
    by_size = _run(pytester, monkeypatch, _LATE_THREAD_WITH_A_CLEAN_SHAPE, mode="fail", sweep=False, floor=3)
    by_size.assert_outcomes(passed=3)
    size_report = _report(by_size)
    assert by_size.ret == 0 and size_report["sweep_auto"] is True
    assert size_report["vacuity"] == [] and any("late storage touch" in w for w in size_report["warnings"])
    # and the terminal summary names the leaking test, not only the counts line
    printed = [ln for ln in by_size.stdout.str().splitlines() if "late storage touch" in ln]
    assert printed and "test_inner.py::test_1_leaks_a_thread" in printed[0], by_size.stdout.str()
    # report mode prints it and never changes the exit status
    reported = _run(pytester, monkeypatch, _LATE_THREAD_WITH_A_CLEAN_SHAPE, mode="report", sweep=True)
    assert reported.ret == 0 and "late storage touch" in reported.stdout.str()


_POOL_WORKER_BODY = '''
from concurrent.futures import ThreadPoolExecutor
from helpers import touch_storage_and_swallow

POOL = ThreadPoolExecutor(max_workers=1)

def test_1_creates_the_pool_worker():
    POOL.submit(lambda: None).result(timeout=30)   # the worker thread is created HERE

def test_2_uses_it_without_a_tenant():
    POOL.submit(touch_storage_and_swallow).result(timeout=30)

def test_3_uses_it_with_a_tenant(t2_tenant):
    POOL.submit(touch_storage_and_swallow).result(timeout=30)
'''


def test_a_pool_worker_created_by_one_test_charges_a_later_touch_to_the_test_that_ran_it(pytester, monkeypatch) -> None:
    """A ``ThreadPoolExecutor`` worker lives across tests: the test that created it
    is not the test that gave it work. The touch belongs to the running test, as a
    ``thread_touch`` finding (the second test), not to the first as a late touch."""
    result = _run(pytester, monkeypatch, _POOL_WORKER_BODY, mode="report")
    result.assert_outcomes(passed=3)
    report = _report(result)
    assert report["late_thread_touches"] == []
    (finding,) = report["findings"]
    assert finding["kind"] == "thread_touch" and finding["nodeid"].endswith("::test_2_uses_it_without_a_tenant")
    c = report["counts"]
    assert (c["thread_touched"], c["provisioned"]) == (1, 1)


def test_a_thread_that_is_not_a_pool_worker_is_still_charged_to_its_owner(pytester, monkeypatch) -> None:
    """The pool rule is narrow: a bare ``Thread`` a finished test leaked stays a late touch."""
    pytester.makepyfile(leak=_LEAK)
    result = _run(pytester, monkeypatch, '''
import leak

def test_1_leaks_a_thread():
    leak.start()

def test_2_lets_it_touch():
    leak.GO.set()
    assert leak.DONE.wait(30)
    leak.THREADS[0].join()
''', mode="report", sweep=False)
    report = _report(result)
    assert report["findings"] == [] and len(report["late_thread_touches"]) == 1


def test_which_threads_are_pool_workers() -> None:
    import multiprocessing.pool
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tests._storage_touch_guard import _is_pool_worker

    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(lambda: None).result(timeout=30)
        (worker,) = ex._threads
        assert _is_pool_worker(worker)
    tp = multiprocessing.pool.ThreadPool(1)
    try:
        (worker,) = tp._pool
        assert _is_pool_worker(worker)
    finally:
        tp.terminate()
        tp.join()
    assert not _is_pool_worker(threading.Thread(target=lambda: None))
    assert not _is_pool_worker(threading.current_thread())


_POOL_LEAK = '''
import threading
from concurrent.futures import ThreadPoolExecutor
from helpers import touch_storage_and_swallow

GO = threading.Event()
DONE = threading.Event()
POOL = ThreadPoolExecutor(max_workers=1)

def _task():
    GO.wait(30)
    touch_storage_and_swallow()
    DONE.set()

def submit_blocked():
    return POOL.submit(_task)
'''

# runs after the named test's teardown has been recorded, so no test is running when the pool touches
_RELEASE_AFTER_TEST_1 = '''
import leak

def pytest_runtest_logfinish(nodeid, location):
    if nodeid.endswith("::test_1"):
        leak.GO.set()
        assert leak.DONE.wait(30)
'''


@pytest.mark.parametrize("created_in_a_test,owner", [
    (True, "test_inner.py::test_1"),
    (False, "<no test running>"),
], ids=["creator-known", "creator-unknown"])
def test_a_pool_worker_touching_between_tests_is_a_late_touch_not_a_dropped_one(
    pytester, monkeypatch, created_in_a_test: bool, owner: str,
) -> None:
    """With no test running there is no test to charge a pool worker's touch to. It
    was dropped; it is a late touch now, owned by the test that created the worker, or
    by ``<no test running>`` when the worker was created outside any test."""
    pytester.makepyfile(leak=_POOL_LEAK, gate_plugin=_RELEASE_AFTER_TEST_1)
    pytester.syspathinsert()
    if created_in_a_test:
        body = "import leak\n\ndef test_1():\n    leak.submit_blocked()\n\ndef test_2():\n    pass\n"
    else:
        body = "import leak\nleak.submit_blocked()\n\ndef test_1():\n    pass\n\ndef test_2():\n    pass\n"
    result = _run(pytester, monkeypatch, body, mode="report", sweep=False, args=("-p", "gate_plugin"))
    result.assert_outcomes(passed=2)
    report = _report(result)
    assert report["findings"] == []
    (late,) = report["late_thread_touches"]
    assert late["nodeid"].endswith(owner) and late["running"] is None
    assert "late storage touch" in result.stdout.str()

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
    out = result.stdout.str()
    report = _report(result)
    assert report["workers_started"] == 2 and report["workers"] == 1, report["workers_started"]
    assert len(report["recovered_files"]) == 1
    c = report["counts"]
    # a_provisioned and b_clean survived the kill on disk; d_after ran on the replacement
    assert c["tests"] == 3 and c["provisioned"] == 1, c
    assert result.ret != 0
    # the vacuity verdict itself, not just any failure (the crash fails a test too)
    assert "vacuous sweep: workers reported 1 of 2 started" in out, out
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
    assert result.ret != 0 and "vacuous sweep: clean_unprovisioned=0" in result.stdout.str()


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

    before = (threading.Thread.__init__, subprocess.Popen.__init__)
    outer_patched = (
        getattr(before[0], "_nx_storage_touch_thread", False), getattr(before[1], "_nx_storage_touch_popen", False)
    )
    monkeypatch.setenv("EXPECT_PATCHED", "1" if substrate == "optin" else "")
    monkeypatch.setenv("EXPECT_OUTER", json.dumps(outer_patched))
    result = _run(pytester, monkeypatch, '''
import json, os, subprocess, threading

def test_patches():
    now = (
        getattr(threading.Thread.__init__, "_nx_storage_touch_thread", False),
        getattr(subprocess.Popen.__init__, "_nx_storage_touch_popen", False),
    )
    if os.environ["EXPECT_PATCHED"]:
        assert now == (True, True)
    else:
        assert now == tuple(json.loads(os.environ["EXPECT_OUTER"])), "the escape hatch added nothing"
''', mode="report", substrate=substrate)
    result.assert_outcomes(passed=1)
    assert (threading.Thread.__init__, subprocess.Popen.__init__) == before


def test_a_late_thread_touch_fails_a_fail_mode_session_and_names_its_owner(pytester, monkeypatch) -> None:
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
    result = _run(pytester, monkeypatch, body, mode="fail")
    result.assert_outcomes(passed=2)  # both tests pass: the finding is the session's
    out = result.stdout.str()
    assert result.ret != 0
    assert "late storage touch: 1 construction(s) after their test finished" in out
    assert "test_inner.py::test_1_leaks_a_thread" in out
    # report mode prints the same line and keeps the exit status
    result = _run(pytester, monkeypatch, body, mode="report")
    assert result.ret == 0 and "late storage touch: 1 construction(s)" in result.stdout.str()


def test_a_late_child_touch_fails_a_fail_mode_session(pytester, monkeypatch, tmp_path) -> None:
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
''', mode="fail")
    result.assert_outcomes(passed=2)
    assert result.ret != 0
    assert "late storage touch" in result.stdout.str() and "test_1_spawns_and_returns" in result.stdout.str()


def test_python_children_that_never_reach_the_shim_are_a_vacuity_problem(pytester, monkeypatch) -> None:
    """A child that scrubs PYTHONPATH loads no shim. Spawning Python children
    while seeing none is the signature of an unexamined run."""
    hidden = '''
import os, subprocess, sys

def test_hides_its_child():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=120)
'''
    result = _run(pytester, monkeypatch, hidden, mode="fail", sweep=False)
    result.assert_outcomes(passed=1)
    assert result.ret != 0
    assert "1 Python child(ren) were spawned but the shim saw none" in result.stdout.str()
    c = _report(result)["counts"]
    assert (c["children_spawned"], c["children_seen"]) == (1, 0)
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
    assert _is_python_command(["nx", "store", "put"]) and _is_python_command([Path("/opt/bin/uv"), "run"])
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

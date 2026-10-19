# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni: the storage-touch guard behind the opt-in test tenant
(``tests/_storage_touch_guard.py``).

Each case runs the real plugin in a nested ``pytester`` session with a tiny
stand-in ``t2_tenant`` fixture (the real one boots an engine; the guard only
needs to know when a tenant is active). Tests inside a nested session run in a
fixed order, so the cases do not depend on how the outer run is split.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests._storage_touch_guard import REPORT_NAME

REPO_ROOT = Path(__file__).resolve().parent.parent

_CONFTEST = '''
import pytest
from tests import _storage_touch_guard as guard


@pytest.fixture
def t2_tenant():
    with guard.tenant_active():
        yield "tenant"
'''

_TOUCH = '''
def touch_storage_and_swallow():
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError
    try:
        raise ServiceEndpointUnresolvableError("nothing resolvable")
    except ServiceEndpointUnresolvableError:
        pass  # the code under test carries on, so the test's outcome shows nothing


def touch_t1_and_swallow():
    from nexus.db.t1 import T1ServerNotFoundError
    try:
        raise T1ServerNotFoundError("no T1")
    except T1ServerNotFoundError:
        pass
'''


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str,
         *, mode: str = "fail", optin: bool = True, args: tuple[str, ...] = (),
         extra: dict[str, str] | None = None, substrate: str | None = "",
         sweep: bool = True):
    """Run ``body`` in a nested session. ``substrate`` names ``NX_TEST_T2_SUBSTRATE``
    (None leaves it unset: the default); by default ``optin`` picks ``optin`` and
    not ``optin`` picks ``per-test``, the escape hatch the guard is inert under.
    ``sweep`` sets ``NX_STORAGE_TOUCH_SWEEP``, which asks the whole-run shape checks."""
    if substrate == "":
        substrate = "optin" if optin else "per-test"
    if substrate is None:
        monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    else:
        monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", substrate)
    if sweep:
        monkeypatch.setenv("NX_STORAGE_TOUCH_SWEEP", "1")
    else:
        monkeypatch.delenv("NX_STORAGE_TOUCH_SWEEP", raising=False)
    if mode:
        monkeypatch.setenv("NX_STORAGE_TOUCH_GUARD_MODE", mode)
    else:
        monkeypatch.delenv("NX_STORAGE_TOUCH_GUARD_MODE", raising=False)
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(helpers=_TOUCH)
    pytester.makepyfile(test_inner=body)
    if extra:
        pytester.makepyfile(**extra)
    return pytester.runpytest_inprocess(
        "-p", "tests._storage_touch_guard", "-p", "no:cacheprovider", "-q", *args,
    )


def _report(result: pytest.RunResult) -> dict:
    m = re.search(r"storage-touch report: (\S+)", result.stdout.str())
    assert m, result.stdout.str()
    path = Path(m.group(1))
    assert path.name == REPORT_NAME
    return json.loads(path.read_text())


def test_a_swallowed_raise_is_named_at_teardown(pytester, monkeypatch) -> None:
    """The positive control: the test PASSES its own call phase, the code under
    test caught the error, and the guard still names the test."""
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow, touch_t1_and_swallow

def test_swallows_resolver_error():
    touch_storage_and_swallow()

def test_swallows_t1_error():
    touch_t1_and_swallow()

def test_touches_nothing():
    assert 1 + 1 == 2
''')
    result.assert_outcomes(passed=3, errors=2)
    out = result.stdout.str()
    assert "test_inner.py::test_swallows_resolver_error: reached storage without a tenant" in out
    assert "test_inner.py::test_swallows_t1_error: reached storage without a tenant" in out
    assert "ServiceEndpointUnresolvableError" in out and "T1ServerNotFoundError" in out
    assert "test_touches_nothing: reached" not in out
    c = _report(result)["counts"]
    assert (c["tests"], c["touched_unprovisioned"], c["clean_unprovisioned"]) == (3, 2, 1)


def test_a_provisioned_test_passes(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

def test_with_the_fixture(t2_tenant):
    touch_storage_and_swallow()

def test_with_the_fixture_untouched(t2_tenant):
    assert t2_tenant == "tenant"
''')
    result.assert_outcomes(passed=2)
    c = _report(result)["counts"]
    assert (c["provisioned"], c["touched_unprovisioned"], c["touch_events"]) == (2, 0, 1)


def test_the_exemption_marker_passes_and_a_stale_one_is_reported(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.mark.expects_unresolvable_endpoint
def test_exempt_and_raises():
    touch_storage_and_swallow()

@pytest.mark.expects_unresolvable_endpoint
def test_exempt_but_never_raises():
    assert True

@pytest.mark.expects_unresolvable_endpoint
def test_exempt_but_skipped():
    pytest.skip("not run, so no verdict on the exemption")
''')
    result.assert_outcomes(passed=2, skipped=1, errors=1)
    out = result.stdout.str()
    assert "test_exempt_but_never_raises: marked expects_unresolvable_endpoint" in out
    assert "the exemption is stale" in out
    assert "test_exempt_and_raises" not in out
    c = _report(result)["counts"]
    assert (c["exempt"], c["stale_exemptions"]) == (3, 1)


def test_a_setup_phase_touch_from_a_fixture_is_named(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.fixture
def seeded():
    touch_storage_and_swallow()
    return 1

def test_uses_the_fixture(seeded):
    assert seeded == 1

def test_tenant_set_up_after_the_touching_fixture(seeded, t2_tenant):
    assert seeded == 1
''', mode="report")
    result.assert_outcomes(passed=2)
    findings = _report(result)["findings"]
    by_id = {f["nodeid"].split("::")[-1]: f for f in findings}
    assert by_id["test_uses_the_fixture"]["phases"] == ["setup"]
    # the fixture ran before the tenant existed, so even this test is named
    assert by_id["test_tenant_set_up_after_the_touching_fixture"]["phases"] == ["setup"]
    assert any("touch_storage_and_swallow" in line for line in by_id["test_uses_the_fixture"]["stack"])


def test_report_mode_names_without_failing_the_test(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def test_swallows():
    touch_storage_and_swallow()
''', mode="report")
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    report = _report(result)
    assert report["mode"] == "report"
    assert [f["kind"] for f in report["findings"]] == ["unprovisioned_touch"]
    assert "touched_unprovisioned=1" in result.stdout.str()


# It constructs the error itself; under the default (optin) outer run that outer guard sees it.
@pytest.mark.expects_unresolvable_endpoint
@pytest.mark.parametrize("substrate", ["per-test", "engine", "none"])
def test_the_guard_is_inert_outside_optin(pytester, monkeypatch, substrate) -> None:
    """``per-test`` (the escape hatch), its older spelling ``engine`` and ``none``
    all leave the guard uninstalled: nothing patched, nothing reported."""
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

    before = ServiceEndpointUnresolvableError.__init__
    refs = _refs()
    result = _run(pytester, monkeypatch, f'''
from tests import _storage_touch_guard as g
from helpers import touch_storage_and_swallow

def test_swallows():
    assert g._patch_refs == {refs}, "the guard installed nothing in this session"
    touch_storage_and_swallow()
''', substrate=substrate)
    result.assert_outcomes(passed=1)
    assert "storage-touch" not in result.stdout.str()
    assert ServiceEndpointUnresolvableError.__init__ is before


def _refs() -> int:
    from tests import _storage_touch_guard as g

    return g._patch_refs


def test_an_unset_substrate_is_the_opt_in_guard_in_fail_mode(pytester, monkeypatch) -> None:
    """The flip: no variable at all runs the guard, and a touch fails the test."""
    result = _run(pytester, monkeypatch, '''
from helpers import touch_storage_and_swallow

def test_swallows():
    touch_storage_and_swallow()

def test_clean():
    pass
''', substrate=None, mode="")
    result.assert_outcomes(passed=2, errors=1)
    out = result.stdout.str()
    assert "test_swallows: reached storage without a tenant" in out
    assert _report(result)["mode"] == "fail"


def test_a_value_that_is_not_a_substrate_is_a_usage_error(pytester, monkeypatch) -> None:
    result = _run(pytester, monkeypatch, "def test_x():\n    pass\n", substrate="enigne")
    assert result.ret == 4
    assert "'enigne' is not a substrate" in result.stderr.str() + result.stdout.str()


def test_the_class_patch_is_removed_when_the_session_ends(pytester, monkeypatch) -> None:
    """Non-vacuous under any outer mode: the nested session must be SEEN holding
    the patch (reference count one above the outer's, constructor marked), and
    the outer's state must be exactly restored after it. Under an outer optin
    run the classes are already patched, so comparing ``__init__`` before and
    after proves nothing on its own."""
    from tests import _storage_touch_guard as g

    classes = g._guarded_classes()
    assert len(classes) == len(g.GUARDED), "every GUARDED entry resolves to a class"
    before = tuple(c.__init__ for c in classes)
    own = tuple("__init__" in c.__dict__ for c in classes)
    refs = g._patch_refs
    outer_patched = refs > 0
    monkeypatch.setenv("EXPECT_REFS", str(refs + 1))
    monkeypatch.setenv("EXPECT_NAMES", ",".join(c.__name__ for c in classes))
    result = _run(pytester, monkeypatch, """
import os
from tests import _storage_touch_guard as g

def test_the_patch_is_live_in_here():
    assert g._patch_refs == int(os.environ["EXPECT_REFS"])
    for cls in g._guarded_classes():
        assert getattr(cls.__init__, "_nx_storage_touch_patched", False), cls
    assert os.environ["EXPECT_NAMES"].split(",") == [c.__name__ for c in g._guarded_classes()]
""", mode="report")
    result.assert_outcomes(passed=1)
    assert g._patch_refs == refs
    assert tuple(c.__init__ for c in classes) == before
    assert own == tuple("__init__" in c.__dict__ for c in classes)
    assert all(
        getattr(c.__init__, "_nx_storage_touch_patched", False) is outer_patched for c in classes
    )


def test_the_vector_lease_cache_does_not_outlive_its_test(pytester, monkeypatch) -> None:
    """Always on, optin or not: a lease one test cached must not serve the next."""
    result = _run(pytester, monkeypatch, '''
from nexus.db import http_vector_client

def test_1_caches_a_fake_lease():
    http_vector_client._lease_cache = ("http://127.0.0.1:1", "fake-token")

def test_2_sees_no_lease():
    assert http_vector_client._lease_cache is None
''', optin=False)
    result.assert_outcomes(passed=2)


def test_optin_counts_as_a_session_that_may_boot_the_engine() -> None:
    from tests.conftest import _selected_t2_substrate_boots_engine

    saved = os.environ.get("NX_TEST_T2_SUBSTRATE")
    try:
        for value, expected in (
            ("optin", True), ("none", False), (None, True), ("sqlite", False),
            ("per-test", True), ("engine", True),
        ):
            if value is None:
                os.environ.pop("NX_TEST_T2_SUBSTRATE", None)
            else:
                os.environ["NX_TEST_T2_SUBSTRATE"] = value
            assert _selected_t2_substrate_boots_engine() is expected, value
    finally:
        if saved is None:
            os.environ.pop("NX_TEST_T2_SUBSTRATE", None)
        else:
            os.environ["NX_TEST_T2_SUBSTRATE"] = saved


def test_workers_report_into_one_merged_file_under_xdist(pytester, monkeypatch) -> None:
    """Each worker writes its own file; the controller prints ONE merged path."""
    pytest.importorskip("xdist")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]),
    )
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "optin")
    monkeypatch.setenv("NX_STORAGE_TOUCH_GUARD_MODE", "report")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(helpers=_TOUCH)
    pytester.makepyfile(test_inner='''
import pytest
from helpers import touch_storage_and_swallow

@pytest.mark.parametrize("i", range(6))
def test_touches(i):
    touch_storage_and_swallow()

@pytest.mark.parametrize("i", range(6))
def test_provisioned(i, t2_tenant):
    touch_storage_and_swallow()

@pytest.mark.parametrize("i", range(4))
def test_clean(i):
    pass
''')
    result = pytester.runpytest_subprocess(
        "-p", "tests._storage_touch_guard", "-p", "no:cacheprovider", "-n", "2", "-q",
    )
    result.assert_outcomes(passed=16)
    report = _report(result)
    assert report["workers"] == 2
    c = report["counts"]
    assert (c["tests"], c["touched_unprovisioned"], c["provisioned"], c["clean_unprovisioned"]) == (16, 6, 6, 4)
    assert len(report["findings"]) == 6

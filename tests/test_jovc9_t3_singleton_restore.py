# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jovc9: the T3 singleton survives the patch that built it.

``mcp_infra.get_t3()`` MEMOISES ``nexus.db.make_t3()``'s return value into the
process-wide ``_t3_instance``. A test that patches ``nexus.db.make_t3`` and
drives any code path reaching ``get_t3()`` therefore installs its stub
PERMANENTLY: ``patch`` restores the factory on exit, never the memo. The stub
then serves every later test in the same xdist worker — including the scenario
journeys' real ``nx index md``, whose chunk write lands in the stub, never
reaches PG, and is correctly refused by the RUNFENCE completion check
(``referenced=1 present=0``).

That is one link upstream of nexus-gtl01 (a fixture calling ``inject_t3``
without restoring) and is invisible to ``test_t3_singleton_leak_lint.py``,
which scans for ``inject_t3`` / ``_t3_instance =`` in fixtures — this shape has
neither.

These tests pin BOTH halves: the hazard itself (so a refactor that removes the
memo does not silently leave a guard nobody understands), and ``conftest``'s
``_restore_t3_singleton`` autouse guard that neutralizes it.
"""

from __future__ import annotations

import types
import warnings
from unittest.mock import MagicMock, patch

import pytest

from nexus import mcp_infra


def _fake_request(nodeid: str = "tests/fake.py::test_leaker"):
    return types.SimpleNamespace(node=types.SimpleNamespace(nodeid=nodeid))


def _drive(gen, *, leak) -> None:
    """Run the guard fixture's setup, install *leak*, run its teardown."""
    next(gen)
    mcp_infra.inject_t3(leak)
    with pytest.raises(StopIteration):
        next(gen)


class _NotAMock:
    """A stand-in for a real handle (T3Database / HttpVectorClient)."""


@pytest.fixture()
def guard():
    """The conftest autouse guard's underlying generator function.

    pytest wraps fixture functions and refuses a direct call; the real
    generator function is stashed on the wrapper.
    """
    from tests import conftest

    fixture = conftest._restore_t3_singleton
    wrapped = getattr(fixture, "__pytest_wrapped__", None)
    return wrapped.obj if wrapped is not None else fixture.__wrapped__


def test_get_t3_memoises_patched_make_t3_past_the_patch() -> None:
    """THE HAZARD. ``patch`` restores the factory; the memo outlives it."""
    mcp_infra.inject_t3(None)
    sentinel = MagicMock()
    with patch("nexus.db.make_t3", return_value=sentinel):
        assert mcp_infra.get_t3() is sentinel
    # Patch is gone. The singleton is not.
    assert mcp_infra._t3_instance is sentinel, (
        "get_t3() no longer memoises make_t3()'s return — if that is "
        "deliberate, the jovc9 guard in conftest can be retired with it."
    )
    mcp_infra.inject_t3(None)


def test_guard_restores_prior_handle_and_fails_the_leaker_on_a_mock(guard) -> None:
    """A mock in the production singleton is never legitimate: fail AT the leaker."""
    prior = _NotAMock()
    mcp_infra.inject_t3(prior)
    gen = guard(_fake_request())
    with pytest.raises(pytest.fail.Exception) as exc:
        _drive(gen, leak=MagicMock())
    assert "tests/fake.py::test_leaker" in str(exc.value)
    assert "_t3_instance" in str(exc.value)
    assert mcp_infra._t3_instance is prior, "guard must restore the PRIOR handle, not None"
    mcp_infra.inject_t3(None)


def test_guard_restores_and_warns_on_a_non_service_handle_leak(guard, monkeypatch) -> None:
    """The gtl01 shape: a real but NON-service handle. Restore + warn, no fail."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    mcp_infra.inject_t3(None)
    gen = guard(_fake_request("tests/fake.py::test_real_leaker"))
    with pytest.warns(UserWarning, match="test_real_leaker"):
        _drive(gen, leak=_NotAMock())
    assert mcp_infra._t3_instance is None


def test_guard_restores_a_service_backed_handle_without_warning(guard, monkeypatch) -> None:
    """Lazy init of the REAL singleton is not a defect — restore, stay quiet.

    Restoring anyway is deliberate: an ``HttpVectorClient`` bakes in the tenant
    token it saw at construction and the substrate mints one tenant per test.
    """
    from nexus.db.http_vector_client import HttpVectorClient

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    mcp_infra.inject_t3(None)
    gen = guard(_fake_request())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _drive(gen, leak=HttpVectorClient())
    assert mcp_infra._t3_instance is None


#: Set by the first module-scoped-survival test so the second can tell
#: "same worker process" from "xdist split us across workers".
_SEEN_MODULE_HANDLE: list[object] = []


@pytest.fixture(scope="module")
def module_scoped_injection():
    """A MODULE-scoped T3 injection — the shape nexus-0ne4s had to protect.

    The objection that kept conftest from guarding ``_t3_instance`` at gtl01
    time was that an autouse function-scoped reset "would invalidate
    module-scoped T1/T3 injections that tests legitimately expect to survive
    across a file". SNAPSHOT/RESTORE is supposed to sidestep it: pytest orders
    the fixture closure by scope, so this fixture is set up BEFORE the
    function-scoped autouse guard takes its snapshot, the snapshot therefore
    already contains this handle, and restoring the snapshot re-installs it.

    0ne4s's acceptance criterion was to VERIFY that ordering empirically
    rather than reason about it. The two tests below are that verification.
    """
    handle = _NotAMock()
    mcp_infra.inject_t3(handle)
    yield handle
    mcp_infra.inject_t3(None)


def test_module_scoped_injection_is_visible_in_the_first_test(module_scoped_injection) -> None:
    assert mcp_infra._t3_instance is module_scoped_injection
    _SEEN_MODULE_HANDLE.append(module_scoped_injection)


def test_module_scoped_injection_survives_the_guard_into_a_second_test(
    module_scoped_injection,
) -> None:
    """The guard restored the SNAPSHOT, so the module injection is still live."""
    assert mcp_infra._t3_instance is module_scoped_injection, (
        "the autouse guard reset a module-scoped injection instead of "
        "restoring it — this is exactly the regression nexus-0ne4s feared"
    )
    if _SEEN_MODULE_HANDLE:  # same worker process: true cross-test survival
        assert _SEEN_MODULE_HANDLE[0] is module_scoped_injection


def test_guard_is_a_no_op_when_nothing_changed(guard) -> None:
    """The common case: no touch, no restore, no warning, no failure."""
    prior = _NotAMock()
    mcp_infra.inject_t3(prior)
    gen = guard(_fake_request())
    next(gen)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(StopIteration):
            next(gen)
    assert mcp_infra._t3_instance is prior
    mcp_infra.inject_t3(None)


def test_guard_tolerates_a_test_that_reset_the_singleton(guard) -> None:
    """``reset_singletons()`` inside a test is not a leak — restore, no warning."""
    prior = _NotAMock()
    mcp_infra.inject_t3(prior)
    gen = guard(_fake_request())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _drive(gen, leak=None)
    assert mcp_infra._t3_instance is prior
    mcp_infra.inject_t3(None)

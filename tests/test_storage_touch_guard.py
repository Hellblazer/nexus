# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni: the storage-touch guard behind the opt-in test tenant
(``tests/_storage_touch_guard.py``).

Each case runs the real plugin in a nested ``pytester`` session with a tiny
stand-in ``t2_tenant`` fixture (the real one boots an engine; the guard only
needs to know when a tenant is active). Tests inside a nested session run in a
fixed order, so the cases do not depend on how the outer run is split.
"""
from __future__ import annotations

import ast
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


@pytest.fixture
def t2_service_env():
    with guard.tenant_active():
        yield "tenant"


@pytest.fixture(autouse=True)
def _pin_t2_substrate(request):
    # the marker path of tests/conftest.py: an autouse FUNCTION-scoped fixture that fetches the
    # tenant with getfixturevalue, which the item's static fixture closure does not list
    if request.node.get_closest_marker("t2_tenant") is not None:
        request.getfixturevalue("t2_tenant")
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
         sweep: bool = True, floor: int | str | None = None):
    """Run ``body`` in a nested session. ``substrate`` names ``NX_TEST_T2_SUBSTRATE``
    (None leaves it unset: the default); by default ``optin`` picks ``optin`` and
    not ``optin`` picks ``none``, which the guard is inert under.
    ``sweep`` sets ``NX_STORAGE_TOUCH_SWEEP``, which asks the whole-run shape checks.
    ``NX_STORAGE_TOUCH_SWEEP_FLOOR`` is deleted unless ``floor`` names it, so a floor
    exported in the outer environment cannot turn a ``sweep=False`` run into a swept one."""
    if substrate == "":
        substrate = "optin" if optin else "none"
    if substrate is None:
        monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    else:
        monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", substrate)
    if sweep:
        monkeypatch.setenv("NX_STORAGE_TOUCH_SWEEP", "1")
    else:
        monkeypatch.delenv("NX_STORAGE_TOUCH_SWEEP", raising=False)
    if floor is None:
        monkeypatch.delenv("NX_STORAGE_TOUCH_SWEEP_FLOOR", raising=False)
    else:
        monkeypatch.setenv("NX_STORAGE_TOUCH_SWEEP_FLOOR", str(floor))
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


def assert_exit_failed_for(code: int, output: str, *reasons: str) -> None:
    """``code`` is non-zero AND ``output`` carries every ``reason``: the form for a child
    process whose exit status and combined output the test already holds.

    A bare ``code != 0`` proves nothing under the default ``sweep=True``: a body
    with no provisioned or no clean test fails on 'vacuous sweep: provisioned=0'
    whatever the rule under test did (nexus-j002k: the late-touch tests passed
    on that for a whole phase). ``tests/test_storage_touch_guard.py::
    test_no_fail_mode_test_asserts_a_bare_nonzero_exit`` keeps the bare form out
    of the guard test files. At least one reason is required, and a blank one is
    refused: ``"" in output`` is true of any output."""
    assert reasons, "name what the session must have failed for"
    assert all(isinstance(r, str) and r.strip() for r in reasons), f"a blank reason names nothing: {reasons!r}"
    assert code != 0, output
    for reason in reasons:
        assert reason in output, (reason, output)


def assert_session_failed_for(result: pytest.RunResult, *reasons: str) -> None:
    """The nested session ended non-zero AND printed every ``reason`` (see ``assert_exit_failed_for``)."""
    assert_exit_failed_for(result.ret, result.stdout.str() + result.stderr.str(), *reasons)


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


def _messages(out: str, test: str) -> list[str]:
    """Every output line that carries the guard's message for ``test``."""
    return [line for line in out.splitlines() if f"{test}: reached storage without a tenant" in line]


def test_a_test_that_already_requests_the_tenant_is_told_to_mark_it(pytester, monkeypatch) -> None:
    """nexus-0lo4l: ``def test_x(db, t2_service_env)`` has the request; the fixture
    before it touched storage first. 'Request the fixture' is wrong advice there:
    only the marker (tenant first) fixes it. A test that never asked still gets
    the old advice, and a touch after the tenant was torn down says so."""
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.fixture
def seeded():
    touch_storage_and_swallow()
    return 1

@pytest.fixture
def leaky():
    yield 1
    touch_storage_and_swallow()

def test_asks_for_the_service_env_too_late(seeded, t2_service_env):
    assert seeded == 1

def test_asks_for_the_tenant_too_late(seeded, t2_tenant):
    assert seeded == 1

def test_never_asked(seeded):
    assert seeded == 1

def test_tenant_is_gone_before_the_teardown_touch(leaky, t2_tenant):
    assert leaky == 1
''')
    result.assert_outcomes(passed=4, errors=4)
    out = result.stdout.str()
    for test, asked in (("test_asks_for_the_service_env_too_late", "t2_service_env"),
                        ("test_asks_for_the_tenant_too_late", "t2_tenant")):
        lines = _messages(out, test)
        assert lines, test
        assert any(
            f"The {asked} fixture was set up after fixture seeded touched storage" in line
            and "Put @pytest.mark.t2_tenant on the test" in line
            and "sets the tenant up first among function-scoped fixtures" in line
            and "module-, class- or session-scoped" in line
            for line in lines
        ), lines
        assert not any("Request the t2_tenant fixture" in line for line in lines), lines
        assert not any("the fixture above" in line for line in lines), lines
    never = _messages(out, "test_never_asked")
    assert never and all("already requests" not in line for line in never), never
    assert any("Request the t2_tenant fixture (or mark it t2_tenant)" in line for line in never), never
    teardown = _messages(out, "test_tenant_is_gone_before_the_teardown_touch")
    assert any(
        "the tenant was already torn down when the touch happened" in line
        and "If a fixture is function-scoped, put @pytest.mark.t2_tenant on the test" in line
        and "If a fixture is module-, class- or session-scoped, its teardown runs after the function-scoped tenant" in line
        for line in teardown
    ), teardown
    assert not any("already there" in line for line in teardown), teardown
    by_id = {f["nodeid"].split("::")[-1]: f for f in _report(result)["findings"]}
    assert by_id["test_asks_for_the_service_env_too_late"]["requested_tenant"] == ["t2_service_env"]
    assert by_id["test_asks_for_the_tenant_too_late"]["requested_tenant"] == ["t2_tenant"]
    assert by_id["test_never_asked"]["requested_tenant"] == []
    assert all(by_id[t]["dynamic_tenant"] == [] for t in by_id)  # a parameter is not a dynamic request
    assert by_id["test_tenant_is_gone_before_the_teardown_touch"]["phases"] == ["teardown"]


def test_a_marked_test_is_recorded_as_having_the_tenant_and_told_what_the_marker_cannot_do(pytester, monkeypatch) -> None:
    """nexus-0lo4l: the marker fetches the tenant with ``getfixturevalue`` from an autouse
    FUNCTION-scoped fixture, so ``item.fixturenames`` never lists it. A marked test must still count
    as having the tenant, and the advice must not tell it to add the mark it carries. The marker
    sets the tenant up first among function-scoped fixtures (a function-scoped touching fixture is
    clean); a module-scoped fixture runs before it, which no mark can change."""
    result = _run(pytester, monkeypatch, '''
import pytest
from helpers import touch_storage_and_swallow

@pytest.fixture
def seeded():
    touch_storage_and_swallow()
    return 1

@pytest.fixture(scope="module")
def seeded_for_the_module():
    touch_storage_and_swallow()
    return 1

@pytest.mark.t2_tenant
def test_marked_with_a_function_scoped_touch_is_clean(seeded):
    assert seeded == 1

@pytest.mark.t2_tenant
def test_marked_with_a_module_scoped_touch_is_named(seeded_for_the_module):
    assert seeded_for_the_module == 1

@pytest.fixture(scope="module")
def leaky_for_the_module():
    yield 1
    touch_storage_and_swallow()

@pytest.mark.t2_tenant
def test_marked_with_a_module_scoped_teardown_touch_is_named(leaky_for_the_module):
    assert leaky_for_the_module == 1
''')
    result.assert_outcomes(passed=3, errors=2)
    out = result.stdout.str()
    assert not _messages(out, "test_marked_with_a_function_scoped_touch_is_clean")
    lines = _messages(out, "test_marked_with_a_module_scoped_touch_is_named")
    assert any(
        "The test already got t2_tenant from its marker" in line
        and "fixture seeded_for_the_module touched storage before it" in line
        and "module-, class- or session-scoped" in line
        for line in lines
    ), lines
    assert not any("Put @pytest.mark.t2_tenant" in line or "Request the t2_tenant fixture" in line for line in lines), lines
    teardown = _messages(out, "test_marked_with_a_module_scoped_teardown_touch_is_named")
    assert any(
        "The test already got t2_tenant from its marker" in line
        and "the tenant was already torn down when the touch happened" in line
        and "The marker is already there" in line
        and "If a fixture is module-, class- or session-scoped, its teardown runs after "
        "the function-scoped tenant is gone" in line
        and "give that fixture a tenant of its own or a narrower scope" in line
        and "expects_unresolvable_endpoint" in line
        for line in teardown
    ), teardown
    assert not any("Put @pytest.mark.t2_tenant" in line or "put @pytest.mark.t2_tenant" in line for line in teardown), teardown
    by_id = {f["nodeid"].split("::")[-1]: f for f in _report(result)["findings"]}
    assert set(by_id) == {
        "test_marked_with_a_module_scoped_touch_is_named",
        "test_marked_with_a_module_scoped_teardown_touch_is_named",
    }
    for finding in by_id.values():
        assert finding["requested_tenant"] == ["t2_tenant"] and finding["dynamic_tenant"] == ["t2_tenant"]
    assert by_id["test_marked_with_a_module_scoped_teardown_touch_is_named"]["phases"] == ["teardown"]


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
@pytest.mark.parametrize("substrate", ["none"])
def test_the_guard_is_inert_outside_optin(pytester, monkeypatch, substrate) -> None:
    """``none`` leaves the guard uninstalled: nothing patched, nothing reported."""
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


# ── a non-zero exit proves nothing on its own (nexus-j002k) ──────────────────

#: The files whose nested or child sessions are expected to FAIL. The first two run the storage-touch
#: guard under pytester; the last two assert that a guard fails a child session the same way
#: (``rc != 0`` with a message assert next to it, which nothing kept paired).
_GUARD_TEST_FILES = (
    "test_storage_touch_guard.py", "test_storage_touch_guard_report.py",
    "test_pfuns_guard_wiring.py", "test_real_config_dir_guard_wiring.py",
)
_HELPERS = frozenset({"assert_session_failed_for", "assert_exit_failed_for"})
_EXIT_ATTRS = frozenset({"ret", "returncode"})
#: Names that read as an exit code when a test unpacks a helper's result (``rc, out = _run_inner(...)``).
_EXIT_NAMES = frozenset({"rc", "ret", "retcode", "returncode", "exit_code", "exitcode"})
_WRAPPERS = frozenset({"bool", "int", "abs"})
#: The only exit codes an equality may name: OK, and USAGE_ERROR (which no sweep verdict can produce).
#: Any other code, 1/TESTS_FAILED above all, is what a vacuous sweep ends with too.
_SPECIFIC_CODES = frozenset({0, 4})
_SPECIFIC_MEMBERS = frozenset({"OK", "USAGE_ERROR"})


def _unwrap(node: ast.AST) -> ast.AST:
    while (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _WRAPPERS
        and len(node.args) == 1 and not node.keywords
    ):
        node = node.args[0]
    return node


def _is_exit(node: ast.AST, aliases: set[str]) -> bool:
    """An exit status: ``x.ret`` / ``x.returncode``, a name that reads as one or was assigned one,
    and ``bool()`` / ``int()`` of either."""
    node = _unwrap(node)
    return (isinstance(node, ast.Attribute) and node.attr in _EXIT_ATTRS) or (
        isinstance(node, ast.Name) and (node.id in _EXIT_NAMES or node.id in aliases)
    )


def _is_specific_code(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) is int and node.value in _SPECIFIC_CODES
    if isinstance(node, ast.Attribute) and node.attr in _SPECIFIC_MEMBERS:
        base = node.value  # ExitCode.OK or pytest.ExitCode.OK
        return (isinstance(base, ast.Name) and base.id == "ExitCode") or (
            isinstance(base, ast.Attribute) and base.attr == "ExitCode"
        )
    return False


def _compare_is_loose(node: ast.Compare, aliases: set[str], negated: bool = False) -> bool:
    """Under ``not`` the polarity flips: ``not r.ret == 0`` accepts every failure, while
    ``not r.ret != 0`` is the specific ``== 0``."""
    specific_op = ast.NotEq if negated else ast.Eq
    operands = [node.left, *node.comparators]
    for left, op, right in zip(operands, node.ops, operands[1:]):
        for mine, other in ((left, right), (right, left)):
            if _is_exit(mine, aliases) and not (isinstance(op, specific_op) and _is_specific_code(other)):
                return True
    return False


def _is_loose(node: ast.AST, aliases: set[str], bools: set[str], negated: bool = False) -> bool:
    """True when ``node``, read as a condition, passes on 'it failed' without saying why.
    ``negated`` is true under an odd number of ``not``: a specific equality turns loose there."""
    if isinstance(node, ast.BoolOp):
        return any(_is_loose(v, aliases, bools, negated) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _is_loose(node.operand, aliases, bools, not negated)
    if isinstance(node, ast.IfExp):
        return any(_is_loose(n, aliases, bools, negated) for n in (node.test, node.body, node.orelse))
    if isinstance(node, ast.Compare):
        return _compare_is_loose(node, aliases, negated) or any(
            _compare_is_loose(c, aliases, negated)
            for c in ast.walk(node) if isinstance(c, ast.Compare) and c is not node
        )
    if _is_exit(node, aliases) or (isinstance(node, ast.Name) and node.id in bools):
        return True  # truthiness of the exit status
    # a call or a comprehension that wraps a comparison (``any(r.ret != 0 for r in runs)``)
    return any(_compare_is_loose(c, aliases) for c in ast.walk(node) if isinstance(c, ast.Compare))


def _aliases(fn: ast.AST) -> tuple[set[str], set[str]]:
    """(names assigned an exit status, names assigned a loose condition on one) in ``fn``, followed
    to a fixed point so ``rc = result.ret`` then ``failed = rc != 0`` is seen."""
    assigns = sorted(
        (n for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr))),
        key=lambda n: (n.lineno, n.col_offset),
    )
    exits: set[str] = set()
    bools: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n in assigns:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if not names or n.value is None:
                continue
            if _is_exit(n.value, exits):
                grown = names - exits
                exits |= grown
            elif _is_loose(n.value, exits, bools):
                grown = names - bools
                bools |= grown
            else:
                continue
            changed = changed or bool(grown)
    return exits, bools


def _loose_exit_asserts(source: str) -> tuple[list[str], int]:
    """(``<test>:<line>`` of every assert that accepts an exit status without naming
    which one, calls of the helpers) in ``source``. The helpers' own bodies are exempt.

    Specific, so passing: ``== 0`` / ``== pytest.ExitCode.OK`` and the usage error
    ``== 4`` / ``ExitCode.USAGE_ERROR``. Loose, so flagged: truthiness, ``!=``, ordering,
    ``in``, and an equality to any other code (``== 1`` is exactly what a vacuous
    sweep ends with); whether the status is spelled ``.ret`` or ``.returncode``, bound to a
    name first, wrapped in ``bool()`` / ``int()``, or buried in a nested boolean expression."""
    tree = ast.parse(source)
    flagged: list[str] = []
    calls = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _HELPERS:
            continue
        calls += sum(
            1 for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) in _HELPERS
        )
        exits, bools = _aliases(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assert) and _is_loose(node.test, exits, bools):
                flagged.append(f"{fn.name}:{node.lineno}")
    return list(dict.fromkeys(flagged)), calls


#: The live helper-call count over ``_GUARD_TEST_FILES`` at the time of writing, and how far it may
#: shrink before the scan is taken to have stopped seeing the tests (a rename of the helper, a
#: restructure). Raise ``_EXPECTED_HELPER_CALLS`` when tests are added; the margin is slack for removals.
_EXPECTED_HELPER_CALLS = 17
_HELPER_CALLS_MARGIN = 2


def test_no_fail_mode_test_asserts_a_bare_nonzero_exit() -> None:
    """The late-touch tests of the phase-4 fix round asserted ``ret != 0`` under the
    default ``sweep=True`` and passed on 'vacuous sweep: provisioned=0' for a
    whole phase. Every assertion that a nested session FAILED goes through
    ``assert_session_failed_for`` (or ``assert_exit_failed_for`` for a child process), which
    also asserts what it failed for."""
    flagged: list[str] = []
    total_calls = 0
    for name in _GUARD_TEST_FILES:
        found, calls = _loose_exit_asserts((Path(__file__).resolve().parent / name).read_text())
        flagged += [f"{name}::{where}" for where in found]
        total_calls += calls
    assert not flagged, f"assert the reason the session failed, with {sorted(_HELPERS)}: {flagged}"
    # non-vacuity: the scan found the tests that assert a failing session
    assert total_calls >= _EXPECTED_HELPER_CALLS - _HELPER_CALLS_MARGIN, total_calls


def test_the_exit_scan_flags_a_bare_failure_and_passes_a_specific_one() -> None:
    """Positive control for the scan above: each spelling the first version of the scan missed."""
    loose = [
        "def test_a(result):\n    assert result.ret != 0\n",
        "def test_b(result):\n    assert result.ret\n",
        "def test_c(result):\n    assert result.ret != 0 and 'x' in result.stdout.str()\n",
        "def test_d(result):\n    assert result.ret > 0\n",
        "def test_e(result):\n    assert not result.ret\n",
        # an equality to the exit a vacuous sweep also gives
        "def test_f(result):\n    assert result.ret == 1\n",
        "def test_g(result):\n    assert result.ret == pytest.ExitCode.TESTS_FAILED\n",
        "def test_h(result):\n    assert 2 == result.ret\n",
        "def test_i(result):\n    assert result.ret in (1, 2)\n",
        "def test_j(result):\n    assert result.ret == ExitCode.INTERNAL_ERROR\n",
        "def test_k(result, want):\n    assert result.ret == want\n",
        # wrapped
        "def test_l(result):\n    assert bool(result.ret)\n",
        "def test_m(result):\n    assert int(result.ret) > 0\n",
        "def test_n(result):\n    assert bool(result.ret) == True\n",
        # bound to a name first, one level and two
        "def test_o(result):\n    ret = result.ret\n    assert ret != 0\n",
        "def test_p(result):\n    code = result.ret\n    again = code\n    assert again\n",
        "def test_q(result):\n    failed = result.ret != 0\n    assert failed\n",
        "def test_r(result):\n    code: int = result.ret\n    assert code == 1\n",
        # .returncode, and a name that reads as an exit code after unpacking
        "def test_s(proc):\n    assert proc.returncode != 0\n",
        "def test_t(proc):\n    assert proc.returncode == 1\n",
        "def test_u(run):\n    rc, out = run()\n    assert rc != 0, out\n",
        # negation flips which equality is specific: ``not ret == 0`` accepts every failure
        "def test_nota(result):\n    assert not result.ret == 0\n",
        "def test_notb(result):\n    assert not (result.ret == 0)\n",
        "def test_notc(result):\n    assert not result.ret != 1\n",
        "def test_notd(result):\n    assert not result.ret != 0 and not result.ret == 4\n",
        # nested boolean expressions
        "def test_v(result, a, b):\n    assert (a and b) or result.ret != 0\n",
        "def test_w(result, a):\n    assert a and not (a and result.ret)\n",
        "def test_x(result, a):\n    assert (result.ret == 0) or (result.ret == 1)\n",
        "def test_y(runs):\n    assert any(r.ret != 0 for r in runs)\n",
        "def test_z(result):\n    assert 'x' in result.stdout.str() if result.ret else True\n",
    ]
    for source in loose:
        assert _loose_exit_asserts(source)[0], source
    specific = [
        "def test_a(result):\n    assert result.ret == 0\n",
        "def test_b(result):\n    assert result.ret == 4\n",
        "def test_c(result):\n    assert_session_failed_for(result, 'why')\n",
        "def test_d(result):\n    assert result.ret == pytest.ExitCode.OK\n",
        "def test_e(result):\n    assert result.ret == ExitCode.USAGE_ERROR\n",
        "def test_f(result):\n    assert 0 == result.ret\n",
        "def test_g(proc):\n    assert proc.returncode == 0\n",
        "def test_h(result):\n    ok = result.ret == 0\n    assert ok\n",
        "def test_i(result):\n    ret = result.ret\n    assert ret == 0 and 'x' in result.stdout.str()\n",
        "def test_j(run):\n    rc, out = run()\n    assert rc == 0, out\n",
        "def test_k(run):\n    rc, out = run()\n    assert_exit_failed_for(rc, out, 'why')\n",
        "def test_l(out):\n    assert 'x' in out and not out.errors\n",
        "def test_m(result):\n    assert not result.ret != 0\n",
        "def test_n(result):\n    assert not not result.ret == 0\n",
    ]
    for source in specific:
        assert _loose_exit_asserts(source)[0] == [], source
    assert _loose_exit_asserts("def test_c(result):\n    assert_session_failed_for(result, 'why')\n")[1] == 1
    assert _loose_exit_asserts("def test_k(rc, out):\n    assert_exit_failed_for(rc, out, 'why')\n")[1] == 1
    # the helpers' own bodies are exempt and are not counted as uses
    assert _loose_exit_asserts("def assert_session_failed_for(r):\n    assert r.ret != 0\n    assert_exit_failed_for(1, '')\n") == ([], 0)


def test_a_failing_session_for_another_reason_is_refused_by_the_helper(pytester, monkeypatch) -> None:
    """The weakness itself, live: a body with nothing provisioned fails the sweep
    under the default ``sweep=True`` whatever any other rule does, so a bare
    exit-status check passes for it. The helper refuses it unless the reason it
    names is the one printed."""
    result = _run(pytester, monkeypatch, "def test_nothing():\n    pass\n", mode="fail")
    # this session failed, and for a reason no rule under test has anything to do with: any
    # exit-status check passes for it, so only the named reason tells it apart
    assert_session_failed_for(result, "vacuous sweep: provisioned=0")
    with pytest.raises(AssertionError):  # ... and says nothing about a late touch
        assert_session_failed_for(result, "late storage touch")
    with pytest.raises(AssertionError):
        assert_session_failed_for(result)
    for empty in ("", " ", "\n\t"):  # '' is a substring of everything, so it would name nothing
        with pytest.raises(AssertionError):
            assert_session_failed_for(result, empty)
        with pytest.raises(AssertionError):
            assert_session_failed_for(result, "vacuous sweep: provisioned=0", empty)


def test_every_marker_the_default_addopts_deselect_is_uncensused() -> None:
    """The census runs under the default addopts, so a marker they deselect was
    never examined and must keep a tenant per test. ``lint`` was missing: the
    docs-readers CI job and a local ``pytest -m lint`` run it under the default
    substrate."""
    import tomllib

    from tests.conftest import _UNCENSUSED_MARKERS

    addopts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    (expression,) = re.findall(r"-m\s+'([^']+)'", addopts)
    deselected = set(re.findall(r"not\s+(\w+)", expression))
    assert deselected >= {"integration", "slow", "lint"}, expression
    assert deselected <= set(_UNCENSUSED_MARKERS), sorted(deselected - set(_UNCENSUSED_MARKERS))


def test_optin_counts_as_a_session_that_may_boot_the_engine() -> None:
    from tests.conftest import _selected_t2_substrate_boots_engine

    saved = os.environ.get("NX_TEST_T2_SUBSTRATE")
    try:
        for value, expected in (
            ("optin", True), ("none", False), (None, True), ("sqlite", False),
            ("per-test", False), ("engine", False),
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


def test_the_guard_and_the_conftest_retire_the_same_values() -> None:
    """The guard lets a retired value through configure so the conftest can refuse it
    by name; the two lists are one fact kept in two modules (nexus-llo7w)."""
    from tests import _storage_touch_guard as g
    from tests.conftest import _RETIRED_T2_SUBSTRATES

    assert set(_RETIRED_T2_SUBSTRATES) == g.RETIRED_VALUES
    assert {"per-test", "engine"} <= g.RETIRED_VALUES
    assert not g.RETIRED_VALUES & g.KNOWN_VALUES


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

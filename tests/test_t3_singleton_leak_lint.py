# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lint: a fixture that seeds the process-wide T3 singleton must un-seed it.

nexus-gtl01. ``mcp_infra._t3_instance`` is a PROCESS-WIDE singleton, and
``mcp_infra.get_t3()`` is what ``doc_indexer._index_document`` resolves its
write handle from in service mode — so it is on the production CLI write path,
not just the MCP tool path. A fixture that installs a test handle there and
never removes it poisons every LATER test in the same worker process: the
later test's ``nx index md`` writes into the leaked handle (typically an
in-memory ``T3Database``) and never reaches the engine, so its chunk is
genuinely absent at verify time and the RUNFENCE completion check refuses.

That is exactly how nexus-gtl01 presented — two scenario journeys failing
``completion REFUSED ... referenced=1 present=0`` only ever under ``-n auto``,
green standalone. It read as load-sensitive silent write loss for two days.
It was neither load nor write loss: xdist's dynamic distribution decides which
tests share a worker, and ``tests/test_catalog_e2e.py::injected_active``
returned without restoring the singleton. Deterministic reproduction on an
idle box: run any ``injected_active`` test, then the journey, in one process.

Three test files already carry hand-rolled defensive ``inject_t3(None)``
fixtures whose docstrings name this leak explicitly. This lint is the
mechanized version of that folklore: it fails at the LEAKER instead of
somewhere downstream, hours later, in an unrelated file.

Scope: fixtures only. A plain test function that injects and never resets is
a separate (and rarer) shape; extend ``_INJECT_CALLS`` handling here if one
ever appears.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

_TESTS_DIR = pathlib.Path(__file__).parent

#: Call names that install a handle into the singleton.
_INJECT_CALLS = frozenset({"inject_t3", "_inject_t3"})
#: Call names that clear it again.
_RESET_CALLS = frozenset({"reset_singletons", "_reset_singletons"})


def _is_fixture(fn: ast.FunctionDef) -> bool:
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        attr = getattr(node, "attr", None) or getattr(node, "id", None)
        if attr == "fixture":
            return True
    return False


def _seeds_singleton(fn: ast.FunctionDef) -> bool:
    """True when *fn* installs a non-None handle into the T3 singleton."""
    for node in ast.walk(fn):
        # mcp_infra._t3_instance = <handle>
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "_t3_instance":
                    # Assigning None IS the reset form, not a seed.
                    if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                        return True
        # inject_t3(<handle>) / _inject_t3(<handle>)
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _INJECT_CALLS and node.args:
                arg = node.args[0]
                if not (isinstance(arg, ast.Constant) and arg.value is None):
                    return True
    return False


def _clears_after_yield(fn: ast.FunctionDef) -> bool:
    """True when *fn* yields and then clears the singleton.

    ``monkeypatch.setattr``-based seeding is exempt at the caller (pytest
    restores it), and is not matched by :func:`_seeds_singleton` in the first
    place — only a raw assignment or an ``inject_t3`` call is.
    """
    yields = [n for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))]
    if not yields:
        return False
    last_yield_line = max(n.lineno for n in yields)
    for node in ast.walk(fn):
        # ast.walk also yields context/argument nodes that carry no position.
        if getattr(node, "lineno", 0) <= last_yield_line:
            continue
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _RESET_CALLS:
                return True
            if name in _INJECT_CALLS and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value is None:
                    return True
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "_t3_instance"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is None
                ):
                    return True
    return False


def _is_autouse(fn: ast.FunctionDef) -> bool:
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        node = dec.func
        if (getattr(node, "attr", None) or getattr(node, "id", None)) != "fixture":
            continue
        for kw in dec.keywords:
            if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value:
                return True
    return False


def _has_module_autouse_resetter(tree: ast.Module) -> bool:
    """True when the module carries an autouse fixture that clears the
    singleton after its yield.

    This is the OTHER legitimate discipline in this suite: rather than each
    injecting fixture cleaning up after itself, the file installs one autouse
    ``_reset`` that brackets every test (``test_mcp_server.py``,
    ``test_phase3_structured_chash.py``, ``test_rdr052_verification.py`` all do
    this). It is sound because pytest sets autouse fixtures up first and tears
    them down LAST, so the reset runs after the injecting fixture's own
    teardown. ``tests/test_catalog_e2e.py`` had NEITHER discipline, which is
    precisely why it was the nexus-gtl01 leaker.
    """
    return any(
        isinstance(node, ast.FunctionDef)
        and _is_fixture(node)
        and _is_autouse(node)
        and _clears_after_yield(node)
        for node in ast.walk(tree)
    )


def _offenders() -> list[str]:
    bad: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a broken test file fails elsewhere
            continue
        if _has_module_autouse_resetter(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not _is_fixture(node):
                continue
            if _seeds_singleton(node) and not _clears_after_yield(node):
                bad.append(f"{path.relative_to(_TESTS_DIR.parent)}::{node.name}")
    return bad


def test_no_fixture_leaks_the_t3_singleton() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these fixtures seed mcp_infra._t3_instance (or call inject_t3) and "
        "never clear it, leaking a test T3 handle into every LATER test in "
        "the same worker process — the nexus-gtl01 failure mode, which "
        "presents as an unrelated file's `nx index md` refusing completion "
        "with referenced=N present=0. Make the fixture a `yield` fixture and "
        "call _reset_singletons() (or inject_t3(None)) after the yield:\n  "
        + "\n  ".join(offenders)
    )


def test_lint_is_non_vacuous() -> None:
    """The detector must actually fire on the shape it claims to catch.

    Without this, a refactor that broke ``_seeds_singleton`` would leave the
    lint above passing over an empty scan forever.
    """
    leaky = ast.parse(
        "import pytest\n"
        "@pytest.fixture\n"
        "def seeds(handle):\n"
        "    mcp_infra._t3_instance = handle\n"
        "    return handle\n"
    ).body[1]
    assert isinstance(leaky, ast.FunctionDef)
    assert _is_fixture(leaky)
    assert _seeds_singleton(leaky)
    assert not _clears_after_yield(leaky)

    fixed = ast.parse(
        "import pytest\n"
        "@pytest.fixture\n"
        "def seeds(handle):\n"
        "    mcp_infra._t3_instance = handle\n"
        "    yield handle\n"
        "    _reset_singletons()\n"
    ).body[1]
    assert isinstance(fixed, ast.FunctionDef)
    assert _seeds_singleton(fixed)
    assert _clears_after_yield(fixed)

    # The module-autouse escape must be recognised, and must NOT be granted by
    # a non-autouse fixture that merely happens to reset.
    with_autouse = ast.parse(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def _reset():\n"
        "    _reset_singletons()\n"
        "    yield\n"
        "    _reset_singletons()\n"
    )
    assert _has_module_autouse_resetter(with_autouse)
    without_autouse = ast.parse(
        "import pytest\n"
        "@pytest.fixture\n"
        "def _reset():\n"
        "    yield\n"
        "    _reset_singletons()\n"
    )
    assert not _has_module_autouse_resetter(without_autouse)

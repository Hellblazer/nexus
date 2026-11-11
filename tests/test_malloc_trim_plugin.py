# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests._malloc_trim gives freed heap back to the OS at each module boundary (labrun-pex)."""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _malloc_trim

_GLIBC_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux") or platform.libc_ver()[0] != "glibc", reason="malloc_trim is glibc only",
)


def _item(module: object) -> SimpleNamespace:
    return SimpleNamespace(module=module)


@pytest.fixture
def trims(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(_malloc_trim, "_TRIM", lambda pad: calls.append(pad) or 1)
    return calls


def test_a_test_followed_by_one_in_the_same_module_does_not_trim(trims: list[int]) -> None:
    mod = object()
    _malloc_trim.trim_at_module_boundary(_item(mod), _item(mod))
    assert trims == []


def test_the_last_test_of_a_module_trims(trims: list[int]) -> None:
    _malloc_trim.trim_at_module_boundary(_item(object()), _item(object()))
    assert trims == [0]


def test_the_last_test_of_the_session_trims(trims: list[int]) -> None:
    _malloc_trim.trim_at_module_boundary(_item(object()), None)
    assert trims == [0]


def test_no_trim_function_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_malloc_trim, "_TRIM", None)
    _malloc_trim.trim_at_module_boundary(_item(object()), None)


def test_off_linux_there_is_no_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_malloc_trim.sys, "platform", "darwin")
    assert _malloc_trim._load_trim() is None


def test_the_off_switch_means_no_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_malloc_trim.sys, "platform", "linux")
    monkeypatch.setenv("NX_TEST_MALLOC_TRIM", "0")
    monkeypatch.setattr(_malloc_trim.ctypes, "CDLL", lambda _name: pytest.fail("loaded libc despite the off switch"))
    assert _malloc_trim._load_trim() is None


def test_a_libc_that_will_not_load_means_no_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_name: str) -> None:
        raise OSError("musl: no libc.so.6")

    monkeypatch.setattr(_malloc_trim.sys, "platform", "linux")
    monkeypatch.setattr(_malloc_trim.ctypes, "CDLL", refuse)
    assert _malloc_trim._load_trim() is None


def test_a_libc_without_malloc_trim_means_no_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_malloc_trim.sys, "platform", "linux")
    monkeypatch.setattr(_malloc_trim.ctypes, "CDLL", lambda _name: SimpleNamespace())
    assert _malloc_trim._load_trim() is None


_TWO_MODULES = {
    "conftest": """
import os

import pytest
from tests import _malloc_trim

EVENTS = []
# Set for the whole subprocess: a function-scoped monkeypatch is undone before the trim runs.
_malloc_trim._TRIM = lambda pad: EVENTS.append("trim") or 1

@pytest.fixture(scope="module")
def held():
    yield
    EVENTS.append("fixture-teardown")
    if os.environ.get("RAISE_IN_TEARDOWN"):
        raise RuntimeError("teardown failed")

def pytest_sessionfinish(session):
    open("events.txt", "w").write(",".join(EVENTS))
""",
    "test_a": "def test_one(held): pass\ndef test_two(held): pass\n",
    "test_b": "def test_three(): pass\n",
}


def _run_two_modules(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, raise_in_teardown: bool):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    if raise_in_teardown:
        monkeypatch.setenv("RAISE_IN_TEARDOWN", "1")
    pytester.makepyfile(**_TWO_MODULES)
    result = pytester.runpytest_subprocess("-p", "tests._malloc_trim", "-p", "no:randomly", "-p", "no:xdist")
    return result, (pytester.path / "events.txt").read_text().split(",")


def test_the_hook_trims_after_the_module_fixture_is_torn_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, events = _run_two_modules(pytester, monkeypatch, raise_in_teardown=False)
    result.assert_outcomes(passed=3)
    assert events == ["fixture-teardown", "trim", "trim"]


def test_a_failing_teardown_still_errors_and_still_trims(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, events = _run_two_modules(pytester, monkeypatch, raise_in_teardown=True)
    result.assert_outcomes(passed=3, errors=1)
    assert events == ["fixture-teardown", "trim", "trim"]


def test_the_plugin_is_registered(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.has_plugin("tests._malloc_trim")


def _anon_mib() -> int:
    return int(re.search(r"RssAnon:\s+(\d+)", Path("/proc/self/status").read_text()).group(1)) // 1024


@_GLIBC_ONLY
def test_trim_returns_heap_that_free_alone_keeps() -> None:
    """The premise: glibc keeps a fragmented heap's free pages until malloc_trim(0) releases them."""
    assert _malloc_trim._TRIM is not None, "glibc without malloc_trim: the plugin would do nothing here"
    blocks = [bytes(1024) for _ in range(150_000)]
    keep = blocks[::64]
    del blocks
    held = _anon_mib()
    _malloc_trim._TRIM(0)
    released = held - _anon_mib()
    assert keep and released > 60, f"malloc_trim released only {released} MiB of about 145 freed"

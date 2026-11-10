# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests._malloc_trim gives freed heap back to the OS at each module boundary (labrun-pex)."""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest

from tests import _malloc_trim

_LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc malloc_trim is Linux only")


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


def test_the_plugin_is_registered(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.has_plugin("tests._malloc_trim")


def _anon_mib() -> int:
    return int(re.search(r"RssAnon:\s+(\d+)", open("/proc/self/status").read()).group(1)) // 1024


@_LINUX_ONLY
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

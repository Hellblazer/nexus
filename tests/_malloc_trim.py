# SPDX-License-Identifier: AGPL-3.0-or-later
"""Give freed heap back to the OS after each test module (labrun-pex).

A CI shard's xdist worker runs a few hundred modules in one process. glibc keeps
pages a module frees when the heap they sit in is fragmented by objects that live
on; plain ``free`` returns memory only from the top of the heap. Measured on chas
(2026-11-10): after test_scratch.py alone a worker held 1616 MiB anon with 74 MiB
of live Python objects, and ``malloc_trim(0)`` brought it to 345 MiB.
``MALLOC_ARENA_MAX=2`` changed nothing, so it is not per-thread arenas. A trim
cannot lower a module's own peak; it lowers what the other workers hold while one
of them peaks. The shard-level effect is in T2 nexus/labrun-pex-shard-growth-2026-11-10.

So at the end of every module, after its fixtures are torn down, this calls
``malloc_trim(0)``. Linux with glibc only; elsewhere (macOS, Windows, musl) there
is no such call and the plugin does nothing. ``NX_TEST_MALLOC_TRIM=0`` turns it off
for a diagnosis run.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Generator

import pytest


def _load_trim() -> Callable[[int], int] | None:
    if not sys.platform.startswith("linux") or os.environ.get("NX_TEST_MALLOC_TRIM") == "0":
        return None
    try:
        fn = getattr(ctypes.CDLL("libc.so.6"), "malloc_trim", None)
    except OSError:
        return None
    if fn is not None:
        fn.argtypes, fn.restype = [ctypes.c_size_t], ctypes.c_int
    return fn


_TRIM: Callable[[int], int] | None = _load_trim()


def trim_at_module_boundary(item: object, nextitem: object | None) -> None:
    """Trim when *item* is its module's last test (or the session's)."""
    if _TRIM is None:
        return
    if getattr(nextitem, "module", None) is not getattr(item, "module", None):
        _TRIM(0)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    try:
        return (yield)
    finally:
        trim_at_module_boundary(item, nextitem)

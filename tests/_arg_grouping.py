# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regroup pytest's path arguments so each directory's arguments are contiguous.

pytest 9.1.1 does not apply a directory's ``conftest.py`` to tests collected from that directory a
second time, after another directory's argument: ``tests/daemon/a.py tests/b.py tests/daemon/c.py``
runs ``c.py`` without ``tests/daemon/conftest.py``'s autouse fixtures (watchdogs, stubs, isolation).
A hand run over a list of changed files is the usual way to hit it. ``tests/conftest.py`` applies
:func:`group_args_by_directory` in ``pytest_configure``; ``NX_TEST_KEEP_ARG_ORDER=1`` keeps the
order as typed (``tests/test_arg_grouping.py``).
"""
from __future__ import annotations

import os
from pathlib import PurePath


def _directory_parts(arg: str, base: str) -> tuple[str, ...]:
    """The directory an argument names or lives in, as normalised absolute path parts.

    A node id's ``::`` part is dropped, and the path is resolved against *base* (pytest's invocation
    directory) and normalised, so ``tests/daemon/a.py``, ``/abs/repo/tests/daemon/b.py`` and
    ``tests/x/../daemon/c.py`` share one key. An existing directory is its own key, even a dotted one
    (``tests/data.v2``); anything else is keyed by its parent.
    """
    path = os.path.normpath(os.path.join(base, arg.split("::", 1)[0]))
    if os.path.isdir(path) or not PurePath(path).suffix:
        return PurePath(path).parts
    return PurePath(path).parent.parts


def group_args_by_directory(args: list[str], base: str | None = None) -> list[str]:
    """*args* sorted by directory (stable), so a directory's arguments, and every subtree's, are contiguous.

    Arguments in one directory keep their relative order; a list that is already contiguous by this
    order comes back unchanged. *base* defaults to the current directory.
    """
    root = base if base is not None else os.getcwd()
    return sorted(args, key=lambda a: _directory_parts(a, root))

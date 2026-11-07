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

from pathlib import PurePath


def _directory_parts(arg: str) -> tuple[str, ...]:
    """The directory an argument names or lives in, as path parts (a node id's ``::`` part dropped)."""
    path = PurePath(arg.split("::", 1)[0])
    return path.parent.parts if path.suffix else path.parts


def group_args_by_directory(args: list[str]) -> list[str]:
    """*args* sorted by directory (stable), so a directory's arguments, and every subtree's, are contiguous.

    Arguments in one directory keep their relative order; a list that is already contiguous by this
    order comes back unchanged.
    """
    return sorted(args, key=_directory_parts)

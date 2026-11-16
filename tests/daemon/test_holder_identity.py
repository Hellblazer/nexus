# SPDX-License-Identifier: AGPL-3.0-or-later
"""``holder_identity``: the shared pid-recycle re-check fails closed (nexus-f3yqd)."""
from __future__ import annotations

from nexus.daemon.service_registry import holder_identity


def _ident(cmd: str, argv: list[str] | None = None, *, ours_argv: bool | None = None) -> str:
    return holder_identity(
        7,
        is_ours=lambda c: "nexus" in c,
        command=lambda _pid: cmd,
        is_ours_argv=None if ours_argv is None else (lambda _a: ours_argv),
        argv_of=lambda _pid: argv or [],
    )


def test_an_empty_command_line_is_unreadable_never_ours() -> None:
    assert _ident("") == "unreadable"
    assert _ident("", ["nexus"], ours_argv=True) == "unreadable"


def test_a_readable_command_line_decides_by_is_ours() -> None:
    assert _ident("nexus-service --x") == "ours"
    assert _ident("sleep 60") == "foreign"


def test_a_readable_argv_decides_when_an_argv_test_is_given() -> None:
    assert _ident("nexus x", ["nexus", "x"], ours_argv=False) == "foreign"
    assert _ident("sleep 60", ["sleep", "60"], ours_argv=True) == "ours"


def test_an_unreadable_argv_falls_back_to_the_command_line() -> None:
    assert _ident("nexus x", [], ours_argv=False) == "ours"

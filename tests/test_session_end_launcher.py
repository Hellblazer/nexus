# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus._session_end_launcher (nexus-2u7o).

Contract pins:
  * Module top-level must NOT import anything from ``nexus`` — only
    ``os`` and ``sys``. Keeps the fork-first guarantee intact on
    cold-cache Python startup.
  * ``main()`` returns to the caller via the first-fork parent path
    (in the real world ~100ms; here we simulate the fork).
  * ``_run_session_end_synchronously`` calls ``nexus.hooks.session_end``
    and swallows exceptions.
  * Platform-without-fork fallback runs synchronously.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch


def test_module_does_not_import_nexus_submodules_at_top_level() -> None:
    """The whole point of this launcher is to delay nexus imports until
    after the double-fork. If ``nexus._session_end_launcher`` pulls in
    heavyweight submodules at import time, the cold-start race against
    Claude Code's shutdown SIGTERM reopens.
    """
    # Force a fresh import so module-level state is observable.
    for mod in list(sys.modules):
        if mod.startswith("nexus._session_end_launcher"):
            del sys.modules[mod]
    before = set(sys.modules)
    import nexus._session_end_launcher  # noqa: F401
    after = set(sys.modules)
    new = after - before
    heavy = {m for m in new if m.startswith("nexus.") and m != "nexus._session_end_launcher"}
    assert heavy == set(), (
        "nexus._session_end_launcher must not import any nexus submodule "
        f"at top level; got {heavy}"
    )


def test_run_session_end_synchronously_invokes_hooks_session_end() -> None:
    """The grandchild path must dispatch to ``nexus.hooks.session_end``."""
    import nexus._session_end_launcher as launcher

    call_count = 0
    def fake_session_end() -> str:
        nonlocal call_count
        call_count += 1
        return "stub"

    with patch("nexus.hooks.session_end", side_effect=fake_session_end):
        launcher._run_session_end_synchronously()
    assert call_count == 1


def test_run_session_end_synchronously_swallows_exceptions() -> None:
    """The grandchild is detached — exceptions must not propagate up or
    we'd drop the ``os._exit(0)`` and leave a zombie.
    """
    import nexus._session_end_launcher as launcher

    def boom() -> str:
        raise RuntimeError("intentional failure from test")

    with patch("nexus.hooks.session_end", side_effect=boom):
        launcher._run_session_end_synchronously()  # must not raise


def test_main_falls_through_to_sync_when_fork_unavailable() -> None:
    """On platforms without ``os.fork`` (Windows), cleanup must still run
    synchronously rather than be silently skipped.
    """
    import nexus._session_end_launcher as launcher

    calls: list[str] = []
    with (
        patch("nexus._session_end_launcher.hasattr", return_value=False),
        patch.object(launcher, "_run_session_end_synchronously",
                     side_effect=lambda: calls.append("sync")),
        patch.object(launcher, "_daemonize_and_run",
                     side_effect=lambda: calls.append("daemon")),
    ):
        launcher.main()
    assert calls == ["sync"], (
        f"platform without fork must run sync path; got {calls}"
    )


def test_daemonize_parent_path_returns_without_running_cleanup() -> None:
    """The first-fork parent returns immediately so Claude Code sees
    exit 0 in single-digit milliseconds. Cleanup does NOT run in the
    parent — only in the grandchild.
    """
    import nexus._session_end_launcher as launcher

    cleanup_calls: list[None] = []

    def fake_fork():
        # Simulate parent side of first fork (non-zero pid).
        return 12345

    with (
        patch("os.fork", side_effect=fake_fork),
        patch.object(launcher, "_run_session_end_synchronously",
                     side_effect=lambda: cleanup_calls.append(None)),
    ):
        launcher._daemonize_and_run()
    assert cleanup_calls == [], (
        "first-fork parent must return without running cleanup; "
        f"got {len(cleanup_calls)} cleanup calls"
    )


def test_daemonize_falls_through_to_sync_on_oserror() -> None:
    """If ``os.fork`` raises (e.g. fork rate-limit), fall through to
    synchronous cleanup rather than drop it.
    """
    import nexus._session_end_launcher as launcher

    calls: list[str] = []
    with (
        patch("os.fork", side_effect=OSError("fork unavailable")),
        patch.object(launcher, "_run_session_end_synchronously",
                     side_effect=lambda: calls.append("sync")),
    ):
        launcher._daemonize_and_run()
    assert calls == ["sync"]

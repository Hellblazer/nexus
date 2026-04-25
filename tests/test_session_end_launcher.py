# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus._session_end_launcher (nexus-2u7o, RDR-094 Phase C).

Contract pins:
  * Module top-level must NOT import anything from ``nexus`` -- only
    ``os`` and ``sys``. Keeps the fork-first guarantee intact on
    cold-cache Python startup (BANNED invariant per nexus-l828).
  * ``main()`` returns to the caller via the first-fork parent path
    (in the real world ~17ms; here we simulate the fork).
  * ``_run_session_end_synchronously`` calls
    ``nexus.hooks.session_end_flush`` (Phase C swap from
    ``session_end`` to drop the chroma-stop race against
    NEXUS_MCP_OWNS_T1) and swallows exceptions.
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


def test_run_session_end_synchronously_invokes_session_end_flush() -> None:
    """RDR-094 Phase C: grandchild path dispatches to session_end_flush.

    The pre-Phase-C contract was ``session_end`` which both flushes and
    stops chroma. Under NEXUS_MCP_OWNS_T1 (Phase 4) the MCP server owns
    chroma teardown, so the launcher must use the storage-only entry.
    """
    import nexus._session_end_launcher as launcher

    call_count = 0
    def fake_flush() -> str:
        nonlocal call_count
        call_count += 1
        return "stub"

    with patch("nexus.hooks.session_end_flush", side_effect=fake_flush):
        launcher._run_session_end_synchronously()
    assert call_count == 1


def test_run_session_end_synchronously_does_not_call_session_end() -> None:
    """Regression sentinel: launcher must NOT route through session_end.

    session_end runs the chroma-stop block when NEXUS_MCP_OWNS_T1 is
    unset, which races MCP-owned cleanup. The Phase C contract is to
    point the launcher at session_end_flush exclusively; the legacy
    session_end path stays only for nx hook session-end (manual debug)
    and for the flag-off rollout window.
    """
    import nexus._session_end_launcher as launcher

    with (
        patch("nexus.hooks.session_end") as mock_full,
        patch("nexus.hooks.session_end_flush", return_value="stub"),
    ):
        launcher._run_session_end_synchronously()
    mock_full.assert_not_called()


def test_run_session_end_synchronously_swallows_exceptions() -> None:
    """The grandchild is detached -- exceptions must not propagate up or
    we'd drop the ``os._exit(0)`` and leave a zombie.
    """
    import nexus._session_end_launcher as launcher

    def boom() -> str:
        raise RuntimeError("intentional failure from test")

    with patch("nexus.hooks.session_end_flush", side_effect=boom):
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


# ── nx/hooks/hooks.json contract (RDR-094 Phase C / nexus-l828) ─────────────


def _read_plugin_hooks_json() -> dict:
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    return json.loads((repo_root / "nx" / "hooks" / "hooks.json").read_text())


def test_hooks_json_session_end_uses_launcher_not_flush_directly() -> None:
    """The hooks.json SessionEnd entry MUST dispatch through
    nx-session-end-launcher, never directly to ``nx hook session-end-flush``.

    Reason: pointing hooks.json at the Click-routed entry reopens the
    256ms-vs-2s cold-start race that the launcher exists to solve
    (Click parses argv + nexus.hooks imports BEFORE os.fork). The
    launcher's __main__ block forks first using only stdlib, then
    imports nexus.hooks in the grandchild.
    """
    cfg = _read_plugin_hooks_json()
    session_end = cfg["hooks"]["SessionEnd"]
    assert session_end, "SessionEnd block must not be empty"

    commands = [
        h["command"]
        for entry in session_end
        for h in entry.get("hooks", [])
    ]
    assert any("nx-session-end-launcher" in c for c in commands), (
        f"SessionEnd must invoke nx-session-end-launcher; got {commands!r}"
    )
    # The Click entry stays as a manual-debug entry point only -- it
    # must NOT appear in hooks.json.
    assert not any(
        "nx hook session-end-flush" in c or "nx hook session-end" in c
        for c in commands
    ), (
        "SessionEnd in hooks.json must not invoke 'nx hook session-end*' "
        "directly -- the launcher is the only fork-first-safe entry. "
        f"Got: {commands!r}"
    )


def test_hooks_json_session_end_timeout_is_three_seconds() -> None:
    """Phase C reduces SessionEnd timeout from 5s to 3s.

    Storage-only flush is sub-second on a typical install; the previous
    5s window covered chroma teardown which the launcher no longer
    triggers. Tighter timeout means a wedged hook is reaped faster.
    """
    cfg = _read_plugin_hooks_json()
    timeouts = [
        h["timeout"]
        for entry in cfg["hooks"]["SessionEnd"]
        for h in entry.get("hooks", [])
    ]
    assert timeouts == [3], f"expected SessionEnd timeout 3s; got {timeouts!r}"


def test_hooks_json_session_end_drops_detach_fallback() -> None:
    """Phase C drops the ``|| nx hook session-end-detach`` fallback.

    The detach Click command imports nexus.hooks before forking and
    has the same 2s cold-start race the launcher fixes. Falling back
    to it on launcher failure was a footgun: a slow launcher that
    reached the inner fork would still skip the SIGTERM -- the
    fallback runs against the same race.
    """
    cfg = _read_plugin_hooks_json()
    commands = [
        h["command"]
        for entry in cfg["hooks"]["SessionEnd"]
        for h in entry.get("hooks", [])
    ]
    assert not any("session-end-detach" in c for c in commands), (
        f"detach fallback must be removed from SessionEnd; got {commands!r}"
    )

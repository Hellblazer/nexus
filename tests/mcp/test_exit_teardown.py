# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The nx-mcp exit teardown (nexus-tu84w, reshaped at RDR-253 Phase 2, nexus-hczdo.2.1).

It used to release the session's ``directory/<name>`` row alongside the T1 teardown; the name lease is gone, so the
teardown is the identity lease's shutdown beside :func:`nexus.mcp.core._t1_shutdown`, started first and awaited last so a
slow identity shutdown never delays the data-bearing T1 steps, and so a SIGTERM, a SIGHUP and a clean exit all run the
same path.
"""
from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from nexus import mcp_infra
from nexus.mcp import core
from tests._module_seam import setattr_in


class _Exit(Exception):
    pass


@pytest.fixture
def order(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(core.identity_seam, "start_shutdown", lambda: seen.append("identity_start"))
    monkeypatch.setattr(core.identity_seam, "await_shutdown", lambda: seen.append("identity_await"))
    monkeypatch.setattr(core, "_t1_shutdown", lambda: seen.append("t1_shutdown"))
    monkeypatch.setattr(core, "_remove_aura_state_files", lambda: seen.append("aura_state"))
    return seen


def _sigterm_stubs(monkeypatch, order: list[str]) -> None:
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_flush_search_telemetry_at_exit", lambda: None)

    def _fake_exit(code: int) -> None:
        order.append(f"exit {code}")
        raise _Exit

    setattr_in(monkeypatch, core, "_os._exit", _fake_exit)


def test_the_teardown_starts_the_identity_shutdown_first_and_awaits_it_last(order) -> None:
    core._exit_teardown()
    assert order == ["identity_start", "t1_shutdown", "aura_state", "identity_await"]


def test_sigterm_runs_the_whole_teardown_then_exits(monkeypatch, order) -> None:
    _sigterm_stubs(monkeypatch, order)
    with pytest.raises(_Exit):
        core._sigterm_handler(15, None)
    assert order == ["identity_start", "t1_shutdown", "aura_state", "identity_await", "exit 0"]


def test_sigterm_while_the_lifespan_is_already_tearing_down_interferes_with_nothing(monkeypatch, order) -> None:
    _sigterm_stubs(monkeypatch, order)
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", True)
    core._sigterm_handler(15, None)
    assert order == []


def test_the_exit_handlers_register_the_teardown_with_atexit(monkeypatch, order) -> None:
    registered: list = []
    signals: dict = {}
    core._install_exit_handlers(
        register=registered.append, set_signal=lambda sig, fn: signals.__setitem__(sig, fn),
        get_signal=lambda _sig: None,
    )

    assert registered == [core._exit_teardown]
    # and main() installs them: the pre-fix main registered _t1_shutdown inline
    assert "_install_exit_handlers()" in inspect.getsource(core.main)
    import signal  # noqa: PLC0415 — test-local

    expected = {signal.SIGTERM: core._sigterm_handler, signal.SIGINT: core._sigterm_handler}
    if hasattr(signal, "SIGHUP"):  # a terminal hangup (tmux kill-server) gets the same teardown (nexus-xwijj)
        expected[signal.SIGHUP] = core._sigterm_handler
    assert signals == expected

    registered[0]()
    assert order == ["identity_start", "t1_shutdown", "aura_state", "identity_await"]


@pytest.mark.t2_tenant
def test_the_lifespan_exit_runs_the_identity_shutdown(monkeypatch, order) -> None:
    """The normal stdio exit: stdin EOF ends the lifespan's ``async with``."""
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    monkeypatch.setattr(core, "_BORROWED_T1_SESSION", {})
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    sid = os.environ["NX_T1_SESSION_ID"]
    # the resolver's name differs across RDR-253 Phase 2 steps (resolve_active_session_id, then resolve_session_id)
    for name in ("resolve_active_session_id", "resolve_session_id"):
        monkeypatch.setattr(f"nexus.session.{name}", lambda *_a, **_k: sid, raising=False)
    mcp_infra.reset_t1_for_release()

    async def _run() -> None:
        async with core._t1_lifespan(None):
            order.append("serving")

    try:
        asyncio.run(_run())
    finally:
        mcp_infra.reset_t1_for_release()

    assert order[0] == "serving"
    assert order.count("identity_start") >= 1 and order[-1] == "identity_await"


def test_an_inherited_sighup_ignore_is_kept() -> None:
    """A session started under nohup ignores SIGHUP; its MCP server keeps the ignore and outlives a
    hangup with it, instead of dying alone (nexus-xwijj)."""
    import signal  # noqa: PLC0415 — test-local

    if not hasattr(signal, "SIGHUP"):
        pytest.skip("no SIGHUP on this platform")
    signals: dict = {}
    core._install_exit_handlers(
        register=lambda _fn: None, set_signal=lambda sig, fn: signals.__setitem__(sig, fn),
        get_signal=lambda sig: signal.SIG_IGN if sig == signal.SIGHUP else signal.SIG_DFL,
    )
    assert signal.SIGHUP not in signals
    assert signals == {signal.SIGTERM: core._sigterm_handler, signal.SIGINT: core._sigterm_handler}

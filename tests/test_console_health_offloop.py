# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console health routes collect their data off the event loop (nexus-jypmj.21).

``_collect_health_data`` runs ``run_health_checks()``, whose rows spawn processes (``claude --version``) and make
HTTP calls. Called straight from an ``async def`` handler, one hung probe froze every console request. The handlers
now run the collector on a worker thread. The collector is replaced by a recorder here, so the test needs no
engine: it pins WHERE the collector runs, not what it collects.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.console.routes import health as health_mod


class _Templates:
    def TemplateResponse(self, request: Any, name: str, context: dict) -> tuple[str, dict]:  # noqa: N802, ARG002 - starlette's name
        return name, context


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(templates=_Templates())))


def _recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def collect() -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False
        return {"health_checks": []}

    monkeypatch.setattr(health_mod, "_collect_health_data", collect)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("handler, template", [
    (health_mod.health_index, "health/index.html"),
    (health_mod.health_refresh, "health/_cards.html"),
])
async def test_the_collector_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch, handler: Any, template: str) -> None:
    seen = _recorder(monkeypatch)
    name, context = await handler(_request(), scope="project")
    assert name == template and context["health_checks"] == []
    assert seen["on_event_loop"] is False, "the blocking collector ran on the event loop thread"


@pytest.mark.asyncio
async def test_a_hung_probe_does_not_stall_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collector blocks until released; meanwhile the loop keeps running other coroutines."""
    release = threading.Event()
    entered = threading.Event()

    def hung() -> dict[str, Any]:
        entered.set()
        assert release.wait(10), "the test never released the collector"
        return {"health_checks": []}

    monkeypatch.setattr(health_mod, "_collect_health_data", hung)
    task = asyncio.ensure_future(health_mod.health_refresh(_request(), scope="project"))
    ticks = 0
    while not entered.is_set():
        await asyncio.sleep(0.01)
    for _ in range(5):
        await asyncio.sleep(0.01)  # the loop is free while the collector is blocked
        ticks += 1
    assert not task.done() and ticks == 5
    release.set()
    await task

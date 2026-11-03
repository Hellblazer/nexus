# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mcp 2.x surface the servers reach into, driven through a real client (nexus-sgmyl).

``src/nexus/mcp`` uses four SDK surfaces that 2.x keeps provisional or private:
``Server.middleware`` (the tool-call log, the stale-host check),
``MCPServer._lowlevel_server.instructions`` (the first-run, embedder and setup
notices), ``Server.add_notification_handler`` (the channel's ``initialized``
gate) and ``create_initialization_options(experimental_capabilities=...)`` (the
``claude/channel`` capability). Each has a unit test of its own against a
stand-in; this file connects one real ``MCPServer`` to ``mcp.client.Client`` and
checks that all four take effect on the wire, so an SDK minor that moves one
fails here by name. ``run_stdio_with_channel`` is also run in a subprocess over
real stdio, the way ``nx-mcp`` runs it.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import textwrap
import threading
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
import structlog
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from structlog.testing import capture_logs

from nexus.mcp import _tool_call_log, channel
from nexus.mcp._tool_call_log import install_tool_call_log


class _ChannelTransport:
    """In-memory transport that starts the server as ``run_stdio_with_channel`` does.

    ``InMemoryTransport`` calls ``create_initialization_options()`` with no
    arguments, so it cannot carry the channel capability; this passes it.
    """

    def __init__(self, server: MCPServer) -> None:
        self._server = server._lowlevel_server  # noqa: SLF001 — what the SDK's own transport and channel.py use

    @asynccontextmanager
    async def _connect(self):  # type: ignore[no-untyped-def]
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: self._server.run(
                        server_read,
                        server_write,
                        self._server.create_initialization_options(
                            experimental_capabilities=channel.CHANNEL_CAPABILITY,
                        ),
                    )
                )
                try:
                    yield client_read, client_write
                finally:
                    tg.cancel_scope.cancel()

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        self._cm = self._connect()
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any) -> bool | None:
        return await self._cm.__aexit__(*exc)


def test_the_sdk_surfaces_the_servers_reach_into_take_effect_through_a_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The suite filters structlog at WARNING and the event is INFO/DEBUG;
    # conftest's autouse fixture restores the config after the test.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    _tool_call_log._reset_state()
    server = MCPServer("contract-probe")
    methods_seen: list[str] = []
    initialized = {"event": None}

    @server.tool(structured_output=False)
    def echo(text: str) -> str:
        return text

    # 1. Server.middleware: the production tool-call log, plus a recorder that
    #    shows which methods the chain sees and that it is not only tools/call.
    assert install_tool_call_log(server) == "applied"

    async def record(ctx: Any, call_next: Any) -> Any:
        methods_seen.append(ctx.method)
        return await call_next(ctx)

    server.middleware.append(record)

    # 2. instructions, written through the low-level server as _first_run does.
    server._lowlevel_server.instructions = "contract instructions"  # noqa: SLF001

    # 3. add_notification_handler, wired by channel.py exactly as in production.
    async def drive() -> tuple[Any, str | None, Any]:
        event = anyio.Event()

        async def on_initialized(_ctx: Any, _params: Any) -> None:
            event.set()

        monkeypatch.setattr(channel, "_on_initialized", on_initialized)
        channel.install_initialized_gate(server._lowlevel_server)  # noqa: SLF001
        initialized["event"] = event
        async with Client(_ChannelTransport(server), mode="legacy") as client:  # type: ignore[arg-type]
            with anyio.fail_after(5):
                await event.wait()
            result = await client.call_tool("echo", {"text": "hi"}, meta={"probeKey": "v"})
            return client.server_capabilities, client.instructions, result

    with capture_logs() as logs:
        capabilities, instructions, result = anyio.run(drive)

    # tools/call went through the chain, and the tool ran.
    assert "tools/call" in methods_seen, methods_seen
    assert result.is_error is False and result.content[0].text == "hi"
    calls = [e for e in logs if e["event"] == "mcp_tool_call"]
    assert len(calls) == 1 and calls[0]["tool"] == "echo", logs
    assert "probeKey" in str(calls[0]), calls[0]
    assert "hi" not in str(calls[0]), "the event must carry keys only, never an argument"
    # instructions mutated on the low-level server reach the initialize result.
    assert instructions == "contract instructions"
    # create_initialization_options(experimental_capabilities=...) reaches the wire.
    assert capabilities.experimental == {"claude/channel": {}}
    # notifications/initialized reached the handler channel.py registers.
    assert initialized["event"].is_set()


# ── run_stdio_with_channel over real stdio, in a subprocess ─────────────────

_SERVER_SCRIPT = textwrap.dedent(
    '''
    import anyio
    from mcp.server.mcpserver import MCPServer
    from nexus.mcp import _tool_call_log, channel

    server = MCPServer("stdio-contract")

    @server.tool(structured_output=False)
    async def notify() -> str:
        sent = await channel.send_channel_notification("contract message", {"kind": "probe"})
        return "sent" if sent else "not-sent"

    anyio.run(channel.run_stdio_with_channel, server)
    '''
)


def test_run_stdio_with_channel_declares_the_capability_and_delivers_a_notification(
    tmp_path,
) -> None:
    """Real stdio, newline-delimited JSON-RPC: initialize, initialized, one tools/call.

    The notification is read raw because the SDK's typed client has no member for
    ``notifications/claude/channel``.
    """
    script = tmp_path / "stdio_server.py"
    script.write_text(_SERVER_SCRIPT, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in {"NX_SERVICE_URL", "NEXUS_CONFIG_DIR"}}
    env.update(HOME=str(home), USERPROFILE=str(home), NEXUS_CONFIG_DIR=str(home / "cfg"), NX_NO_TELEMETRY="1")

    proc = subprocess.Popen(  # noqa: S603 — the interpreter running this test, a script this test wrote
        [sys.executable, "-I", str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=str(tmp_path),
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        proc.stdin.flush()

    def recv(timeout: float = 30.0) -> dict[str, Any]:
        item = lines.get(timeout=timeout)
        assert item is not None, "server closed stdout"
        return json.loads(item)

    try:
        send({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "contract", "version": "1"},
        }})
        init = recv()
        assert init["id"] == 1, init
        assert init["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
        send({"method": "notifications/initialized"})
        send({"id": 2, "method": "tools/call", "params": {"name": "notify", "arguments": {}}})
        seen: list[dict[str, Any]] = [recv(), recv()]
    except queue.Empty:
        proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"no reply from the stdio server; stderr tail: {err[-800:]}") from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()

    notes = [m for m in seen if m.get("method") == "notifications/claude/channel"]
    replies = [m for m in seen if m.get("id") == 2]
    assert len(notes) == 1 and len(replies) == 1, seen
    assert notes[0]["params"] == {"content": "contract message", "meta": {"kind": "probe"}}
    assert replies[0]["result"]["content"][0]["text"] == "sent"

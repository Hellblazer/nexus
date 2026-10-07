# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every MCP server the plugins ship starts on native Windows with no Node.js.

nexus-f9bgu: on a clean Windows 11 guest (Claude Code 2.1.292), sn's context7
and conexus's sequential-thinking failed to connect in every session. Both
were declared ``"command": "npx"``. A clean Windows box has no Node.js, and
even with Node installed ``npx`` there is ``npx.cmd``, which an exec-form
spawn cannot start; the usual fix, ``cmd /c npx``, cannot be written in an
.mcp.json that macOS and Linux read too. The fix removed Node from the plugin
surface: context7 is the vendor's hosted HTTP endpoint (same two tools), and
sequential-thinking is a standard-library Python port started through uv,
which conexus already requires.

These tests pin that shape for both plugins and run the shipped server
through the exact argv its .mcp.json declares.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_JSONS = {
    "conexus": REPO_ROOT / "conexus" / ".mcp.json",
    "sn": REPO_ROOT / "sn" / ".mcp.json",
}
SEQ_SCRIPT = REPO_ROOT / "conexus" / "mcp" / "sequential_thinking.py"

#: Commands a stdio server may name. Each is a real executable on Windows
#: (uv.exe, uvx.exe, and the nx console-script .exe shims), so Claude Code
#: can spawn it without a shell. npx/npm/node need Node.js and are .cmd
#: shims on Windows; cmd/sh/bash are platform-specific.
WINDOWS_SAFE_COMMANDS = frozenset({"uv", "uvx", "nx-mcp", "nx-mcp-catalog"})

#: The launch argv for sequential-thinking: the hardened uv form the hooks use
#: (a tool environment never consults a project's or an ancestor's .venv, and
#: --no-config keeps a user's uv.toml from changing the launch).
SEQ_ARGS = [
    "tool",
    "run",
    "--directory",
    "${CLAUDE_PLUGIN_ROOT}",
    "--no-config",
    "--quiet",
    "--python",
    ">=3.12",
    "python",
    "${CLAUDE_PLUGIN_ROOT}/mcp/sequential_thinking.py",
]

CONTEXT7_URL = "https://mcp.context7.com/mcp"

#: Upstream @modelcontextprotocol/server-sequential-thinking's tool contract
#: (src/sequentialthinking/index.ts). Skills and agents cite the tool by name
#: and pass these parameters, so the port must keep them.
UPSTREAM_PROPERTIES = {
    "thought",
    "nextThoughtNeeded",
    "thoughtNumber",
    "totalThoughts",
    "isRevision",
    "revisesThought",
    "branchFromThought",
    "branchId",
    "needsMoreThoughts",
}
UPSTREAM_REQUIRED = {"thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"}


def _servers() -> list[tuple[str, str, dict]]:
    out = []
    for plugin, path in MCP_JSONS.items():
        for name, cfg in json.loads(path.read_text()).items():
            out.append((plugin, name, cfg))
    return out


def test_no_server_needs_node_or_a_shell() -> None:
    offenders = []
    for plugin, name, cfg in _servers():
        kind = cfg.get("type", "stdio")
        if kind == "stdio":
            if cfg.get("command") not in WINDOWS_SAFE_COMMANDS:
                offenders.append(f"{plugin}/{name}: command {cfg.get('command')!r}")
        elif kind != "http":
            offenders.append(f"{plugin}/{name}: type {kind!r}")
    assert not offenders, (
        "an MCP server would not start on a clean Windows box (nexus-f9bgu): "
        f"{offenders}; allowed stdio commands are {sorted(WINDOWS_SAFE_COMMANDS)}"
    )


@pytest.mark.parametrize("command", ["npx", "cmd", "node"])
def test_the_shape_check_rejects_the_old_forms(command: str) -> None:
    """Non-vacuity: the predicate above refuses what shipped before the fix."""
    assert command not in WINDOWS_SAFE_COMMANDS


def test_context7_is_the_hosted_http_endpoint() -> None:
    cfg = json.loads(MCP_JSONS["sn"].read_text())["context7"]
    assert cfg == {"type": "http", "url": CONTEXT7_URL, "alwaysLoad": True}


def test_sequential_thinking_launch_argv() -> None:
    cfg = json.loads(MCP_JSONS["conexus"].read_text())["sequential-thinking"]
    assert cfg["command"] == "uv"
    assert cfg["args"] == SEQ_ARGS
    assert SEQ_SCRIPT.is_file()


def _rpc(proc: subprocess.Popen, message: dict) -> dict | None:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    if "id" not in message:
        return None
    return json.loads(proc.stdout.readline())


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is a conexus prerequisite")
def test_sequential_thinking_serves_through_its_declared_argv(tmp_path: Path) -> None:
    """Spawn the server exactly as .mcp.json says (no shell), from an unrelated
    cwd, and drive one MCP session: initialize, list, two calls."""
    plugin_root = str(REPO_ROOT / "conexus")
    argv = ["uv"] + [a.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root) for a in SEQ_ARGS]
    proc = subprocess.Popen(
        argv,
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            },
        )
        assert init and init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"]["tools"] is not None
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools is not None
        [tool] = tools["result"]["tools"]
        assert tool["name"] == "sequentialthinking"
        assert set(tool["inputSchema"]["properties"]) == UPSTREAM_PROPERTIES
        assert set(tool["inputSchema"]["required"]) == UPSTREAM_REQUIRED

        first = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sequentialthinking",
                    "arguments": {"thought": "a", "nextThoughtNeeded": True, "thoughtNumber": 3, "totalThoughts": 2},
                },
            },
        )
        assert first is not None
        assert first["result"]["structuredContent"] == {
            "thoughtNumber": 3,
            "totalThoughts": 3,  # raised to thoughtNumber, as upstream does
            "nextThoughtNeeded": True,
            "branches": [],
            "thoughtHistoryLength": 1,
        }
        assert json.loads(first["result"]["content"][0]["text"]) == first["result"]["structuredContent"]

        second = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "sequentialthinking",
                    "arguments": {
                        "thought": "b",
                        "nextThoughtNeeded": "false",
                        "thoughtNumber": "4",
                        "totalThoughts": 4,
                        "branchFromThought": 2,
                        "branchId": "alt",
                    },
                },
            },
        )
        assert second is not None
        payload = second["result"]["structuredContent"]
        assert payload["nextThoughtNeeded"] is False
        assert payload["branches"] == ["alt"]
        assert payload["thoughtHistoryLength"] == 2

        bad = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "sequentialthinking",
                    "arguments": {"thought": "c", "nextThoughtNeeded": "maybe", "thoughtNumber": 1, "totalThoughts": 1},
                },
            },
        )
        assert bad is not None and bad["result"]["isError"] is True
        assert "nextThoughtNeeded" in bad["result"]["content"][0]["text"]

        unknown = _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        assert unknown is not None and unknown["error"]["code"] == -32601
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        rc = proc.wait(timeout=30)
    stderr = proc.stderr.read() if proc.stderr else ""
    assert rc == 0, stderr
    # Claude Code logs each stderr line of a stdio server as an error entry.
    assert stderr == ""

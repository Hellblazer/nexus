# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/launch/nx_standin.py: the servers the launcher serves when there is no
CLI (RDR-228 Phase 1 Step 2, nexus-sv2wx.6).

With ``nx-mcp`` absent the application still gets a connected server, and the
model is told the truth: Nexus is not installed, and these commands install it.
In this phase the stand-in installs nothing. The cases drive the launcher as a
real stdio server with the MCP SDK's own client, the way a Claude application
starts it. Every child environment sets ``NX_TOOLS_DIR`` and an empty ``PATH``
directory, so the developer's own install is not found.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests._platform import scrubbed_env

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCH = _ROOT / "conexus" / "launch" / "nx_launch.py"
_STANDIN = _ROOT / "conexus" / "launch" / "nx_standin.py"

#: The first install on THIS platform, as README.md gives it (Linux names the CPU
#: build of torch).
_FIRST = "uv tool install conexus --python 3.12" + (" --torch-backend cpu" if sys.platform.startswith("linux") else "")
_MANUAL_COMMANDS = (_FIRST, "nx self install", "nx init")
_INSTALL_PAGE = "https://hellblazer.github.io/nexus/"


def _standin():
    spec = importlib.util.spec_from_file_location("nx_standin_under_test", _STANDIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _isolated(tmp_path: Path) -> dict[str, Path]:
    """An isolated home: empty tools root, empty ``PATH`` directory, empty config."""
    dirs = {name: tmp_path / name for name in ("home", "tools", "bin", "config", "project")}
    for d in dirs.values():
        d.mkdir()
    return dirs


def _env(dirs: dict[str, Path]) -> dict[str, str]:
    return scrubbed_env(
        PATH=str(dirs["bin"]),
        NX_TOOLS_DIR=str(dirs["tools"]),
        NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]),
        USERPROFILE=str(dirs["home"]),
        CLAUDE_PROJECT_DIR=str(dirs["project"]),
    )


def _tree(dirs: dict[str, Path]) -> list[str]:
    """Every path under the isolated directories, for a before/after comparison."""
    return sorted(str(p.relative_to(base.parent)) for base in dirs.values() for p in base.rglob("*"))


async def _session(dirs: dict[str, Path], program: str):
    """Start the launcher for *program* as a stdio server and return what an SDK
    client learns from it: (initialize result, tool list, call results)."""
    from mcp import ClientSession  # noqa: PLC0415 -- the SDK is a test dependency only
    from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: PLC0415

    params = StdioServerParameters(
        command=sys.executable, args=[str(_LAUNCH), program], env=_env(dirs), cwd=str(dirs["project"]),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            calls = {tool.name: await session.call_tool(tool.name, {}) for tool in tools}
            return init, tools, calls


def _not_json(constant: str) -> float:
    raise ValueError(f"{constant} is not JSON")


def _strict_loads(line: str):
    """``json.loads`` as a client reads a reply: ``NaN`` and ``Infinity`` are
    Python's extension, not JSON, and a strict parser drops the line."""
    return json.loads(line, parse_constant=_not_json)


def _text(result) -> str:
    return "".join(block.text for block in result.content if getattr(block, "type", "") == "text")


@pytest.mark.asyncio
async def test_no_cli_and_nx_mcp_is_a_stand_in_with_exactly_the_two_setup_tools(tmp_path: Path) -> None:
    dirs = _isolated(tmp_path)
    init, tools, _calls = await _session(dirs, "nx-mcp")
    assert sorted(tool.name for tool in tools) == ["setup", "setup_status"]
    for tool in tools:
        assert tool.inputSchema.get("properties") in ({}, None), f"{tool.name} takes an argument in Phase 1"
        assert "not installed" in (tool.description or ""), tool.name
        assert tool.annotations is None, f"{tool.name}: a hint given now would outlive Phase 1"
    setup = next(tool for tool in tools if tool.name == "setup")
    assert "installs nothing" in (setup.description or "")
    assert "Do not run them yourself" in (setup.description or "")
    assert init.serverInfo.name == "nexus-not-installed"


@pytest.mark.asyncio
async def test_the_instructions_say_nexus_is_not_installed_and_name_the_manual_commands(tmp_path: Path) -> None:
    init, _tools, _calls = await _session(_isolated(tmp_path), "nx-mcp")
    text = init.instructions or ""
    assert "Nexus is not installed" in text
    for command in _MANUAL_COMMANDS:
        assert command in text, command
    assert "/conexus:setup" not in text


@pytest.mark.asyncio
async def test_setup_answers_with_the_manual_commands_and_starts_nothing(tmp_path: Path) -> None:
    """Phase 1 installs nothing. ``setup`` is text only: no process, and nothing
    written under the config directory, the tools directory or the home."""
    dirs = _isolated(tmp_path)
    before = _tree(dirs)
    _init, _tools, calls = await _session(dirs, "nx-mcp")
    setup = _text(calls["setup"])
    for command in _MANUAL_COMMANDS:
        assert command in setup, command
    assert _INSTALL_PAGE in setup
    assert "/conexus:setup" not in setup
    assert "do not run them yourself unless the user asks" in setup, "the result must leave the choice with the user"
    assert not calls["setup"].isError
    assert "not installed" in _text(calls["setup_status"])
    assert "/conexus:setup" not in _text(calls["setup_status"])
    assert _tree(dirs) == before, "the stand-in wrote something"


@pytest.mark.asyncio
async def test_no_cli_and_nx_mcp_catalog_is_a_stand_in_with_no_tools(tmp_path: Path) -> None:
    """The application shows a connected server, and the question is not offered
    twice: no tools and no instructions."""
    init, tools, _calls = await _session(_isolated(tmp_path), "nx-mcp-catalog")
    assert tools == []
    assert not init.instructions


def test_the_commands_are_the_platform_s_own_and_the_instructions_are_one_sentence() -> None:
    """README.md: on Linux the first command names the CPU build of torch, and
    without it uv installs 4.3 GB that Nexus never uses."""
    mod = _standin()
    assert mod.manual_commands("linux")[0] == "uv tool install conexus --python 3.12 --torch-backend cpu"
    for platform in ("darwin", "win32"):
        assert mod.manual_commands(platform)[0] == "uv tool install conexus --python 3.12"
    for platform in ("linux", "darwin", "win32"):
        assert mod.manual_commands(platform)[1:] == ("nx self install", "nx init")
        text = mod.instructions(platform)
        assert text.endswith(".") and ". " not in text, f"more than one sentence: {text}"
        for command in mod.manual_commands(platform):
            assert command in text and command in mod.setup_text(platform)


def test_linux_is_told_which_uv_the_first_command_needs() -> None:
    """README.md: `--torch-backend` needs uv 0.9.19 or newer."""
    mod = _standin()
    assert "uv 0.9.19 or newer" in mod.setup_text("linux") and "uv self update" in mod.setup_text("linux")
    for platform in ("darwin", "win32"):
        assert "0.9.19" not in mod.setup_text(platform)


def test_serving_the_stand_in_leaves_no_bytecode_in_the_plugin(tmp_path: Path) -> None:
    """The launcher imports its siblings from the installed plugin's directories;
    it must not write a cache there."""
    import shutil

    for sub in ("hooks/scripts", "launch"):
        shutil.copytree(_ROOT / "conexus" / sub, tmp_path / "plugin" / sub, ignore=shutil.ignore_patterns("__pycache__"))
    before = sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "plugin").rglob("*"))
    dirs = _isolated(tmp_path)
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
    for argv in (["nx-mcp"], ["nx-hook", "preflight"]):
        r = subprocess.run(
            [sys.executable, str(tmp_path / "plugin" / "launch" / "nx_launch.py"), *argv],
            input=ping, capture_output=True, env=_env(dirs), timeout=30, check=False,
        )
        assert r.returncode == 0, r.stderr
    assert sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "plugin").rglob("*")) == before


def test_the_install_page_is_the_site_s_install_guide() -> None:
    assert _standin().INSTALL_PAGE == _INSTALL_PAGE
    assert "<title>Installing Nexus</title>" in (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def test_no_text_the_stand_in_carries_names_the_phase_2_command() -> None:
    """nexus-j8ebc item 2: ``/conexus:setup`` ships in Phase 2."""
    assert "/conexus:setup" not in _STANDIN.read_text(encoding="utf-8")


def test_the_stand_in_never_writes_to_standard_error(tmp_path: Path) -> None:
    """Claude Code records every stderr line of a stdio server as an error in its
    MCP log. Driven raw: a handshake, a tool call, an unknown method, bad JSON."""
    dirs = _isolated(tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "setup", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    ]
    payload = ("\n".join(json.dumps(line) for line in lines) + "\nnot json\n").encode()
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        input=payload, capture_output=True, env=_env(dirs), timeout=30, check=False,
    )
    assert r.returncode == 0
    assert r.stderr == b""
    replies = [_strict_loads(line) for line in r.stdout.decode("utf-8").splitlines()]
    assert [reply.get("id") for reply in replies] == [1, 2, 3, 4, None]
    assert "error" in replies[2], "a Nexus tool name must not be served by the stand-in"
    assert "error" in replies[3]
    assert b"\r\n" not in r.stdout, "MCP stdio is newline-delimited, with bare newlines"


_HOSTILE = {
    "bytes that are not UTF-8": b'{"jsonrpc":"2.0","id":1,"method":"pi\xffng"}\n',
    "nesting past the recursion limit": b"[" * 200_000 + b"\n",
    "an integer too long to parse": b'{"jsonrpc":"2.0","id":' + b"9" * 5000 + b',"method":"ping"}\n',
    "a lone surrogate in the id": b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n',
    "an id that is not JSON": b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
}


@pytest.mark.parametrize("line", list(_HOSTILE.values()), ids=list(_HOSTILE))
def test_hostile_input_gets_a_reply_and_the_server_lives(tmp_path: Path, line: bytes) -> None:
    """Each of these ended the server with a traceback on standard error (code
    review of 973e7c470). The server must answer and go on to the next request."""
    after = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode() + b"\n"
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        input=line + after, capture_output=True, env=_env(_isolated(tmp_path)), timeout=60, check=False,
    )
    assert r.returncode == 0
    assert r.stderr == b""
    replies = [_strict_loads(reply) for reply in r.stdout.decode("utf-8").splitlines()]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies
    assert len(replies) == 2, replies


def test_a_client_that_stops_reading_ends_the_server_quietly(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(_isolated(tmp_path)),
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
    try:
        proc.stdin.write(ping * 2000)
        proc.stdin.close()
    except OSError:
        pass  # the server left before reading it all, which is the point
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == b""


@pytest.mark.asyncio
async def test_a_tool_call_to_the_catalog_stand_in_is_an_error(tmp_path: Path) -> None:
    from mcp import ClientSession, McpError  # noqa: PLC0415
    from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: PLC0415

    dirs = _isolated(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=[str(_LAUNCH), "nx-mcp-catalog"], env=_env(dirs))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.protocolVersion
            with pytest.raises(McpError):
                await session.call_tool("setup", {})


@pytest.mark.parametrize("script", [_STANDIN], ids=lambda p: p.name)
def test_it_imports_only_the_standard_library(script: Path) -> None:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            found.add((node.module or "").split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__"}, found


def test_the_stand_in_serves_with_nexus_unimportable(tmp_path: Path) -> None:
    dirs = _isolated(tmp_path)
    program = (
        "import runpy, sys\n"
        "sys.modules['nexus'] = None\n"
        f"sys.argv = [{str(_LAUNCH)!r}, 'nx-mcp']\n"
        f"runpy.run_path({str(_LAUNCH)!r}, run_name='__main__')\n"
    )
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n"
    r = subprocess.run(
        [sys.executable, "-c", program], input=request, capture_output=True, env=_env(dirs), timeout=30, check=False,
    )
    assert r.returncode == 0, r.stderr
    names = [tool["name"] for tool in json.loads(r.stdout)["result"]["tools"]]
    assert sorted(names) == ["setup", "setup_status"]


def test_a_cli_that_resolves_is_run_and_the_stand_in_is_not(tmp_path: Path) -> None:
    """What is served is decided from one fact. With ``nx-mcp`` on ``PATH`` the
    launcher runs it, whatever else is true."""
    dirs = _isolated(tmp_path)
    if os.name == "nt":
        (dirs["bin"] / "nx-mcp.cmd").write_text("@echo real\r\n")
    else:
        fake = dirs["bin"] / "nx-mcp"
        fake.write_text("#!/bin/sh\necho real\n")
        fake.chmod(0o755)
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        input=b"", capture_output=True, env=_env(dirs), timeout=30, check=False,
    )
    assert r.stdout.strip() == b"real"

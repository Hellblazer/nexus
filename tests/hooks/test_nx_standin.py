# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/launch/nx_standin.py: the servers the launcher serves when there is no
CLI (RDR-228 Phase 1 Step 2, nexus-sv2wx.6).

With ``nx-mcp`` absent the application still gets a connected server, and the
model is told the truth: Nexus is not installed, and the ``setup`` tool installs
it (Phase 2 Step 3, nexus-sv2wx.16; Phase 1 only listed the commands). The cases
drive the launcher as a real stdio server with the MCP SDK's own client, the way a
Claude application starts it. Every child environment sets ``NX_TOOLS_DIR`` and an
empty ``PATH`` directory, so the developer's own install is not found. A case that
would start a real build runs a copy of the plugin whose ``nx_setup.py`` is the
recording script of ``tests/_setup_recorder.py``.
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
from tests._platform import WINDOWS_SHEBANG, install_windows_exe_fake, scrubbed_env
from tests._setup_recorder import Recorder

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


async def _session(dirs: dict[str, Path], program: str, launch: Path = _LAUNCH, calls: tuple[tuple[str, dict], ...] = ()):
    """Start the launcher for *program* as a stdio server and return what an SDK
    client learns from it: (initialize result, tool list, results of *calls*)."""
    from mcp import ClientSession  # noqa: PLC0415 -- the SDK is a test dependency only
    from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: PLC0415

    params = StdioServerParameters(
        command=sys.executable, args=[str(launch), program], env=_env(dirs), cwd=str(dirs["project"]),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            results = [await session.call_tool(name, arguments) for name, arguments in calls]
            return init, tools, results


def _plugin_with_recorder(tmp_path: Path) -> tuple[Path, Recorder]:
    """A copy of the plugin's launcher, its hook scripts and a recording ``nx_setup.py``."""
    import shutil

    for sub in ("hooks/scripts", "launch"):
        shutil.copytree(_ROOT / "conexus" / sub, tmp_path / "plugin" / sub, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "plugin" / "launch" / "nx_setup.py").unlink()
    recorder = Recorder(tmp_path / "plugin" / "launch")
    return tmp_path / "plugin" / "launch" / "nx_launch.py", recorder


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
    init, tools, _results = await _session(dirs, "nx-mcp")
    assert sorted(tool.name for tool in tools) == ["setup", "setup_status"]
    by_name = {tool.name: tool for tool in tools}
    schema = by_name["setup"].inputSchema
    assert sorted(schema["properties"]) == ["autostart", "mode"] and schema["required"] == ["mode"]
    assert by_name["setup_status"].inputSchema.get("properties") in ({}, None)
    for tool in tools:
        assert tool.annotations is None, f"{tool.name}: Phase 1 gave no hints; the stand-in and the real server give none"
        assert "Nexus is missing" in (tool.description or ""), tool.name
        assert "/conexus:setup" in (tool.description or ""), tool.name
    assert "ask the user first" in (by_name["setup"].description or "").lower()
    assert "no sooner than" in (by_name["setup_status"].description or "")
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
@pytest.mark.parametrize(
    ("arguments", "argv"),
    [
        ({"mode": "local", "autostart": True}, ["start", "--mode", "local", "--autostart"]),
        ({"mode": "local"}, ["start", "--mode", "local"]),
        ({"mode": "local", "autostart": False}, ["start", "--mode", "local"]),
        ({"mode": "cli-only"}, ["start", "--mode", "cli-only"]),
        ({"mode": "cli-only", "autostart": True}, ["start", "--mode", "cli-only"]),
        ({"mode": "not-now"}, ["not-now"]),
    ],
    ids=["local+autostart", "local", "local autostart false", "cli-only", "cli-only ignores autostart", "not-now"],
)
async def test_setup_starts_the_setup_script_with_the_mode_and_autostart_given(
    tmp_path: Path, arguments: dict, argv: list[str],
) -> None:
    launch, recorder = _plugin_with_recorder(tmp_path)
    running = {
        "state": "running", "live": True, "stage": "starting", "mode": arguments["mode"], "log": "/cfg/setup/setup.log",
        "next": "Call setup_status to follow it.", "started": True,
    }
    recorder.views(start=running, **{"not-now": {"state": "none", "live": False, "not_now": True, "next": "x"}})
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup", arguments),))
    assert recorder.calls() == [argv]
    assert not result.isError
    text = _text(result)
    if arguments["mode"] == "not-now":
        assert "nothing was installed" in text.lower()
    else:
        assert "Setup started" in text and "Stage: starting" in text
        assert "/cfg/setup/setup.log" in text
        assert "no sooner than 10 seconds" in text, "the answer must say when to ask again"


@pytest.mark.asyncio
async def test_setup_status_reports_the_stage_the_log_and_the_next_step(tmp_path: Path) -> None:
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(status={
        "state": "running", "live": True, "stage": "init", "mode": "local", "log": "/cfg/setup/setup.log",
        "message": "running: init", "next": "Call setup_status to follow it.",
    })
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup_status", {}),))
    assert recorder.calls() == [["status"]]
    text = _text(result)
    assert "Stage: init" in text and "/cfg/setup/setup.log" in text and "Next: Call setup_status to follow it." in text
    assert "2 to 5 minutes" in text and "no sooner than 120 seconds" in text


@pytest.mark.asyncio
async def test_a_finished_build_says_to_reconnect_or_start_a_new_session(tmp_path: Path) -> None:
    """Record: the stand-in does not swap itself for the real server; when the
    build is done its status says to reconnect the server or start a new session."""
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(status={
        "state": "done", "live": False, "stage": "verify", "mode": "local", "log": "/cfg/setup/setup.log",
        "message": "Nexus is installed and nx doctor passes.", "next": "Reconnect the nexus server or start a new session.",
    })
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup_status", {}),))
    assert "Setup is done." in _text(result) and "Reconnect the nexus server or start a new session." in _text(result)
    assert "no sooner than" not in _text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"mode": "cloud"}, {"mode": "Cloud"}, {"mode": "bogus"}, {}, {"mode": 3}, {"mode": "local", "autostart": "yes"}])
async def test_a_refused_call_is_an_error_and_starts_nothing(tmp_path: Path, arguments: dict) -> None:
    """Until the credentials page exists the stand-in refuses a managed service,
    and nothing it says lists one."""
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(start={"state": "running", "live": True, "stage": "starting", "mode": "local", "next": "x"})
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup", arguments),))
    assert result.isError
    assert recorder.calls() == []
    if arguments.get("mode") in ("cloud", "Cloud"):
        assert "managed service" in _text(result) or "Unknown mode" in _text(result)
        assert "local" in _text(result) and "cli-only" in _text(result) and "not-now" in _text(result)


@pytest.mark.asyncio
async def test_nothing_the_stand_in_offers_lists_cloud(tmp_path: Path) -> None:
    _init, tools, _ = await _session(_isolated(tmp_path), "nx-mcp")
    assert "cloud" not in json.dumps([[t.title, t.description, t.inputSchema] for t in tools]).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("views", [{"_exit": 3}, {"_raw": "not json\n"}, {"_raw": "[]\n"}, {"_raw": ""}], ids=["exit 3", "not json", "not an object", "empty"])
async def test_a_setup_script_that_misbehaves_is_an_error_with_the_manual_commands(tmp_path: Path, views: dict) -> None:
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(**views)
    _init, _tools, results = await _session(
        _isolated(tmp_path), "nx-mcp", launch, (("setup", {"mode": "local"}), ("setup_status", {})),
    )
    for result in results:
        assert result.isError
        for command in _MANUAL_COMMANDS:
            assert command in _text(result), command
        assert "Traceback" not in _text(result)


@pytest.mark.asyncio
async def test_a_plugin_without_its_setup_script_answers_with_the_manual_commands(tmp_path: Path) -> None:
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.path.unlink()
    _init, _tools, results = await _session(
        _isolated(tmp_path), "nx-mcp", launch, (("setup", {"mode": "local"}), ("setup_status", {})),
    )
    for result in results:
        assert result.isError
        for command in _MANUAL_COMMANDS:
            assert command in _text(result), command


@pytest.mark.asyncio
async def test_status_through_the_real_script_in_an_empty_config_directory(tmp_path: Path) -> None:
    """The real ``nx_setup.py``, a command that builds nothing: ``status`` reads a
    directory that does not exist and must not create it."""
    dirs = _isolated(tmp_path)
    before = _tree(dirs)
    _init, _tools, (result,) = await _session(dirs, "nx-mcp", _LAUNCH, (("setup_status", {}),))
    assert not result.isError
    assert "Nexus is not installed" in _text(result) and "No setup has run." in _text(result)
    assert _tree(dirs) == before, "status wrote something"


@pytest.mark.asyncio
async def test_not_now_through_the_real_script_writes_only_its_marker(tmp_path: Path) -> None:
    dirs = _isolated(tmp_path)
    _init, _tools, (result,) = await _session(dirs, "nx-mcp", _LAUNCH, (("setup", {"mode": "not-now"}),))
    assert not result.isError and "nothing was installed" in _text(result).lower()
    assert sorted(p.name for p in (dirs["config"] / "setup").iterdir()) == ["not-now"]


@pytest.mark.asyncio
async def test_cloud_through_the_real_script_path_changes_nothing(tmp_path: Path) -> None:
    dirs = _isolated(tmp_path)
    before = _tree(dirs)
    _init, _tools, (result,) = await _session(dirs, "nx-mcp", _LAUNCH, (("setup", {"mode": "cloud"}),))
    assert result.isError
    assert _tree(dirs) == before


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


def test_the_descriptions_name_the_command_that_ships_with_the_tools() -> None:
    """Phase 1 held ``/conexus:setup`` back; it ships in the client release that
    carries these tools, so the descriptions name it. The instructions text is the
    Phase 1 sentence and stays so until the question and the command land."""
    mod = _standin()
    for tool in mod.SETUP_TOOLS:
        assert "/conexus:setup" in tool["description"]
    assert "/conexus:setup" not in mod.instructions()


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


def test_the_stand_in_spawns_only_its_own_interpreter() -> None:
    """``tests/test_hooks_bare_exec_lint.py`` exempts this file from resolving a
    program name because every spawn here starts ``sys.executable``, an absolute path
    that nothing searches for. This keeps the exemption true: any spawn whose argv
    does not start with it fails here."""
    tree = ast.parse(_STANDIN.read_text(encoding="utf-8"))
    spawns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in {"run", "Popen", "call", "check_output", "system", "execv", "run_bounded"}
    ]
    assert spawns, "the scan found no spawn in the stand-in; it examined nothing"
    for call in spawns:
        head = call.args[0] if call.args else None
        assert isinstance(head, ast.List) and head.elts, ast.dump(call)[:200]
        first = head.elts[0]
        assert isinstance(first, ast.Attribute) and first.attr == "executable" and getattr(first.value, "id", "") == "sys", ast.dump(first)


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
        install_windows_exe_fake(dirs["bin"], "nx-mcp", f"{WINDOWS_SHEBANG}\nprint('real')\n")
    else:
        fake = dirs["bin"] / "nx-mcp"
        fake.write_text("#!/bin/sh\necho real\n")
        fake.chmod(0o755)
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        input=b"", capture_output=True, env=_env(dirs), timeout=30, check=False,
    )
    assert r.stdout.strip() == b"real"

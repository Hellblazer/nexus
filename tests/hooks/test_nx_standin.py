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
import time
from pathlib import Path

import pytest
from tests._platform import IS_WINDOWS, WINDOWS_SHEBANG, install_windows_exe_fake, prune_python, scrubbed_env
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
        # A cloud run asks the system to open the credentials page; this records the opener's path instead, so
        # no case here can start a browser. It sits beside the isolated directories, not in them.
        NX_SETUP_OPEN_RECORD=str(dirs["home"].parent / "open.record"),
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
async def test_the_instructions_say_nexus_is_not_installed_put_the_one_question_and_name_the_manual_commands(
    tmp_path: Path,
) -> None:
    """RDR-228 "The one question": the same table the hook gives, the Local follow-up
    (start at login; a no means starting it again after each reboot), the command,
    and the clause that the user is asked first."""
    init, _tools, _calls = await _session(_isolated(tmp_path), "nx-mcp")
    text = init.instructions or ""
    mod = _standin()
    assert "Nexus is not installed" in text
    for command in _MANUAL_COMMANDS:
        assert command in text, command
    assert mod.QUESTION_TABLE in text and mod.LOCAL_FOLLOW_UP in text
    assert "/conexus:setup" in text
    assert "ASK THE USER FIRST" in text
    assert "| Cloud |" in text and mod.CLOUD_FOLLOW_UP in text, "the Cloud row arrived with the credentials page (nexus-sv2wx.18)"
    assert "never ask for the token" in text
    assert "installed but cannot start" not in text


def test_the_table_is_the_records_four_choices() -> None:
    """The record's four choices with their sizes, row for row."""
    record = (_ROOT / "docs" / "rdr" / "rdr-228-plugin-installs-the-cli.md").read_text(encoding="utf-8")
    rows = [line for line in record.splitlines() if line.startswith("| ") and line.count("|") == 4]
    by_choice = {row.split("|")[1].strip(): row for row in rows}
    ours = [line for line in _standin().QUESTION_TABLE.splitlines() if line.startswith("| ") and not line.startswith("| Choice")]
    assert [line.split("|")[1].strip() for line in ours] == ["Local", "Cloud", "CLI only", "Not now"]
    for line in ours:
        choice = line.split("|")[1].strip()
        assert by_choice[choice].strip() == line.strip(), choice


def test_the_tool_descriptions_give_the_tables_sizes() -> None:
    """Download and disk, taken from the table itself so the two cannot drift."""
    import re

    mod = _standin()
    description = mod.SETUP_DESCRIPTION
    rows = {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(r"\| (Local|Cloud|CLI only) \|[^|]*\| about ([\d.]+) GB down, ([\d.]+) GB on disk \|", mod.QUESTION_TABLE)
    }
    assert rows == {"Local": ("1.3", "2.4"), "Cloud": ("0.7", "1.6"), "CLI only": ("0.7", "1.6")}, rows
    local, cloud, cli = rows["Local"], rows["Cloud"], rows["CLI only"]
    assert f"up to about {local[0]} GB to download, {local[1]} GB on disk" in description
    assert f"up to about {cloud[0]} GB to download, {cloud[1]} GB on disk" in description
    assert f"up to about {cli[0]} GB to download, {cli[1]} GB on disk" in description


# -- installed but cannot start (Sam, 2026-10-14) --------------------------------------------


def test_the_broken_stand_in_says_installed_but_cannot_start_in_every_channel() -> None:
    mod = _standin()
    reason = "the Python it was built with (/x/cpython-gone) is gone"
    stand_in = mod.stand_in("nx-mcp", reason)
    assert stand_in.broken == reason
    for text in (stand_in.instructions, *(tool["description"] for tool in stand_in.tools)):
        assert text.startswith(mod.BROKEN_PHRASE) or mod.BROKEN_PHRASE in text
        assert "/x/cpython-gone" in text
        assert "Nexus is not installed" not in text
    assert "ASK THE USER FIRST" in stand_in.instructions
    assert [t["name"] for t in stand_in.tools] == ["setup", "setup_status"]
    plain = mod.stand_in("nx-mcp")
    assert plain.broken is None and plain.tools == mod.SETUP_TOOLS, "the plain stand-in is unchanged"
    # Schemas and titles are the normal ones: only the description changes.
    for broken, normal in zip(stand_in.tools, mod.SETUP_TOOLS, strict=True):
        assert {k: v for k, v in broken.items() if k != "description"} == {k: v for k, v in normal.items() if k != "description"}


def test_the_catalog_stand_in_is_the_same_whether_the_cli_is_missing_or_broken() -> None:
    mod = _standin()
    plain, broken = mod.stand_in("nx-mcp-catalog"), mod.stand_in("nx-mcp-catalog", "x")
    assert (plain.instructions, plain.tools, plain.name) == (broken.instructions, broken.tools, broken.name) == ("", (), plain.name)


def _pruned_generation(dirs: dict[str, Path], tmp_path: Path) -> Path:
    """A ``current`` generation under the tools root whose Python ``uv`` pruned."""
    gen = dirs["tools"] / "gen-20260101T000000Z"
    (gen / "bin").mkdir(parents=True)
    exe = gen / "bin" / ("nx-mcp.exe" if os.name == "nt" else "nx-mcp")
    exe.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
    exe.chmod(0o755)
    prune_python(gen, tmp_path / "uv" / "cpython-pruned")
    if os.name == "nt":
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(gen), str(dirs["tools"] / "current"))
    else:
        os.symlink(gen, dirs["tools"] / "current")
    return gen


@pytest.mark.asyncio
async def test_a_found_cli_that_cannot_start_is_served_to_a_real_client_as_the_broken_stand_in(tmp_path: Path) -> None:
    dirs = _isolated(tmp_path)
    _pruned_generation(dirs, tmp_path)
    init, tools, (status,) = await _session(dirs, "nx-mcp", calls=(("setup_status", {}),))
    assert init.serverInfo.name == "nexus-installed-cannot-start"
    assert "Nexus is installed but cannot start; setup repairs it" in (init.instructions or "")
    assert sorted(t.name for t in tools) == ["setup", "setup_status"]
    assert all((t.description or "").startswith("Nexus is installed but cannot start") for t in tools)
    assert "cpython-pruned" in _text(status) and "Nexus is not installed" not in _text(status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "argv"),
    [
        ({"mode": "local", "autostart": True}, ["start", "--mode", "local", "--autostart"]),
        ({"mode": "local"}, ["start", "--mode", "local"]),
        ({"mode": "local", "autostart": False}, ["start", "--mode", "local"]),
        ({"mode": "cloud"}, ["start", "--mode", "cloud"]),
        ({"mode": "cloud", "autostart": True}, ["start", "--mode", "cloud"]),
        ({"mode": "cli-only"}, ["start", "--mode", "cli-only"]),
        ({"mode": "cli-only", "autostart": True}, ["start", "--mode", "cli-only"]),
        ({"mode": "not-now"}, ["not-now"]),
    ],
    ids=[
        "local+autostart", "local", "local autostart false", "cloud", "cloud ignores autostart", "cli-only",
        "cli-only ignores autostart", "not-now",
    ],
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
async def test_a_done_record_with_no_cli_found_is_not_reported_as_a_finished_build(tmp_path: Path) -> None:
    """Record: the stand-in does not swap itself for the real server, and when the build is
    done its status says to reconnect. The poll asks the lookup first, so a ``done`` record on
    a machine where no CLI resolves (here: the isolated machine has none) is an install that is
    gone, not a finished build; the reconnect step once the CLI resolves is pinned in process
    (``test_once_the_cli_can_start_the_record_is_reported_with_the_reconnect_step_once``)."""
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(status={
        "state": "done", "live": False, "stage": "verify", "mode": "local", "log": "/cfg/setup/setup.log",
        "message": "Nexus is installed and nx doctor passes.", "next": "Reconnect the nexus server or start a new session.",
    })
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup_status", {}),))
    text = _text(result)
    assert text.startswith("Nexus is not installed") and "is gone" in text and "Log: /cfg/setup/setup.log" in text
    assert "Setup is done." not in text and "no sooner than" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"mode": "Cloud"}, {"mode": "bogus"}, {}, {"mode": 3}, {"mode": "local", "autostart": "yes"}])
async def test_a_refused_call_is_an_error_and_starts_nothing(tmp_path: Path, arguments: dict) -> None:
    """A mode the stand-in does not know, or an argument of the wrong type, is an error that starts nothing."""
    launch, recorder = _plugin_with_recorder(tmp_path)
    recorder.views(start={"state": "running", "live": True, "stage": "starting", "mode": "local", "next": "x"})
    _init, _tools, (result,) = await _session(_isolated(tmp_path), "nx-mcp", launch, (("setup", arguments),))
    assert result.isError
    assert recorder.calls() == []
    if arguments.get("mode") == "Cloud":
        for mode in ("local", "cloud", "cli-only", "not-now"):
            assert mode in _text(result)


@pytest.mark.asyncio
async def test_the_stand_in_offers_cloud_in_both_tool_descriptions_it_carries_a_choice_in(tmp_path: Path) -> None:
    _init, tools, _ = await _session(_isolated(tmp_path), "nx-mcp")
    by_name = {t.name: t for t in tools}
    assert "cloud" in json.dumps([by_name["setup"].description, by_name["setup"].inputSchema]).lower()
    assert "never ask for the token" in by_name["setup"].description


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
async def test_cloud_through_the_real_script_path_starts_a_run_that_serves_the_page(tmp_path: Path) -> None:
    """Mode ``cloud`` goes to the real setup process like any other. The isolated PATH has no ``uv``, so the run
    serves its page, asks the system to open it (recorded, not started) and then fails at the build.

    The session ends when this call returns, and on native Windows the host's job can end a run the session
    started once the session is gone (the real-server twin of this case, in ``tests/mcp/test_setup_tools.py``,
    keeps its server alive and asserts the whole run there). Here the start is asserted everywhere and the run's
    end where the run outlives its starter."""
    dirs = _isolated(tmp_path)
    _init, _tools, (result,) = await _session(dirs, "nx-mcp", _LAUNCH, (("setup", {"mode": "cloud"}),))
    assert not result.isError
    assert "mode cloud" in _text(result)
    status_file = dirs["config"] / "setup" / "status.json"
    assert json.loads(status_file.read_text(encoding="utf-8"))["mode"] == "cloud"
    if IS_WINDOWS:
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if json.loads(status_file.read_text(encoding="utf-8")).get("state") == "failed":
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["state"] == "failed" and data["failed_step"] == "build" and data["mode"] == "cloud"
    assert data["credentials"] == "waiting for credentials"
    assert (dirs["home"].parent / "open.record").read_text(encoding="utf-8").strip().endswith("credentials-opener.html")
    assert not (dirs["config"] / "setup" / "credentials-opener.html").exists(), "the failed run closed its page and deleted the opener"


@pytest.mark.asyncio
async def test_no_cli_and_nx_mcp_catalog_is_a_stand_in_with_no_tools(tmp_path: Path) -> None:
    """The application shows a connected server, and the question is not offered
    twice: no tools and no instructions."""
    init, tools, _calls = await _session(_isolated(tmp_path), "nx-mcp-catalog")
    assert tools == []
    assert not init.instructions


def test_the_commands_are_the_platform_s_own_and_the_instructions_name_them() -> None:
    """README.md: on Linux the first command names the CPU build of torch, and
    without it uv installs 4.3 GB that Nexus never uses."""
    mod = _standin()
    assert mod.manual_commands("linux")[0] == "uv tool install conexus --python 3.12 --torch-backend cpu"
    for platform in ("darwin", "win32"):
        assert mod.manual_commands(platform)[0] == "uv tool install conexus --python 3.12"
    for platform in ("linux", "darwin", "win32"):
        assert mod.manual_commands(platform)[1:] == ("nx self install", "nx init")
        text = mod.instructions(platform)
        assert text.endswith(")."), text
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
    carries these tools (nexus-sv2wx.17), so the descriptions and the instructions
    name it."""
    mod = _standin()
    for tool in mod.SETUP_TOOLS:
        assert "/conexus:setup" in tool["description"]
    assert "/conexus:setup" in mod.instructions()


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
    # ``_cli_lookup`` is the plugin's own sibling (hooks/scripts, itself standard library only), which the
    # poll asks again whether the CLI can start; ``cli_state`` imports it in the function.
    assert found <= set(sys.stdlib_module_names) | {"__future__", "_cli_lookup"}, found


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
        fake.write_text("#!/bin/sh\necho real\n", encoding="utf-8")
        fake.chmod(0o755)
    r = subprocess.run(
        [sys.executable, str(_LAUNCH), "nx-mcp"],
        input=b"", capture_output=True, env=_env(dirs), timeout=30, check=False,
    )
    assert r.stdout.strip() == b"real"


# -- the broken stand-in's tools, by the state of the setup record (review of 4cffeb898, critic 2) -----------

_REASON = "the Python it was built with (/x/cpython-gone) is gone"

#: ``(state, view)``: what ``nx_setup.py status`` can answer.
_VIEWS: dict[str, dict] = {
    "none": {"state": "none", "next": "Ask the user which they want.", "log": "/c/setup/setup.log", "live": False},
    "running": {"state": "running", "stage": "build", "mode": "cli-only", "live": True, "log": "/c/setup/setup.log"},
    "done": {"state": "done", "stage": "verify", "mode": "cli-only", "message": "The Nexus CLI is installed.", "live": False},
    "failed": {"state": "failed", "stage": "build", "mode": "cli-only", "message": "the build exited 2.", "live": False},
    "interrupted": {"state": "interrupted", "stage": "build", "mode": "local", "live": False},
    "doctor-failed": {"state": "doctor-failed", "stage": "verify", "mode": "local", "doctor_failing": ["[✗] x"], "live": False},
    "unsupported": {"state": "unsupported", "message": "no service manager here.", "live": False, "log": "/c/setup/setup.log"},
}


def _status_of(mod, monkeypatch: pytest.MonkeyPatch, state: str):
    monkeypatch.setattr(mod, "_call_script", lambda args, broken=None: dict(_VIEWS[state]))
    return mod


def _cli_is(mod, monkeypatch: pytest.MonkeyPatch, kind: str, reason: str | None = None) -> None:
    """What the poll finds when it asks the lookup again."""
    monkeypatch.setattr(mod, "cli_state", lambda: (kind, reason))


def test_in_broken_mode_a_machine_with_no_setup_history_gets_the_cannot_start_text_first(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _status_of(_standin(), monkeypatch, "none")
    _cli_is(mod, monkeypatch, "broken", _REASON)
    text = mod.run_status(_REASON)
    assert text.startswith(mod.broken_text(_REASON)) and mod.BROKEN_REPAIR in text
    assert "Nexus is not installed" not in text
    assert text.endswith(mod.render(_VIEWS["none"]))


def test_in_broken_mode_a_live_run_is_reported_as_it_is_and_never_under_the_repair_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stand-in is served until the user reconnects, so a poll DURING the repair must
    say how it is going, not offer it again with 'ask first'."""
    mod = _status_of(_standin(), monkeypatch, "running")
    _cli_is(mod, monkeypatch, "broken", _REASON)
    text = mod.run_status(_REASON)
    assert text == mod.render(_VIEWS["running"])
    assert mod.BROKEN_PHRASE not in text and "ASK THE USER FIRST" not in text
    assert mod.run_status(None) == text, "without the broken reason the answer is the same"


@pytest.mark.parametrize("state", ["done", "failed", "interrupted", "doctor-failed", "unsupported"])
def test_in_broken_mode_a_recorded_history_does_not_hide_the_cannot_start_statement_while_it_still_cannot_start(
    monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    """A setup record outlives the install it describes (``status.json`` is never cleared), so
    a ``done`` from months ago says nothing about a CLI whose Python was pruned since. The
    poll asks the lookup again: while the CLI still cannot start, the statement and the repair
    offer come first, and the record's report after them."""
    mod = _status_of(_standin(), monkeypatch, state)
    fresh = "the Python it was built with (/y/cpython-fresh) is gone"
    _cli_is(mod, monkeypatch, "broken", fresh)
    text = mod.run_status(_REASON)
    assert text.startswith(mod.broken_text(fresh)), "the reason is the one found now"
    assert mod.BROKEN_REPAIR in text
    assert "Nexus is not installed" not in text
    if state == "done":
        assert "did not repair" in text and "Setup is done" not in text and "Reconnect" not in text
    else:
        assert mod.render(_VIEWS[state]) in text, "the record's report follows"
        assert text.index(mod.BROKEN_REPAIR) < text.index(mod.render(_VIEWS[state]))


def test_in_broken_mode_a_failed_run_says_it_failed_and_gives_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``failed`` on record may be an old build or init failure, not a repair, so the words
    say the last run failed and do not claim a repair was tried."""
    mod = _status_of(_standin(), monkeypatch, "failed")
    monkeypatch.setattr(mod, "_call_script", lambda args, broken=None: {**_VIEWS["failed"], "log": "/c/setup/setup.log"})
    _cli_is(mod, monkeypatch, "broken", _REASON)
    text = mod.run_status(_REASON)
    assert "The last setup run failed" in text and "Log: /c/setup/setup.log" in text and "the build exited 2." in text
    assert "repair failed" not in text


#: The ``next`` a real ``done`` record carries (``nx_setup.py``): the reconnect step itself.
_DONE_NEXT = "Reconnect the nexus server or start a new session."


@pytest.mark.parametrize("launcher_reason", [_REASON, None], ids=["found broken at start", "not found at start"])
@pytest.mark.parametrize("state", ["none", "running", "done", "failed", "interrupted", "doctor-failed"])
def test_once_the_cli_can_start_the_record_is_reported_with_the_reconnect_step_once(
    monkeypatch: pytest.MonkeyPatch, state: str, launcher_reason: str | None,
) -> None:
    """Whichever way the launcher found the CLI at start, once it can start the answer is the
    record and ONE reconnect step: a ``done`` keeps its own next step (which is that step),
    any other record's next step was written before the CLI could start and is dropped."""
    mod = _standin()
    view = {**_VIEWS[state], "next": _DONE_NEXT if state == "done" else "Ask the user, then call setup."}
    monkeypatch.setattr(mod, "_call_script", lambda args, broken=None: dict(view))
    _cli_is(mod, monkeypatch, "startable")
    text = mod.run_status(launcher_reason)
    assert mod.BROKEN_PHRASE not in text and mod.BROKEN_REPAIR not in text and mod.NOT_INSTALLED not in text
    if state == "running":
        assert text == mod.render(view)
        return
    assert "Ask the user, then call setup." not in text, "a next step from before the CLI could start is not offered"
    assert sum(text.count(s) for s in (_DONE_NEXT, mod.RECONNECT)) == 1, text


@pytest.mark.parametrize("launcher_reason", [_REASON, None], ids=["found broken at start", "not found at start"])
def test_a_done_record_with_no_cli_now_is_an_install_that_is_gone(
    monkeypatch: pytest.MonkeyPatch, launcher_reason: str | None,
) -> None:
    """A ``done`` from an earlier setup outlives the install (its tools directory removed or
    moved): with no CLI found now, the answer is "not installed" and the question again, never
    "Setup is done ... reconnect", which would serve the stand-in again forever."""
    mod = _status_of(_standin(), monkeypatch, "done")
    _cli_is(mod, monkeypatch, "missing")
    text = mod.run_status(launcher_reason)
    assert text.startswith(mod.NOT_INSTALLED) and "is gone" in text and mod.question() in text
    assert "Setup is done" not in text and "econnect" not in text
    assert mod.BROKEN_PHRASE not in text


@pytest.mark.parametrize("state", ["none", "failed", "interrupted", "doctor-failed", "unsupported"])
def test_with_no_cli_now_any_other_record_is_reported_under_not_installed(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    mod = _status_of(_standin(), monkeypatch, state)
    _cli_is(mod, monkeypatch, "missing")
    assert mod.run_status(_REASON) == mod.run_status(None) == f"{mod.NOT_INSTALLED}\n{mod.render(_VIEWS[state])}"


def test_a_lookup_that_cannot_be_loaded_changes_nothing_from_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cli_state`` answers ``("broken", None)`` when the lookup cannot be loaded: the launcher's
    own finding stands, broken with its reason or not installed."""
    mod = _status_of(_standin(), monkeypatch, "done")
    _cli_is(mod, monkeypatch, "broken", None)
    assert mod.run_status(_REASON).startswith(mod.broken_text(_REASON))
    assert mod.run_status(None).startswith(mod.NOT_INSTALLED)


def test_the_poll_asks_the_lookup_again_of_the_program_the_launcher_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli_state`` resolves ``nx-mcp`` as the launcher does and asks ``cannot_start`` again: a
    pruned generation is broken, the same tree once ``uv`` rebuilt it can start, and a CLI that
    is no longer there is missing."""
    mod = _standin()
    dirs = _isolated(tmp_path)
    gen = _pruned_generation(dirs, tmp_path)
    monkeypatch.setenv("NX_TOOLS_DIR", str(dirs["tools"]))
    monkeypatch.setenv("PATH", str(dirs["bin"]))
    kind, reason = mod.cli_state()
    assert kind == "broken" and "cpython-pruned" in (reason or "")
    (gen / "pyvenv.cfg").unlink()  # the repair: a generation with nothing wrong in its files
    assert mod.cli_state() == ("startable", None)
    for entry in (gen / "bin").iterdir():
        entry.unlink()
    assert mod.cli_state() == ("missing", None)


def test_without_the_broken_reason_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _status_of(_standin(), monkeypatch, "none")
    _cli_is(mod, monkeypatch, "missing")
    assert mod.run_status(None) == f"{mod.NOT_INSTALLED}\n{mod.render(_VIEWS['none'])}"


def test_a_missing_setup_process_in_broken_mode_says_cannot_start_not_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _standin()
    text = mod.setup_text(broken=_REASON)
    assert mod.BROKEN_PHRASE in text and "/x/cpython-gone" in text
    assert "not installed" not in text.lower()
    assert "self install --bootstrap" in text, "the one command that repairs it, for the user to run"
    plain = mod.setup_text()
    assert plain.startswith(mod.NOT_INSTALLED), "the plain text is unchanged"
    # Through the tools: a missing nx_setup.py in broken mode.
    monkeypatch.setattr(mod, "setup_script", lambda: None)
    _cli_is(mod, monkeypatch, "broken", _REASON)
    with pytest.raises(mod.SetupError) as status_error:
        mod.run_status(_REASON)
    assert mod.BROKEN_PHRASE in str(status_error.value) and "not installed" not in str(status_error.value).lower()
    with pytest.raises(mod.SetupError) as setup_error:
        mod.run_setup({"mode": "cli-only"}, _REASON)
    assert mod.BROKEN_PHRASE in str(setup_error.value) and "not installed" not in str(setup_error.value).lower()
    with pytest.raises(mod.SetupError) as plain_error:
        mod.run_setup({"mode": "cli-only"})
    assert str(plain_error.value).startswith(mod.NOT_INSTALLED)


def test_the_tool_call_hands_the_broken_reason_to_both_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _standin()
    monkeypatch.setattr(mod, "setup_script", lambda: None)
    _cli_is(mod, monkeypatch, "broken", _REASON)  # the poll asks the lookup again; without this it reads the host's own install
    server = mod.stand_in("nx-mcp", _REASON)
    for name, arguments in (("setup", {"mode": "cli-only"}), ("setup_status", {})):
        reply = mod.handle(server, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments},
        })
        assert reply is not None
        text = reply["result"]["content"][0]["text"]
        assert reply["result"]["isError"] is True and mod.BROKEN_PHRASE in text and "not installed" not in text.lower(), name


def _names_the_desktop_restart(text: str) -> bool:
    return "Claude Desktop" in text and "quit the app" in text and "a new chat" in text


def test_every_reconnect_instruction_names_the_desktop_restart() -> None:
    """Claude Desktop starts a plugin's server once, when the app starts, and keeps it for every
    new chat, with no Reconnect command; only quitting the app and opening it again replaces the
    stand-in with the real server (nexus-93xx0, measured on hellmini 2026-10-16). So every text
    that tells the user to reconnect also gives Desktop's step, not only Claude Code's."""
    mod = _standin()
    texts = {
        "RECONNECT": mod.RECONNECT,
        "instructions": mod.instructions(),
        "instructions(broken)": mod.instructions(broken="the program exited 1"),
        "setup_text": mod.setup_text(),
        "setup_text(broken)": mod.setup_text(broken="the program exited 1"),
        "setup_text(cloud)": mod.setup_text(mode="cloud"),
        "SETUP_STATUS_DESCRIPTION": mod.SETUP_STATUS_DESCRIPTION,
    }
    missing = [name for name, text in texts.items() if not _names_the_desktop_restart(text)]
    assert not missing, missing

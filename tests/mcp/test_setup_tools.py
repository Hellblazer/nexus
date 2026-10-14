# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``setup`` and ``setup_status`` tools on the real ``nx-mcp`` (RDR-228 Phase 2
Step 3, nexus-sv2wx.16; residuals 6 to 8 of nexus-9i4d4).

The real server registers both tools always. They start ``conexus/launch/
nx_setup.py`` (the path in ``NX_SETUP_SCRIPT``, set by the launcher) with the
server's own Python and go through that file's command line for everything: the
server never reads or writes the setup directory. The cases here use a recording
script that stands in for ``nx_setup.py`` where a run would otherwise start a real
build, and the real ``nx_setup.py`` only for commands that touch nothing outside a
temporary config directory (``status``, ``not-now``).
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from nexus.mcp import setup_tools
from nexus.mcp.core import mcp as core_mcp
from tests._module_seam import patch_in
from tests._platform import scrubbed_env
from tests._setup_recorder import Recorder

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCH_DIR = _ROOT / "conexus" / "launch"
_NX_SETUP = _LAUNCH_DIR / "nx_setup.py"
_STANDIN = _LAUNCH_DIR / "nx_standin.py"
_LAUNCH = _LAUNCH_DIR / "nx_launch.py"
_TOOLS_SOURCE = _ROOT / "src" / "nexus" / "mcp" / "setup_tools.py"

def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def nx_setup_module() -> Any:
    return _load(_NX_SETUP, "nx_setup_for_setup_tools_tests")


@pytest.fixture(scope="module")
def standin_module() -> Any:
    return _load(_STANDIN, "nx_standin_for_setup_tools_tests")


def _view(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "none", "live": False, "terminal": False, "not_now": False,
        "next": "Call setup with a mode (local, cli-only or not-now).",
    }
    base.update(fields)
    base["terminal"] = base["state"] in ("done", "doctor-failed", "failed", "unsupported")
    return base


RUNNING_BUILD = _view(
    state="running", live=True, stage="build", mode="local", log="/cfg/setup/setup.log",
    message="running: build", next="Call setup_status to follow it.", started=True,
)
RUNNING_INIT = {**RUNNING_BUILD, "stage": "init", "message": "running: init"}
DONE = _view(
    state="done", stage="verify", mode="local", log="/cfg/setup/setup.log",
    message="Nexus is installed and nx doctor passes.", next="Reconnect the nexus server or start a new session.",
)
FAILED = _view(
    state="failed", stage="build", failed_step="build", mode="local", log="/cfg/setup/setup.log",
    message="the build exited 1.", next="Call setup again: it resumes at the build step. Log: /cfg/setup/setup.log",
)
DOCTOR_FAILED = _view(
    state="doctor-failed", stage="verify", mode="local", log="/cfg/setup/setup.log",
    message="nx doctor reported problems.", next="Run `nx doctor` in a terminal to see the failing rows.",
    doctor_failing=["[✗] T3 service: not reachable"],
)
INTERRUPTED = _view(
    state="interrupted", stage="init", mode="local", log="/cfg/setup/setup.log",
    next="Setup was interrupted. Call setup again: it resumes. Log: /cfg/setup/setup.log",
)
UNSUPPORTED = _view(
    state="unsupported", stage="starting", mode="local", log="/cfg/setup/setup.log",
    message="setup cannot detach on this host.", next="Install Nexus in a terminal. Log: /cfg/setup/setup.log",
)
NONE = _view()
ERROR = _view(state="error", started=False, message="OSError: disk full", next="Nothing was started.")

#: One status view per state ``nx_setup.py`` can print (checked against ALL_STATES).
VIEWS = {
    "running": RUNNING_BUILD, "done": DONE, "failed": FAILED, "doctor-failed": DOCTOR_FAILED,
    "interrupted": INTERRUPTED, "unsupported": UNSUPPORTED, "none": NONE, "error": ERROR,
}


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A recording stand-in for nx_setup.py, wired through NX_SETUP_SCRIPT."""
    rec = Recorder(tmp_path / "recorder")
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, str(rec.path))
    rec.views(status=NONE, start=RUNNING_BUILD, **{"not-now": _view(not_now=True)})
    return rec


def _service(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    monkeypatch.setattr(setup_tools, "usable_service", lambda: usable)


def _call(name: str, **arguments: Any) -> str:
    result = asyncio.run(core_mcp.call_tool(name, arguments))
    return "".join(block.text for block in result if getattr(block, "type", "") == "text")


def _tools() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(core_mcp.list_tools())}


# -- registration -----------------------------------------------------------------


@pytest.mark.parametrize("usable", [True, False], ids=["with a usable service", "without one"])
def test_the_real_server_registers_both_tools_always(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    _service(monkeypatch, usable)
    tools = _tools()
    assert {"setup", "setup_status"} <= set(tools)
    assert set(core_mcp._tool_manager._tools) >= {"setup", "setup_status"}


def test_the_schema_takes_a_mode_and_an_autostart_and_status_takes_nothing() -> None:
    tools = _tools()
    schema = tools["setup"].inputSchema
    assert sorted(schema["properties"]) == ["autostart", "mode"]
    assert schema["required"] == ["mode"]
    assert schema["properties"]["mode"]["type"] == "string"
    assert schema["properties"]["autostart"]["type"] == "boolean"
    assert schema["properties"]["autostart"]["default"] is False
    assert tools["setup_status"].inputSchema.get("properties") in ({}, None)


def test_no_description_or_schema_text_lists_cloud() -> None:
    for tool in _tools().values():
        if tool.name not in ("setup", "setup_status"):
            continue
        text = json.dumps([tool.title, tool.description, tool.inputSchema]).lower()
        assert "cloud" not in text, tool.name


def test_the_descriptions_tell_the_model_to_ask_first_and_name_both_fixes() -> None:
    tools = _tools()
    setup, status = tools["setup"].description, tools["setup_status"].description
    for text in (setup, status):
        assert "/conexus:setup" in text
        assert "Nexus is missing" in text
    assert "ask the user first" in setup.lower() and "act only on the answer" in setup.lower()
    assert "no sooner than" in status
    assert "minutes" in status, "the description must say how long a run takes"


# -- the instructions notice --------------------------------------------------------


class _Server:
    def __init__(self, instructions: str | None = None) -> None:
        self._mcp_server = type("Low", (), {"instructions": instructions})()


def test_no_usable_service_is_said_through_the_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp._first_run import apply_setup_notice

    _service(monkeypatch, False)
    server = _Server()
    assert apply_setup_notice(server) is True
    text = server._mcp_server.instructions
    assert "no usable" in text.lower() and "setup" in text and "/conexus:setup" in text
    assert "ask the user" in text.lower()


def test_a_usable_service_leaves_the_instructions_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp._first_run import apply_setup_notice

    _service(monkeypatch, True)
    server = _Server("existing")
    assert apply_setup_notice(server) is False
    assert server._mcp_server.instructions == "existing"


def test_the_notice_is_appended_after_an_existing_instructions_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp._first_run import apply_setup_notice

    _service(monkeypatch, False)
    server = _Server("existing")
    assert apply_setup_notice(server) is True
    assert server._mcp_server.instructions.startswith("existing\n\n")


def test_a_broken_check_never_blocks_the_server_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp import _first_run

    def boom() -> bool:
        raise RuntimeError("no")

    monkeypatch.setattr(setup_tools, "usable_service", boom)
    assert _first_run.apply_setup_notice(_Server()) is False


def test_main_applies_the_setup_notice() -> None:
    """The wiring: ``main`` is the only caller that reaches a real client, and a
    notice nobody calls says nothing."""
    from nexus.mcp import core

    tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {c.func.id for c in ast.walk(main) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "apply_setup_notice" in called


def test_usable_service_follows_the_endpoint_definition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The record's definition: a saved or exported service URL, or a live local
    lease. Nothing set, nothing usable. Reads no network."""
    for name in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert setup_tools.usable_service() is False
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    assert setup_tools.usable_service() is True


# -- setup: no script ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["local", "cli-only", "not-now"])
def test_without_nx_setup_script_setup_starts_no_process(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run, patch_in("nexus.mcp.setup_tools", "subprocess.Popen") as popen:
        text = _call("setup", mode=mode)
    run.assert_not_called()
    popen.assert_not_called()
    assert "nx init" in text if mode == "local" else text


def test_without_nx_setup_script_status_starts_no_process_and_gives_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        text = _call("setup_status")
    run.assert_not_called()
    assert "No usable Nexus service" in text and "nx init" in text


def test_a_script_path_that_is_not_a_file_is_treated_as_not_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, str(tmp_path / "missing.py"))
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        text = _call("setup", mode="local")
    run.assert_not_called()
    assert "nx init" in text


# -- setup: with the script ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "autostart", "argv"),
    [
        ("local", True, ["start", "--mode", "local", "--autostart"]),
        ("local", False, ["start", "--mode", "local"]),
        ("cli-only", False, ["start", "--mode", "cli-only"]),
        ("cli-only", True, ["start", "--mode", "cli-only"]),
        ("not-now", False, ["not-now"]),
    ],
)
def test_setup_starts_the_script_with_the_mode_and_autostart_given(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, mode: str, autostart: bool, argv: list[str],
) -> None:
    _service(monkeypatch, False)
    _call("setup", mode=mode, autostart=autostart)
    assert recorder.calls() == [argv]


def test_autostart_is_false_when_not_given(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    _call("setup", mode="local")
    assert recorder.calls() == [["start", "--mode", "local"]]


def test_setup_runs_the_script_with_the_servers_own_python(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        run.return_value.stdout = json.dumps(RUNNING_BUILD)
        run.return_value.returncode = 0
        _call("setup", mode="local")
    assert run.call_args.args[0][0] == sys.executable
    assert run.call_args.args[0][1] == str(recorder.path)


def test_setup_returns_the_stage_and_how_long_it_usually_takes(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    text = _call("setup", mode="local")
    assert "Setup started" in text and "build" in text
    assert "1 to 3 minutes" in text
    assert re.search(r"no (less|sooner) than 60 seconds", text), text
    assert "/cfg/setup/setup.log" in text
    recorder.views(status=NONE, start={**RUNNING_BUILD, "stage": "init"}, **{"not-now": NONE})
    text = _call("setup", mode="local")
    assert "2 to 5 minutes" in text and re.search(r"no (less|sooner) than 120 seconds", text), text


def test_a_start_refused_because_a_run_is_live_returns_that_runs_stage(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    live = {**RUNNING_BUILD, "started": False, "refused": "A setup run is already in progress (mode local, stage build). This request for mode cli-only started nothing."}
    recorder.views(status=NONE, start=live)
    text = _call("setup", mode="cli-only")
    assert "already in progress" in text and "started nothing" in text
    assert "Setup started" not in text


def test_not_now_says_it_installed_nothing(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    text = _call("setup", mode="not-now")
    assert "not now" in text.lower() and "nothing was installed" in text.lower()


def test_an_error_view_is_an_error_result(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    recorder.views(status=NONE, start=ERROR)
    with pytest.raises(ToolError, match="disk full"):
        _call("setup", mode="local")


@pytest.mark.parametrize(
    "views",
    [{"_exit": 3}, {"_raw": "not json\n"}, {"_raw": "[1]\n"}, {"_raw": ""}, {"_raw": '{"no_state": 1}\n'}],
    ids=["exit 3", "not json", "not an object", "empty", "no state"],
)
def test_a_script_that_misbehaves_is_a_tool_error_and_never_a_traceback(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, views: dict[str, Any],
) -> None:
    _service(monkeypatch, False)
    recorder.views(**views)
    for tool, args in (("setup", {"mode": "local"}), ("setup_status", {})):
        with pytest.raises(ToolError) as caught:
            _call(tool, **args)
        assert "Traceback" not in str(caught.value)
        assert "nx init" in str(caught.value), "the terminal commands are the way out"


def test_a_script_that_cannot_start_is_a_tool_error(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        run.side_effect = OSError("no such interpreter")
        with pytest.raises(ToolError, match="no such interpreter"):
            _call("setup", mode="local")


# -- cloud and bad modes ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["cloud", "Cloud", "bogus", ""])
def test_cloud_and_unknown_modes_are_refused_and_start_nothing(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, mode: str,
) -> None:
    _service(monkeypatch, False)
    with pytest.raises(ToolError) as caught:
        _call("setup", mode=mode)
    message = str(caught.value)
    assert "local" in message and "cli-only" in message and "not-now" in message
    assert recorder.calls() == []


def test_the_cloud_refusal_says_the_managed_service_is_not_offered_yet(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    with pytest.raises(ToolError, match="managed service"):
        _call("setup", mode="cloud")


# -- nothing is missing -------------------------------------------------------------


@pytest.mark.parametrize("mode", ["local", "cli-only", "not-now"])
def test_when_nothing_is_missing_setup_starts_nothing(monkeypatch: pytest.MonkeyPatch, recorder: Recorder, mode: str) -> None:
    _service(monkeypatch, True)
    text = _call("setup", mode=mode)
    assert "nothing is missing" in text.lower()
    assert all(call[0] == "status" for call in recorder.calls()), recorder.calls()


def test_when_nothing_is_missing_and_no_script_setup_says_so_and_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    _service(monkeypatch, True)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        assert "nothing is missing" in _call("setup", mode="local").lower()
        assert "nothing is missing" in _call("setup_status").lower()
    run.assert_not_called()


def test_when_nothing_is_missing_status_says_so(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, True)
    assert "nothing is missing" in _call("setup_status").lower()


def test_a_live_run_is_reported_even_when_a_service_already_answers(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """The service becomes usable midway through a run; the run is still live
    until its last step, and the hook reports a live run before anything else."""
    _service(monkeypatch, True)
    recorder.views(status=RUNNING_INIT, start=RUNNING_INIT)
    for tool, args in (("setup_status", {}), ("setup", {"mode": "local"})):
        text = _call(tool, **args)
        assert "in progress" in text and "init" in text, (tool, text)
    assert all(call[0] == "status" for call in recorder.calls()), "setup must start nothing while a run is live"


# -- status -------------------------------------------------------------------------


def test_the_views_cover_every_state_the_script_can_print(nx_setup_module: Any) -> None:
    assert set(VIEWS) == set(nx_setup_module.ALL_STATES)


@pytest.mark.parametrize("state", sorted(VIEWS))
def test_status_reports_the_stage_the_log_and_the_next_step_for_every_state(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, state: str,
) -> None:
    _service(monkeypatch, False)
    view = VIEWS[state]
    recorder.views(status=view)
    if state == "error":
        with pytest.raises(ToolError, match="disk full"):
            _call("setup_status")
        return
    text = _call("setup_status")
    assert view["next"] in text
    if view.get("log"):
        assert view["log"] in text
    if view.get("stage"):
        assert view["stage"] in text
    if view.get("message"):
        assert view["message"] in text


def test_a_running_status_names_the_interval_to_wait(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    recorder.views(status=RUNNING_INIT)
    text = _call("setup_status")
    assert "2 to 5 minutes" in text and re.search(r"no (less|sooner) than 120 seconds", text), text


def test_a_finished_status_does_not_tell_the_model_to_wait(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    recorder.views(status=DONE)
    assert "no sooner than" not in _call("setup_status") and "no less than" not in _call("setup_status")


def test_doctor_failures_are_listed(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    recorder.views(status=DOCTOR_FAILED)
    assert "[✗] T3 service: not reachable" in _call("setup_status")


def test_status_runs_the_read_only_status_command(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    _call("setup_status")
    assert recorder.calls() == [["status"]]


# -- the real nx_setup.py, nothing that builds ---------------------------------------


@pytest.fixture
def real_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    for name in ("NX_CONFIG_DIR", "NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, str(_NX_SETUP))
    return config


def test_status_through_the_real_script_in_an_empty_config_directory(real_script: Path) -> None:
    text = _call("setup_status")
    assert "No usable Nexus service" in text
    assert not (real_script / "setup").exists(), "status created the setup directory"


def test_not_now_through_the_real_script_writes_its_marker_and_nothing_else(real_script: Path) -> None:
    text = _call("setup", mode="not-now")
    assert "nothing was installed" in text.lower()
    assert sorted(p.name for p in (real_script / "setup").iterdir()) == ["not-now"]


def test_cloud_through_the_real_script_is_refused_before_the_script_runs(real_script: Path) -> None:
    with pytest.raises(ToolError):
        _call("setup", mode="cloud")
    assert not (real_script / "setup").exists()


# -- the server does not own the setup directory ---------------------------------------


def test_nothing_under_the_mcp_package_names_the_setup_directory_layout() -> None:
    """Residual 8: the real server's tools go through ``nx_setup.py``'s command
    line. If the server read ``status.json`` or ``lock`` itself, the directory
    layout would have two implementations."""
    forbidden = ("status.json", "status.error", "setup.log", "lock.reclaim", "heartbeat")
    for source in sorted((_ROOT / "src" / "nexus" / "mcp").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if source.name == "setup_tools.py":
            assert any("nx_setup" in s or "NX_SETUP_SCRIPT" in s for s in strings), "the scan examined the wrong file"
        for token in forbidden:
            assert not [s for s in strings if token in s and len(s) < 60], f"{source.name} names {token!r}"


def test_the_tools_module_never_resolves_a_config_directory_or_opens_a_file_for_writing() -> None:
    tree = ast.parse(_TOOLS_SOURCE.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"nexus_config_dir", "config_dir", "setup_dir", "Path", "open", "mkdir", "makedirs", "write_text"}, names


# -- stand-in and real server agree ----------------------------------------------------


def _comparable_schema(schema: Any) -> Any:
    """The argument schema without the ``title`` keys pydantic adds."""
    if isinstance(schema, dict):
        return {k: _comparable_schema(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_comparable_schema(v) for v in schema]
    return schema


async def _standin_tools(tmp_path: Path) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    dirs = {name: tmp_path / name for name in ("home", "tools", "bin", "config", "project")}
    for folder in dirs.values():
        folder.mkdir()
    env = scrubbed_env(
        PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]), USERPROFILE=str(dirs["home"]), CLAUDE_PROJECT_DIR=str(dirs["project"]),
    )
    params = StdioServerParameters(command=sys.executable, args=[str(_LAUNCH), "nx-mcp"], env=env, cwd=str(dirs["project"]))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return {tool.name: tool for tool in (await session.list_tools()).tools}


@pytest.mark.asyncio
async def test_the_stand_in_and_the_real_server_offer_the_same_two_tools(tmp_path: Path) -> None:
    """Residual 7: the record says one definition on both servers, and the two
    can only be copies of each other. Started as the application starts them: the
    stand-in as the launcher serves it with no CLI, the real server through its
    own tool registry."""
    standin = await _standin_tools(tmp_path)
    real = {tool.name: tool for tool in await core_mcp.list_tools()}
    for name in ("setup", "setup_status"):
        a, b = standin[name], real[name]
        assert a.name == b.name
        assert a.title == b.title, name
        assert a.description == b.description, f"{name}: the descriptions differ"
        assert _comparable_schema(a.inputSchema) == _comparable_schema(b.inputSchema), f"{name}: the argument schemas differ"
        assert (a.annotations or None) == (b.annotations or None), f"{name}: the annotations differ"


@pytest.mark.parametrize("state", sorted(VIEWS))
@pytest.mark.parametrize("started", [None, True, False])
def test_both_servers_render_every_status_the_same_way(standin_module: Any, state: str, started: bool | None) -> None:
    view = VIEWS[state]
    assert setup_tools.render(view, started=started) == standin_module.render(view, started=started)


def test_both_servers_hold_the_same_stage_table_modes_and_texts(standin_module: Any) -> None:
    assert setup_tools.STAGE_WAITS == standin_module.STAGE_WAITS
    assert setup_tools.MODES == standin_module.MODES
    assert setup_tools.CLOUD_REFUSAL == standin_module.CLOUD_REFUSAL
    assert setup_tools.NOT_NOW_TEXT == standin_module.NOT_NOW_TEXT
    assert setup_tools.mode_refusal("cloud") == standin_module.mode_refusal("cloud")
    assert setup_tools.mode_refusal("bogus") == standin_module.mode_refusal("bogus")


def test_every_stage_nx_setup_reports_has_a_wait(nx_setup_module: Any) -> None:
    """A stage the table lacks would get the generic wait; the table must cover
    the ones the script names."""
    tree = ast.parse(_NX_SETUP.read_text(encoding="utf-8"))
    stages = {
        c.args[0].value for c in ast.walk(tree)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "at"
        and c.args and isinstance(c.args[0], ast.Constant)
    }
    assert {"build", "service", "service-install", "service-start", "init", "verify", "starting"} <= stages
    assert stages <= set(setup_tools.STAGE_WAITS), stages - set(setup_tools.STAGE_WAITS)

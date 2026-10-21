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
import time
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


NX_SETUP: Any = _load(_NX_SETUP, "nx_setup_for_setup_tools_tests")


@pytest.fixture(scope="module")
def nx_setup_module() -> Any:
    return NX_SETUP


@pytest.fixture(scope="module")
def standin_module() -> Any:
    return _load(_STANDIN, "nx_standin_for_setup_tools_tests")


def _view(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "none", "live": False, "terminal": False, "not_now": False,
        "next": NX_SETUP._NEXT_NONE,
    }
    base.update(fields)
    base["terminal"] = base["state"] in ("done", "doctor-failed", "failed", "unsupported")
    return base


RUNNING_BUILD = _view(
    state="running", live=True, stage="build", mode="local", autostart=True, log="/cfg/setup/setup.log",
    message="running: build", next="Call setup_status to follow it.", started=True,
)
RUNNING_INIT = {**RUNNING_BUILD, "stage": "init", "message": "running: init"}
DONE = _view(
    state="done", stage="verify", mode="local", autostart=True, log="/cfg/setup/setup.log",
    message="Nexus is installed and nx doctor passes.", next="Reconnect the nexus server or start a new session.",
)
FAILED = _view(
    state="failed", stage="build", failed_step="build", mode="local", autostart=False, log="/cfg/setup/setup.log",
    message="the build exited 1.",
    next="If the user agrees, call setup again: it resumes at the build step. Log: /cfg/setup/setup.log",
)
DOCTOR_FAILED = _view(
    state="doctor-failed", stage="verify", mode="local", autostart=True, log="/cfg/setup/setup.log",
    message="nx doctor reported problems.", next="Run `nx doctor` in a terminal to see the failing rows.",
    doctor_failing=["[✗] T3 service: not reachable"],
)
INTERRUPTED = _view(
    state="interrupted", stage="init", mode="cli-only", autostart=False, log="/cfg/setup/setup.log",
    next="Setup was interrupted. If the user agrees, call setup again with the same mode: it resumes. Log: /cfg/setup/setup.log",
)
UNSUPPORTED = _view(
    state="unsupported", stage="starting", mode="local", autostart=False, log="/cfg/setup/setup.log",
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


def test_the_setup_tool_offers_cloud_and_says_the_token_is_typed_into_a_page() -> None:
    """The Cloud choice arrived with the credentials page (nexus-sv2wx.18). The description names it with its
    size, and tells the model never to take the token itself."""
    tools = _tools()
    text = json.dumps([tools["setup"].title, tools["setup"].description, tools["setup"].inputSchema]).lower()
    assert "cloud" in text
    assert "never ask for the token" in tools["setup"].description
    assert "cloud" in tools["setup"].inputSchema["properties"]["mode"]["description"]


def test_the_descriptions_tell_the_model_to_ask_first_and_name_both_fixes() -> None:
    tools = _tools()
    setup, status = tools["setup"].description, tools["setup_status"].description
    for text in (setup, status):
        assert "/conexus:setup" in text
        assert "Nexus is missing" in text
    assert "ask the user first" in setup.lower() and "act only on the answer" in setup.lower()
    assert "no sooner than" in status
    assert "minutes" in status, "the description must say how long a run takes"


def test_the_download_sizes_are_ceilings_not_promises() -> None:
    """A provisioned service downloads nothing: the description says "up to"."""
    text = _tools()["setup"].description
    assert "up to about 1.3 GB" in text and "up to about 0.7 GB" in text
    assert "about 1.3 GB to download" not in text.replace("up to about 1.3 GB", "")


def test_the_descriptions_give_the_disk_sizes_beside_the_download_sizes() -> None:
    """The Desktop question (Sam, 2026-10-14): the choice shows what it takes on disk as
    well as what it downloads, as measured from nothing on macOS, Linux and Windows with conexus
    7.81.0 (2.4 to 3.1 GB for local, 2.1 to 2.3 GB for the CLI alone; nexus-gxzfq), so the user
    is not surprised after answering."""
    text = _tools()["setup"].description
    assert "up to about 1.3 GB to download, 2.4 to 3.1 GB on disk" in text, text
    assert "up to about 0.7 GB to download, 2.1 to 2.3 GB on disk" in text, text


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
    assert "act only on the user's answer" in text


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


def test_the_notice_is_passive_and_asks_nothing_every_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The record's form: what is missing, what fixes it, and that the user decides.
    It does not tell the model to put a question to the user at every server start."""
    _service(monkeypatch, False)
    text = setup_tools.no_service_notice() or ""
    assert "Ask the user whether" not in text and "ask the user" not in text.lower()
    assert "no usable service" in text and "/conexus:setup" in text and "setup tool" in text and "setup_status" in text
    assert "act only on the user's answer" in text


def test_the_notice_gives_the_sizes_a_choice_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-service notice is what a model reads when the real server runs with no
    service: the sizes of the two ways to fix it, download and disk, in the table's words."""
    _service(monkeypatch, False)
    text = setup_tools.no_service_notice() or ""
    assert "1.3 GB" in text and "2.4 to 3.1 GB on disk" in text and "0.7 GB" in text and "2.1 to 2.3 GB on disk" in text, text


def _provision(config: Path, *, files: int = 3) -> None:
    """The files ``nx init`` leaves for a local service, the first *files* of the three."""
    from nexus.daemon.binary_lifecycle import well_known_binary_path

    for path in [config / "pg_credentials", config / "postgres" / "PG_VERSION", well_known_binary_path(config)][:files]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def test_a_provisioned_local_service_that_is_down_is_not_called_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Found in the Windows MVV (nexus-sv2wx.24, row 2): after a reboot with no start at
    login the hook said the installed service was down, and this notice said nothing was
    set up and quoted the 1.3 GB install. In Desktop this text is all the model sees."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    text = setup_tools.no_service_notice() or ""
    assert "not running" in text and "/conexus:setup" in text and "setup tool" in text, text
    assert "act only on the user's answer" in text and "ask the user" not in text.lower()
    assert "no service address is configured" not in text and "1.3 GB" not in text, text


def test_a_partly_provisioned_service_is_still_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Provisioned means all three files (the record, "A usable service")."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path, files=2)
    _service(monkeypatch, False)
    assert "1.3 GB" in (setup_tools.no_service_notice() or "")


def _recorded(config: Path, **status: Any) -> None:
    setup = config / "setup"
    setup.mkdir(parents=True, exist_ok=True)
    (setup / "status.json").write_text(json.dumps(status), encoding="utf-8")


def test_a_recorded_cli_only_choice_keeps_the_instructions_quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sam, 2026-10-15 (MVV row 3): after a finished CLI-only run the hook says one quiet
    line; the server's instructions say nothing, in Desktop too."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _recorded(tmp_path, state="done", mode="cli-only", stage="build")
    _service(monkeypatch, False)
    assert setup_tools.no_service_notice() is None


@pytest.mark.parametrize(("state", "mode"), [("failed", "cli-only"), ("running", "cli-only"), ("done", "local")])
def test_only_a_finished_cli_only_run_quiets_the_instructions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str, mode: str
) -> None:
    """The hook's rule: a failed CLI-only run, a live one, or another mode is not a choice to stop hearing about it."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _recorded(tmp_path, state=state, mode=mode, stage="build")
    _service(monkeypatch, False)
    assert "no usable service" in (setup_tools.no_service_notice() or "")


def test_status_says_a_provisioned_service_is_down_not_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder
) -> None:
    """Found in the Windows MVV (nexus-sv2wx.24, row 2): with no start at login and a
    reboot, /conexus:setup read "Setup is done ... Next: Reconnect" from setup_status and
    started nothing. The answer must say the installed service is not running and that
    setup with mode local starts it, with start at login a choice again."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    recorder.views(status=DONE)
    text = _call("setup_status")
    assert "not running" in text and "mode local" in text and "start at login" in text, text
    assert "downloads nothing" in text and "Reconnect" not in text and "Setup is done" not in text, text


def test_a_doctor_failed_record_over_one_optional_tool_does_not_mask_a_down_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder
) -> None:
    """nexus-36qyh, cascade of the Windows MVV (nexus-sv2wx.24): the doctor-failed record from a box with no
    git outranked "the service is not running, setup starts it" after a reboot, and sent the user to a
    terminal. The view is the real ``status_view`` over a recorded status file."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    _recorded(
        tmp_path, state="doctor-failed", stage="verify", mode="local", autostart=False,
        doctor="fail", doctor_failing=["✗ git: not found on PATH"],
    )
    recorder.views(status=NX_SETUP.status_view(tmp_path / "setup"))
    text = _call("setup_status")
    assert "not running" in text and "mode local" in text and "downloads nothing" in text, text
    assert "nx doctor" not in text and "terminal" not in text, text


def test_a_doctor_failed_record_with_another_failing_row_still_outranks_a_down_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    _recorded(
        tmp_path, state="doctor-failed", stage="verify", mode="local", autostart=False,
        doctor="fail", doctor_failing=["✗ git: not found on PATH", "✗ Schema: behind"],
    )
    recorder.views(status=NX_SETUP.status_view(tmp_path / "setup"))
    text = _call("setup_status")
    assert "nx doctor reported problems" in text and "Schema: behind" in text, text


@pytest.mark.parametrize("record", [{"doctor_failing": []}, {}], ids=["empty-rows", "no-rows-key"])
def test_a_doctor_failed_record_with_no_parsed_rows_does_not_mask_a_down_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder, record: dict[str, Any]
) -> None:
    """nexus-36qyh. A 7.82.0 record for a box with no git is ``doctor-failed`` with ``doctor_failing: []``:
    that release's filter never matched the main sweep's failing row. Nothing says which row failed, so
    the record cannot be read as that one tool alone; it must still not send the user to a terminal while
    the installed service is down. The user is offered the restart instead."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    _recorded(tmp_path, state="doctor-failed", stage="verify", mode="local", autostart=False, doctor="fail", **record)
    recorder.views(status=NX_SETUP.status_view(tmp_path / "setup"))
    text = _call("setup_status")
    assert "not running" in text and "mode local" in text and "downloads nothing" in text, text
    assert "nx doctor" not in text and "terminal" not in text, text


def test_a_doctor_failed_record_with_no_rows_and_no_provisioned_service_keeps_its_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder
) -> None:
    """The restart offer is for an installed service; with none provisioned the record is reported as it was."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _service(monkeypatch, False)
    _recorded(tmp_path, state="doctor-failed", stage="verify", mode="local", autostart=False, doctor="fail", doctor_failing=[])
    recorder.views(status=NX_SETUP.status_view(tmp_path / "setup"))
    assert "nx doctor reported problems" in _call("setup_status")


def test_status_keeps_a_failed_run_ahead_of_a_down_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: Recorder
) -> None:
    """A failed or interrupted last run still names its failure and resume step."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _provision(tmp_path)
    _service(monkeypatch, False)
    recorder.views(status=FAILED)
    text = _call("setup_status")
    assert "the build exited 1." in text and "resumes at the build step" in text, text


@pytest.mark.parametrize("files", [0, 1, 2, 3])
def test_the_cli_and_the_plugin_agree_on_provisioned(tmp_path: Path, files: int) -> None:
    """Two copies of one test (the plugin runs with no nexus package): the server's
    notice and the setup hook must call the same machine provisioned."""
    from nexus.db.pg_provision import local_service_files_present

    _provision(tmp_path, files=files)
    assert local_service_files_present(tmp_path) is NX_SETUP.provisioned(tmp_path) is (files == 3)


def test_the_notice_does_not_start_or_read_the_setup_process(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """Item 5: the not-now marker is not routed through the server start. The
    notice is built from the endpoint alone, so it starts nothing, and not-now says
    on the real server what it does there (see the not-now tests)."""
    from nexus.mcp._first_run import apply_setup_notice

    _service(monkeypatch, False)
    server = _Server()
    with mock.patch.object(setup_tools, "run_bounded") as run:
        assert apply_setup_notice(server) is True
    run.assert_not_called()
    assert recorder.calls() == []


# -- usable_service is the record's definition: resolve_service_endpoint() ----------------

_ENDPOINT_ENV = ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN")


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty config directory, no endpoint variable and no lease."""
    from nexus.db import service_endpoint

    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "endpoint-config"
    config.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (None, None))
    return config


def _lease(monkeypatch: pytest.MonkeyPatch, url: str = "http://127.0.0.1:4599", token: str | None = "lease-token") -> None:
    from nexus.db import service_endpoint

    monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (url, token))


def _saved(config: Path, **credentials: str) -> None:
    body = "credentials:\n" + "".join(f"  {key}: {value}\n" for key, value in credentials.items())
    (config / "config.yml").write_text(body, encoding="utf-8")


@pytest.mark.t2_tenant
def test_nothing_set_is_not_usable(endpoint: Path) -> None:
    assert setup_tools.usable_service() is False


@pytest.mark.t2_tenant
def test_a_service_url_with_no_resolvable_token_is_not_usable(endpoint: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the earlier definition got wrong: every tool that needs storage
    raises RuntimeError there, so setup must not say nothing is missing."""
    from nexus.db.service_endpoint import resolve_service_endpoint

    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    with pytest.raises(RuntimeError):
        resolve_service_endpoint()
    assert setup_tools.usable_service() is False
    monkeypatch.delenv("NX_SERVICE_URL")
    _saved(endpoint, service_url="https://example.invalid")
    assert setup_tools.usable_service() is False


def test_a_service_url_with_a_token_is_usable(endpoint: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    assert setup_tools.usable_service() is True


def test_a_saved_service_url_and_token_are_usable(endpoint: Path) -> None:
    _saved(endpoint, service_url="https://example.invalid", service_token="t")
    assert setup_tools.usable_service() is True


@pytest.mark.t2_tenant
def test_a_service_url_with_a_lease_token_is_usable(endpoint: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    _lease(monkeypatch)
    assert setup_tools.usable_service() is True
    _lease(monkeypatch, token=None)
    assert setup_tools.usable_service() is False


def test_a_service_url_with_a_mint_credential_is_usable(endpoint: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    monkeypatch.setenv("NX_MINT_TOKEN", "m")
    assert setup_tools.usable_service() is True


@pytest.mark.t2_tenant
def test_the_host_port_and_token_variables_are_usable_together_and_not_apart(
    endpoint: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "4599")
    assert setup_tools.usable_service() is False, "no token"
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    assert setup_tools.usable_service() is True
    monkeypatch.delenv("NX_SERVICE_HOST")
    assert setup_tools.usable_service() is True, "the host defaults to 127.0.0.1"
    monkeypatch.setenv("NX_SERVICE_PORT", "not-a-number")
    assert setup_tools.usable_service() is False


def test_a_live_local_lease_is_usable(endpoint: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _lease(monkeypatch)
    assert setup_tools.usable_service() is True


def test_any_failure_to_resolve_is_not_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.db import service_endpoint

    def boom(**_: Any) -> tuple[str, str]:
        raise ValueError("anything")

    monkeypatch.setattr(service_endpoint, "resolve_service_endpoint", boom)
    assert setup_tools.usable_service() is False


def test_usable_service_asks_the_resolver_the_clients_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.db import service_endpoint

    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> tuple[str, str]:
        calls.append(kwargs)
        return "http://x", "t"

    monkeypatch.setattr(service_endpoint, "resolve_service_endpoint", fake)
    assert setup_tools.usable_service() is True
    assert calls == [{}], "no wait budget: the check must not wait for a respawning supervisor"


def test_the_usable_service_docstring_does_not_claim_no_network() -> None:
    """Lease discovery can probe the recorded service's /health on the loopback."""
    doc = (setup_tools.usable_service.__doc__ or "").lower()
    assert "no network request" not in doc and "makes no network" not in doc
    assert "/health" in doc


# -- setup: no script ---------------------------------------------------------------


#: What each mode says when this server cannot start the setup process.
_NO_SCRIPT_TEXT = {
    "local": ("nx init", "nothing was started"),
    "cli-only": ("nothing to install",),
    "not-now": ("nothing was recorded", "do not ask the user again"),
}


@pytest.mark.parametrize("mode", ["local", "cli-only", "not-now"])
def test_without_nx_setup_script_setup_starts_no_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    rec = Recorder(tmp_path / "unused")
    rec.views(status=NONE, start=RUNNING_BUILD, **{"not-now": _view(not_now=True)})
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run, patch_in("nexus.mcp.setup_tools", "subprocess.Popen") as popen, \
            patch_in("nexus.mcp.setup_tools", "subprocess.run") as plain_run:
        text = _call("setup", mode=mode, autostart=True)
    run.assert_not_called()
    popen.assert_not_called()
    plain_run.assert_not_called()
    assert rec.calls() == []
    lowered = text.lower()
    for fragment in _NO_SCRIPT_TEXT[mode]:
        assert fragment in lowered, (mode, fragment, text)
    assert "not started by the conexus plugin's launcher" in lowered or mode == "cli-only", text


def test_without_nx_setup_script_status_starts_no_process_and_gives_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        text = _call("setup_status")
    run.assert_not_called()
    assert "No usable Nexus service" in text and "nx init" in text


def test_a_script_path_that_is_not_a_file_is_treated_as_not_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, str(tmp_path / "nx_setup.py"))
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        text = _call("setup", mode="local")
    run.assert_not_called()
    assert "nx init" in text


def _script_env_case(tmp_path: Path, case: str) -> str:
    """The value of NX_SETUP_SCRIPT for one kind of wrong answer. Each names a real
    file or directory that would answer in the recorder's way, were it started."""
    rec = Recorder(tmp_path / "tools")
    rec.views(status=NONE, start=RUNNING_BUILD, **{"not-now": _view(not_now=True)})
    if case == "wrong basename":
        other = tmp_path / "tools" / "other.py"
        other.write_text(rec.path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(other)
    if case == "right basename in a directory":
        folder = tmp_path / "dir" / "nx_setup.py"
        folder.mkdir(parents=True)
        return str(folder)
    if case == "case differs":
        other = tmp_path / "tools" / "NX_SETUP.PY"
        if other.exists() and other.samefile(rec.path):  # a case-insensitive file system
            pytest.skip("this file system folds case: the name is the same file")
        other.write_text(rec.path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(other)
    if case == "relative":
        return "nx_setup.py"
    if case == "a link named nx_setup.py to another file":
        other = tmp_path / "tools" / "other.py"
        other.write_text(rec.path.read_text(encoding="utf-8"), encoding="utf-8")
        link = tmp_path / "link" / "nx_setup.py"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(other)
        except OSError:
            pytest.skip("this host cannot create a symbolic link")
        return str(link)
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    ["wrong basename", "right basename in a directory", "case differs", "relative", "a link named nx_setup.py to another file"],
)
def test_a_script_variable_that_is_not_an_absolute_nx_setup_py_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str,
) -> None:
    """The variable is inherited by every child of the launcher. Only the plugin's
    own file name, as an absolute path to a file, is ever started."""
    value = _script_env_case(tmp_path, case)
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, value)
    if case == "relative":
        monkeypatch.chdir(tmp_path / "tools")
    _service(monkeypatch, False)
    assert setup_tools.setup_script() is None
    with mock.patch.object(setup_tools, "run_bounded") as run:
        assert "nx init" in _call("setup", mode="local")
        assert "nx init" in _call("setup_status")
    run.assert_not_called()
    assert Recorder(tmp_path / "tools").calls() == []


# -- setup: with the script ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "autostart", "argv"),
    [
        ("local", True, ["start", "--mode", "local", "--autostart"]),
        ("local", False, ["start", "--mode", "local"]),
        ("cloud", False, ["start", "--mode", "cloud"]),
        ("cloud", True, ["start", "--mode", "cloud"]),
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


def test_not_now_promises_nothing_this_server_does_not_do(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """Item 5. On the real server the marker changes nothing the server says: the
    instructions note about a missing service describes the machine, and is built
    without reading the marker. The text says so, and does not claim to stop it."""
    from nexus.mcp._first_run import apply_setup_notice

    _service(monkeypatch, False)
    text = _call("setup", mode="not-now")
    lowered = text.lower()
    assert "stops asking" not in lowered and "stop asking" not in lowered
    assert "not by this server" in lowered and "stays" in lowered, text
    assert "/conexus:setup" in text
    server = _Server()
    assert apply_setup_notice(server) is True, "the notice is unchanged after not-now"
    assert "no usable service" in server._mcp_server.instructions


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


@pytest.mark.parametrize("mode", ["Cloud", "bogus", ""])
def test_unknown_modes_are_refused_and_start_nothing(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, mode: str,
) -> None:
    _service(monkeypatch, False)
    with pytest.raises(ToolError) as caught:
        _call("setup", mode=mode)
    message = str(caught.value)
    assert "local" in message and "cloud" in message and "cli-only" in message and "not-now" in message
    assert recorder.calls() == []


def test_cloud_without_the_setup_script_gives_the_terminal_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real server the launcher did not start has no setup script: cloud says `nx config init`, the wizard
    that asks for the address and token, and starts nothing."""
    _service(monkeypatch, False)
    monkeypatch.delenv(setup_tools.SETUP_SCRIPT_ENV, raising=False)
    text = _call("setup", mode="cloud")
    assert "nx config init" in text and "nothing was started" in text


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


_TROUBLE_STATES = ("doctor-failed", "failed", "interrupted", "unsupported")


@pytest.mark.parametrize("state", _TROUBLE_STATES)
def test_a_run_that_went_wrong_is_reported_even_when_a_service_answers(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, state: str,
) -> None:
    """A service that answers is not proof the last run finished: the log and the
    failing rows are what the user needs, and "nothing is missing" would hide them."""
    _service(monkeypatch, True)
    view = VIEWS[state]
    recorder.views(status=view, start=view)
    for tool, args in (("setup_status", {}), ("setup", {"mode": "local"}), ("setup", {"mode": "not-now"})):
        text = _call(tool, **args)
        assert "nothing is missing" not in text.lower(), (tool, state, text)
        assert view["log"] in text, (tool, state, text)
        # setup will not start while a service answers, so no text may send the
        # model back to it: the next step is the log and nx doctor.
        assert "setup again" not in text.lower() and "to resume" not in text.lower(), (tool, state, text)
        assert "nx doctor" in text, (tool, state, text)
        if view.get("message"):
            assert view["message"] in text
        assert "started nothing" in text.lower() or tool == "setup_status", (tool, text)
    assert all(call[0] == "status" for call in recorder.calls()), "setup must start nothing while a service answers"


def test_a_failed_cloud_doctor_with_a_usable_service_starts_nothing_and_names_no_second_page(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder,
) -> None:
    """Review I2: a saved address with a token is a usable service, so ``setup`` with mode cloud starts nothing
    after a cloud run whose ``nx doctor`` failed. The doctor note the run wrote says the replacement is
    ``nx config init`` in a terminal, and nothing the model is shown says a second call reopens the page."""
    _service(monkeypatch, True)
    view = _view(
        state="doctor-failed", stage="verify", mode="cloud", autostart=False, log="/cfg/setup/setup.log",
        message="nx doctor reported problems.", credentials="credentials saved",
        next=f"Run `nx doctor` in a terminal to see the failing rows.{NX_SETUP._CLOUD_DOCTOR_NOTE} Log: /cfg/setup/setup.log",
    )
    recorder.views(status=view, start=view)
    text = _call("setup", mode="cloud")
    assert "started nothing" in text.lower()
    assert all(call[0] == "status" for call in recorder.calls()), "setup must start nothing while a service answers"
    for said in (text, view["next"]):
        assert "mode cloud again" not in said.lower() and "opens the credentials page again" not in said, said
    assert "nx config init" in view["next"]


@pytest.mark.parametrize("state", ["done", "none"])
def test_a_clean_last_run_is_nothing_missing_when_a_service_answers(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, state: str,
) -> None:
    _service(monkeypatch, True)
    recorder.views(status=VIEWS[state])
    assert "nothing is missing" in _call("setup_status").lower()
    assert "nothing is missing" in _call("setup", mode="local").lower()


def test_a_status_the_script_cannot_give_does_not_hide_a_working_service(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, True)
    recorder.views(_exit=3)
    assert "nothing is missing" in _call("setup_status").lower()
    assert "nothing is missing" in _call("setup", mode="local").lower()


# -- the real server's spawn of the script is not contained ---------------------------------


def test_the_real_servers_spawn_of_the_script_goes_through_no_containment(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder,
) -> None:
    """Ship-blocker (Windows): ``run_bounded`` normally puts the child in a
    KILL_ON_JOB_CLOSE job with no breakaway and closes it on return, which refuses
    ``detach`` and would kill the detached build anyway. The tools ask for no job.
    POSIX has no job to skip, so the observable here is that the containment hook is
    never reached; the survival test below is the behavioural one."""
    from nexus.util import process_group

    reached: list[object] = []
    monkeypatch.setattr(process_group, "contain", lambda proc: reached.append(proc))
    _service(monkeypatch, False)
    _call("setup_status")
    _call("setup", mode="local")
    _call("setup", mode="not-now")
    assert recorder.calls() == [["status"], ["start", "--mode", "local"], ["not-now"]]
    assert reached == [], "the setup script was spawned inside a job object"


def test_the_tools_ask_run_bounded_for_no_containment(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    _service(monkeypatch, False)
    with mock.patch.object(setup_tools, "run_bounded") as run:
        run.return_value.stdout = json.dumps(NONE)
        run.return_value.returncode = 0
        _call("setup_status")
    assert run.call_args.kwargs.get("contained") is False


_GRANDCHILD_SCRIPT = """\
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
code = "import time; time.sleep(1.5); open(%r, 'w').write('alive')" % os.path.join(here, "survived")
subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.stdout.write(json.dumps({"state": "none", "live": False, "terminal": False, "not_now": False, "next": "x"}) + "\\n")
"""


def test_what_the_script_starts_outlives_the_call_that_ran_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The behaviour the detach needs. The script starts a child that sleeps past
    the call's return, as ``nx_setup.py start`` does with its build. On Windows a
    contained script would have the child killed when the call's job closed; the
    marker is written only if it lived."""
    import time

    folder = tmp_path / "grandchild"
    folder.mkdir()
    script = folder / "nx_setup.py"
    script.write_text(_GRANDCHILD_SCRIPT, encoding="utf-8")
    monkeypatch.setenv(setup_tools.SETUP_SCRIPT_ENV, str(script))
    _service(monkeypatch, False)
    _call("setup_status")
    marker = folder / "survived"
    assert not marker.exists(), "the child ran past the call before the check"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.read_text(encoding="utf-8") == "alive", "the child the script started did not survive the call's return"


def test_the_real_servers_setup_detaches_a_run_that_outlives_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End to end through the real ``nx_setup.py``: ``setup`` with mode cli-only
    returns while the stand-in ``uv`` is still building, and the run it detached
    finishes ``done`` afterwards. On Windows a contained call has the detach refused
    (state ``unsupported``, the job has no breakaway) or the build killed when the
    call's job closed; neither happens here."""
    from tests.hooks.test_nx_setup import Box

    box = Box(tmp_path / "box")
    for name, value in (
        ("PATH", str(box.bin)), ("NX_TOOLS_DIR", str(box.tools)), ("NEXUS_CONFIG_DIR", str(box.config)),
        ("HOME", str(box.home)), ("USERPROFILE", str(box.home)), ("FAKE_LOG", str(box.log)),
        ("FAKE_BEHAVE", str(box.behave_file)), (setup_tools.SETUP_SCRIPT_ENV, str(_NX_SETUP)),
    ):
        monkeypatch.setenv(name, value)
    for name in ("NX_CONFIG_DIR", *_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    box.behave([box.uv_builds_nx(sleep=2.0)])
    _service(monkeypatch, False)
    text = _call("setup", mode="cli-only")
    assert "Setup started in mode cli-only" in text, text
    assert "unsupported" not in text.lower() and "cannot run on this host" not in text.lower(), text
    assert box.status_file()["state"] == "running", "the call returned after the run, not while it ran"
    done = box.wait_terminal(120)
    assert done["state"] == "done", (done, box.read_log())
    assert [call["args"][:2] for call in box.calls("uv")][:1] == [["tool", "run"]], box.argvs()


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


@pytest.mark.t2_tenant
def test_status_through_the_real_script_in_an_empty_config_directory(real_script: Path) -> None:
    text = _call("setup_status")
    assert "No usable Nexus service" in text
    assert not (real_script / "setup").exists(), "status created the setup directory"


@pytest.mark.t2_tenant
def test_not_now_through_the_real_script_writes_its_marker_and_nothing_else(real_script: Path) -> None:
    text = _call("setup", mode="not-now")
    assert "nothing was installed" in text.lower()
    assert sorted(p.name for p in (real_script / "setup").iterdir()) == ["not-now"]


@pytest.mark.t2_tenant
def test_cloud_through_the_real_script_starts_a_run_that_serves_the_page(
    real_script: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Mode cloud reaches the real setup process like any other. The sandbox has no ``uv``, so the run serves its
    page, asks the system to open it (recorded: no browser starts) and fails at the build."""
    empty, home, record = tmp_path / "empty", tmp_path / "home", tmp_path / "open.record"
    empty.mkdir()
    home.mkdir()
    for name, value in (
        ("PATH", str(empty)), ("HOME", str(home)), ("USERPROFILE", str(home)), ("NX_TOOLS_DIR", str(tmp_path / "tools")),
        ("NX_SETUP_OPEN_RECORD", str(record)),
    ):
        monkeypatch.setenv(name, value)
    text = _call("setup", mode="cloud")
    assert "Setup started in mode cloud" in text, text
    status = real_script / "setup" / "status.json"
    deadline = time.monotonic() + 30
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("state") == "failed":
            break
        time.sleep(0.05)
    assert data.get("state") == "failed" and data["failed_step"] == "build" and data["credentials"] == "waiting for credentials", data
    assert record.read_text(encoding="utf-8").strip().endswith("credentials-opener.html")
    assert not (real_script / "setup" / "credentials-opener.html").exists()


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


# -- consent in the text the tools return --------------------------------------------------

#: A sentence that tells the model to call ``setup`` (not ``setup_status``) and does not
#: forbid it.
_CALL_SETUP = re.compile(r"(?<!not )(?<!n't )\bcall (?:the )?`?setup\b(?!_)", re.IGNORECASE)
#: The clause that makes such a sentence conditional on the user.
_ASK_FIRST = re.compile(
    r"ask the user|if the user (?:agrees|asks|says|chooses|changes)|if they agree|when the user|only if the user|"
    r"the user(?:'s)? (?:answer|choice)",
    re.IGNORECASE,
)


def _string_literals(path: Path) -> list[str]:
    """Every string a module can hand to the model, docstrings excluded; an f-string
    is its constant parts with ``{}`` for each field."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append(node.value)
    return found


def _unconsented(texts: list[str]) -> list[str]:
    return [t for t in texts if _CALL_SETUP.search(t) and not _ASK_FIRST.search(t)]


def test_no_string_in_the_three_files_tells_the_model_to_call_setup_without_the_ask_first_clause() -> None:
    """Item 4. The ``next`` strings in ``nx_setup.py`` (the none, interrupted,
    failed, error and refused-mode texts), the renderers and the tool text. The
    scan is over source, so a string no test happens to render is covered too."""
    for path in (_NX_SETUP, _STANDIN, _TOOLS_SOURCE):
        texts = _string_literals(path)
        assert any(_CALL_SETUP.search(t) for t in texts), f"{path.name}: the scan found no 'call setup' string at all"
        assert _unconsented(texts) == [], path.name


def test_the_scan_sees_a_string_that_lacks_the_clause() -> None:
    assert _unconsented(["Call setup again."]) == ["Call setup again."]
    assert _unconsented(["If the user agrees, call setup again."]) == []
    assert _unconsented(["Call setup_status to follow it."]) == []
    assert _unconsented(["do not call setup again while it runs"]) == []


@pytest.mark.parametrize("server", ["real", "stand-in"])
@pytest.mark.parametrize("started", [None, True, False])
@pytest.mark.parametrize("autostart", [True, False])
@pytest.mark.parametrize("state", sorted(VIEWS))
def test_no_rendered_view_tells_the_model_to_call_setup_without_the_clause(
    standin_module: Any, nx_setup_module: Any, state: str, autostart: bool, started: bool | None, server: str,
) -> None:
    render = setup_tools.render if server == "real" else standin_module.render
    view = {**VIEWS[state], "autostart": autostart}
    text = render(view, started=started)
    assert _unconsented(text.split("\n")) == [], text


def test_unsupported_is_not_offered_as_resumable() -> None:
    """A second setup on a host that cannot detach meets the same refusal."""
    assert "unsupported" not in setup_tools._RESUMABLE


@pytest.mark.parametrize("server", ["real", "stand-in"])
@pytest.mark.parametrize("state", [s for s in sorted(VIEWS) if s not in ("none", "error")])
def test_every_view_with_a_recorded_choice_shows_the_mode_and_the_autostart(
    standin_module: Any, state: str, server: str,
) -> None:
    render = setup_tools.render if server == "real" else standin_module.render
    for autostart, shown in ((True, "autostart yes"), (False, "autostart no")):
        view = {**VIEWS[state], "autostart": autostart}
        text = render(view)
        assert f"Recorded choice: mode {view['mode']}, {shown}." in text, text
        if state in ("failed", "interrupted", "doctor-failed"):
            assert "ask the user first" in text and "same choice" in text and f"mode {view['mode']}" in text, text
            assert ("autostart true" in text) is autostart, text


@pytest.mark.parametrize("server", ["real", "stand-in"])
def test_a_view_with_no_recorded_choice_shows_none(standin_module: Any, server: str) -> None:
    render = setup_tools.render if server == "real" else standin_module.render
    for state in ("none", "error"):
        assert "Recorded choice" not in render(VIEWS[state])


def test_the_real_nx_setup_views_pass_the_clause_check(nx_setup_module: Any, tmp_path: Path) -> None:
    """The ``none`` and ``interrupted`` texts as the script computes them, not as
    the test copies them."""
    none = nx_setup_module.status_view(tmp_path / "empty")
    assert _unconsented([none["next"]]) == []
    setup = tmp_path / "setup"
    setup.mkdir()
    nx_setup_module.write_status(setup, {"state": "running", "stage": "init", "mode": "local", "autostart": True})
    interrupted = nx_setup_module.status_view(setup)
    assert interrupted["state"] == "interrupted"
    assert _unconsented([interrupted["next"]]) == []
    assert "same mode" in interrupted["next"]


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
    assert not hasattr(setup_tools, "CLOUD_REFUSAL") and not hasattr(standin_module, "CLOUD_REFUSAL")
    assert setup_tools.NOT_NOW_TEXT == standin_module.NOT_NOW_TEXT
    assert "cloud" in setup_tools.MODES and setup_tools.mode_refusal("Cloud") == standin_module.mode_refusal("Cloud")
    assert setup_tools.mode_refusal("bogus") == standin_module.mode_refusal("bogus")
    assert setup_tools._DEFAULT_WAIT == standin_module._DEFAULT_WAIT
    assert setup_tools._DOCTOR_ROWS_SHOWN == standin_module._DOCTOR_ROWS_SHOWN
    assert setup_tools._HEADS == standin_module._HEADS
    assert setup_tools._RESUMABLE == standin_module._RESUMABLE


@pytest.mark.parametrize("started", [None, True])
def test_both_servers_agree_on_an_unknown_stage_and_a_long_doctor_list(standin_module: Any, started: bool | None) -> None:
    """The generic wait for a stage the table lacks, and the cap on listed rows,
    were in neither earlier comparison: each copy could drift alone."""
    rows = [f"[x] row {n}" for n in range(12)]
    view = {**RUNNING_BUILD, "stage": "a-stage-the-table-lacks", "doctor_failing": rows}
    real, standin = setup_tools.render(view, started=started), standin_module.render(view, started=started)
    assert real == standin
    assert "no sooner than 30 seconds" in real and "a minute or so" in real
    assert real.count("[x] row") == setup_tools._DOCTOR_ROWS_SHOWN == 10
    assert "[x] row 10" not in real


@pytest.fixture
def standin_with(standin_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    """A recording script wired into the STAND-IN, which finds its script beside
    itself and so is pointed at one through its ``setup_script``."""
    rec = Recorder(tmp_path / "standin-recorder")
    monkeypatch.setattr(standin_module, "setup_script", lambda: str(rec.path))
    return rec


_NO_SERVICE_PREFIX = "No usable Nexus service was found on this machine.\n"


def _answer(call: Any) -> tuple[str, str]:
    """("ok" | "error", text) for one tool call, whichever exception class it raises."""
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 -- each server has its own SetupError class
        if type(exc).__name__ != "SetupError":
            raise
        return "error", str(exc)


@pytest.mark.parametrize("state", sorted(VIEWS))
def test_both_servers_answer_setup_status_alike_for_every_state(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, standin_with: Recorder, standin_module: Any, state: str,
) -> None:
    """Behaviour, not constants: the same script prints the same view and each
    server's ``setup_status`` is read.

    INTENTIONALLY DIFFERENT, and removed before comparing: the real server first
    asks whether a service is usable and, finding none, leads with "No usable Nexus
    service was found on this machine."; the stand-in has no such question and,
    for a ``none`` state, leads with "Nexus is not installed". Everything after the
    lead (stage, message, doctor rows, choice, next, log, wait) must be identical.
    The real server alone says "Nothing is missing" for a usable service, and its
    answer for a script it cannot start is the terminal commands for a machine with
    a CLI; the stand-in's is the install commands for a machine without one.

    The stand-in asks the plugin's lookup at each poll (nexus-sv2wx.17), so it is pinned
    here and the machine's own CLI cannot decide the answer. It is "no CLI resolves",
    the state the stand-in is served in, except for ``done``: a ``done`` with no CLI found
    is an install that is gone (``nx_standin._missing``), so ``done`` is compared where it
    is a finished setup, the poll after the build while the stand-in still serves and the
    CLI now resolves."""
    _service(monkeypatch, False)
    kind = "startable" if state == "done" else "missing"
    monkeypatch.setattr(standin_module, "cli_state", lambda: (kind, None))
    view = VIEWS[state]
    for rec in (recorder, standin_with):
        rec.views(status=view)
    real = _answer(setup_tools.run_status)
    standin = _answer(standin_module.run_status)
    assert real[0] == standin[0], (real, standin)
    real_text = real[1].removeprefix(_NO_SERVICE_PREFIX)
    standin_text = standin[1].removeprefix(standin_module.NOT_INSTALLED + "\n")
    assert real_text == standin_text, (state, real_text, standin_text)
    assert recorder.calls() == standin_with.calls() == [["status"]]


_SETUP_CASES = [
    ("local", True, ["start", "--mode", "local", "--autostart"]),
    ("local", False, ["start", "--mode", "local"]),
    ("cli-only", True, ["start", "--mode", "cli-only"]),
    ("not-now", False, ["not-now"]),
]


@pytest.mark.parametrize(("mode", "autostart", "argv"), _SETUP_CASES)
@pytest.mark.parametrize("state", sorted(VIEWS))
def test_both_servers_answer_setup_alike_for_every_state_and_start_the_same_command(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, standin_with: Recorder, standin_module: Any,
    state: str, mode: str, autostart: bool, argv: list[str],
) -> None:
    """Same recording script on both, same view for the command; the text and the
    command line each server produced are equal. Intentionally different, and not
    exercised here: the real server answers "Nothing is missing" first when a
    service is usable (``_service`` pins it unusable) and has no script-less path
    that matches the stand-in's. Argument validation is the framework's on the real
    server and hand-rolled in the stand-in (tests above)."""
    _service(monkeypatch, False)
    view = {**VIEWS[state], "started": state == "running"}
    for rec in (recorder, standin_with):
        rec.views(status=NONE, start=view, **{"not-now": view})
    real = _answer(lambda: setup_tools.run_setup(mode, autostart))
    standin = _answer(lambda: standin_module.run_setup({"mode": mode, "autostart": autostart}))
    assert real == standin, (real, standin)
    assert recorder.calls() == standin_with.calls() == [argv]


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

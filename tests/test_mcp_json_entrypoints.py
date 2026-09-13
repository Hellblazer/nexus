# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""conexus/.mcp.json's declared server commands resolve to the intended
console-script entry points (nexus-cnzei.5, nexus-cnzei.1 critic-pass
finding 3, T2 nexus/cnzei1-critic-pass-2026-09-13).

Finding 3 flagged this as a pre-existing, orthogonal gap: no test parsed
``.mcp.json`` against the packaged entry points, or asserted the declared
command resolves to the intended module. This is that test -- config/
deployment-layer wiring, not application code, but still load-bearing: a
``command`` in ``.mcp.json`` that no longer matches a real
``[project.scripts]`` entry (or that resolves to the wrong module) breaks
every session that loads the plugin, silently, with no test failure
anywhere else in the suite.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import tomllib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP_JSON = _REPO_ROOT / "conexus" / ".mcp.json"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Servers in conexus/.mcp.json backed by THIS repo's own console scripts
#: (as opposed to an external package like sequential-thinking's npx
#: invocation, which has no pyproject.toml entry to check against).
_LOCAL_SERVERS = {
    "nexus": "nx-mcp",
    "nexus-catalog": "nx-mcp-catalog",
}

#: The module each local server's console script MUST resolve to. A drift
#: here (command renamed to point at the wrong module) would silently wire
#: a session to the wrong tool set.
_EXPECTED_MODULE = {
    "nexus": "nexus.mcp.core",
    "nexus-catalog": "nexus.mcp.catalog",
}


def _load_mcp_json() -> dict:
    return json.loads(_MCP_JSON.read_text())


def _load_console_scripts() -> dict[str, str]:
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["scripts"]


def test_mcp_json_exists() -> None:
    assert _MCP_JSON.is_file(), f"expected {_MCP_JSON}"


def test_every_local_server_command_is_a_declared_console_script() -> None:
    mcp_json = _load_mcp_json()
    scripts = _load_console_scripts()
    for server_name, expected_command in _LOCAL_SERVERS.items():
        assert server_name in mcp_json, f"{server_name!r} missing from {_MCP_JSON.relative_to(_REPO_ROOT)}"
        actual_command = mcp_json[server_name]["command"]
        assert actual_command == expected_command, (
            f"{server_name!r}'s command in .mcp.json is {actual_command!r}, "
            f"expected {expected_command!r} (this test's own fixture is stale, "
            "or .mcp.json drifted)"
        )
        assert actual_command in scripts, (
            f".mcp.json declares command {actual_command!r} for server "
            f"{server_name!r}, but pyproject.toml [project.scripts] has no "
            f"such entry point -- the plugin would fail to spawn this server"
        )


def test_console_script_target_resolves_to_the_intended_module_and_callable() -> None:
    scripts = _load_console_scripts()
    for server_name, command in _LOCAL_SERVERS.items():
        target = scripts[command]  # e.g. "nexus.mcp.core:main"
        module_path, _, func_name = target.partition(":")
        assert module_path == _EXPECTED_MODULE[server_name], (
            f"console script {command!r} points at module {module_path!r}, "
            f"expected {_EXPECTED_MODULE[server_name]!r} for server "
            f"{server_name!r} -- .mcp.json's command would spawn the wrong "
            "tool set"
        )
        mod = importlib.import_module(module_path)
        assert hasattr(mod, func_name), (
            f"{module_path}.{func_name} (the {command!r} entry point) does "
            "not exist"
        )
        assert callable(getattr(mod, func_name))


def test_console_script_module_exposes_the_expected_fastmcp_server() -> None:
    """The resolved module's ``mcp`` object is a FastMCP server with the
    server name .mcp.json's client-facing key implies (RDR-062 split)."""
    for server_name, module_path in _EXPECTED_MODULE.items():
        mod = importlib.import_module(module_path)
        assert hasattr(mod, "mcp"), f"{module_path} has no module-level `mcp` FastMCP instance"
        assert mod.mcp.name == server_name, (
            f"{module_path}.mcp.name is {mod.mcp.name!r}, expected "
            f"{server_name!r} to match its .mcp.json server key"
        )


def test_a_planted_command_mismatch_is_detected() -> None:
    """The comparison logic actually distinguishes a match from a drift."""
    scripts = _load_console_scripts()
    assert scripts.get("nx-mcp") != scripts.get("nx-mcp-catalog"), (
        "nx-mcp and nx-mcp-catalog resolve to the SAME target -- either the "
        "fixture is degenerate or the two servers are no longer distinct, "
        "either of which would make the equality checks above vacuous"
    )

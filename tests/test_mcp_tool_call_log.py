# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2nxoo (shakeout F18): one ``mcp_tool_call`` event per tools/call.

Nobody could tell whether a client (the Claude Desktop remote-devices bridge)
sends per-request ``_meta`` such as a session id, because nx-mcp logged no
tool calls. The hook is a ``Server.middleware`` entry (mcp 2.x; nexus-sgmyl
replaced the 1.x ``request_handlers[CallToolRequest]`` wrap) that acts on
``tools/call`` and logs the tool name plus the SORTED KEYS of ``_meta``. Never values, never arguments: they can carry tokens.
The event is INFO the first time a key-set is seen in the process and DEBUG
after (Claude Code sends ``claudecode/toolUseId`` on every call), carries the
process ``pid``, and caps the keys it lists.

These tests drive a real MCPServer through a real in-process client
(``tests/_mcp_inproc.py``), the same path ``tests/test_stale_host.py`` uses. The
two production servers have lifespans that open T1, so their wiring is checked by
running the installed middleware entry directly (``_through_middleware``).
"""
from __future__ import annotations

import logging
import os

import anyio
import pytest
import structlog
from mcp.server.mcpserver import MCPServer
from structlog.testing import capture_logs

from nexus.mcp import _tool_call_log
from tests._mcp_inproc import call_tool

_SECRET = "s3cr3t-token-value"


@pytest.fixture(autouse=True)
def _fresh_keyset_memory():
    _tool_call_log._reset_state()
    yield
    _tool_call_log._reset_state()


@pytest.fixture(autouse=True)
def _debug_level():
    """The suite filters structlog at WARNING; the event is DEBUG. conftest's
    autouse ``_restore_structlog_after_test`` puts the config back."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


def _mk_server() -> MCPServer:
    server = MCPServer("test-tool-call-log")

    @server.tool(description="echo")
    def echo(value: str) -> str:
        return value

    return server


def _call(server: MCPServer, tool: str, meta: dict | None = None):
    """One tools/call through the real client -> dispatcher -> middleware path."""
    return call_tool(server, tool, {"value": _SECRET}, meta=meta)


def _through_middleware(server: MCPServer, tool: str, meta: dict | None = None):
    """Run the server's installed tool-call-log entry on a hand-built context.

    For a server whose lifespan a test must not start. The stub ``call_next``
    stands for the rest of the chain; what the entry logs is what it read off
    ``ctx``.
    """
    from types import SimpleNamespace

    entry = next(m for m in server.middleware if getattr(m, "_nx_tool_call_log", False))
    params: dict = {"name": tool, "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    ctx = SimpleNamespace(method="tools/call", params=params)

    async def _next(_ctx):
        return {"content": [], "isError": False}

    return anyio.run(entry, ctx, _next)


def _events(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["event"] == "mcp_tool_call"]


class TestToolCallLog:
    def test_meta_keys_logged_sorted_without_values(self):
        server = _mk_server()
        assert _tool_call_log.install_tool_call_log(server) == "applied"
        with capture_logs() as logs:
            result = _call(server, "echo", {"progressToken": 7, "a": _SECRET})
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["tool"] == "echo"
        assert events[0]["meta_keys"] == ["a", "progressToken"]
        assert events[0]["log_level"] == "info", "a first-seen key-set must reach mcp.log at the default INFO level"
        # keys only: no value and no argument anywhere in any event
        assert _SECRET not in repr(logs)
        # the call itself is untouched
        assert result.is_error is False

    def test_no_meta_logs_empty_list(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["meta_keys"] == []

    def test_first_sight_of_a_key_set_is_info_then_debug(self):
        """Claude Code sends claudecode/toolUseId on EVERY call: the first
        logs at INFO, the rest at DEBUG, so a steady client adds one INFO
        line per process, not per call."""
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            for i in range(5):
                _call(server, "echo", {"claudecode/toolUseId": "toolu_" + str(i)})
        assert [(e["meta_keys"], e["log_level"]) for e in _events(logs)] == [
            (["claudecode/toolUseId"], "info"),
            (["claudecode/toolUseId"], "debug"),
            (["claudecode/toolUseId"], "debug"),
            (["claudecode/toolUseId"], "debug"),
            (["claudecode/toolUseId"], "debug"),
        ]

    def test_a_new_key_set_logs_info_again(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo", {"claudecode/toolUseId": "t1"})
            _call(server, "echo", {"claudecode/toolUseId": "t2"})
            _call(server, "echo", {"claudecode/toolUseId": "t3", "session": _SECRET})
            _call(server, "echo", {"session": _SECRET, "claudecode/toolUseId": "t4"})
            _call(server, "echo")
            _call(server, "echo")
        assert [(e["meta_keys"], e["log_level"]) for e in _events(logs)] == [
            (["claudecode/toolUseId"], "info"),
            (["claudecode/toolUseId"], "debug"),
            (["claudecode/toolUseId", "session"], "info"),
            (["claudecode/toolUseId", "session"], "debug"),  # key order does not matter
            ([], "info"),
            ([], "debug"),
        ]

    def test_the_remembered_set_is_bounded_and_says_so_once(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        limit = _tool_call_log._MAX_KEYSETS
        with capture_logs() as logs:
            for i in range(limit + 10):
                _call(server, "echo", {"k" + str(i): 1})
            _call(server, "echo", {"k0": 1})  # remembered: DEBUG
            _call(server, "echo", {"k" + str(limit + 3): 1})  # never remembered: DEBUG
        events = _events(logs)
        assert [e["log_level"] for e in events].count("info") == limit
        assert all(e["log_level"] == "debug" for e in events[limit:])
        assert len(_tool_call_log._seen_keysets) == limit
        notes = [e for e in logs if e["event"] == "mcp_tool_call_keyset_limit"]
        assert len(notes) == 1, "the full-set notice is emitted once"
        assert notes[0]["log_level"] == "info"
        assert notes[0]["limit"] == limit
        assert notes[0]["pid"] == os.getpid()

    def test_every_event_carries_the_process_pid(self):
        """mcp.log is shared by many nx-mcp processes; without pid a line
        cannot be tied to the process the patch status line describes."""
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
            _call(server, "echo", {"x": 1})
        assert [e["pid"] for e in _events(logs)] == [os.getpid(), os.getpid()]

    def test_logged_keys_are_capped(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        many = {"key" + format(i, "03d"): _SECRET for i in range(_tool_call_log._MAX_KEYS + 8)}
        long_key = "k" * (_tool_call_log._MAX_KEY_LEN + 50)
        with capture_logs() as logs:
            _call(server, "echo", many)
            _call(server, "echo", {long_key: _SECRET})
            _call(server, "echo", {"a": 1})
        many_ev, long_ev, plain_ev = _events(logs)
        assert len(many_ev["meta_keys"]) == _tool_call_log._MAX_KEYS
        assert many_ev["meta_keys"][0] == "key000"
        assert many_ev["meta_keys_truncated"] is True
        assert many_ev["meta_keys_total"] == _tool_call_log._MAX_KEYS + 8
        assert long_ev["meta_keys"] == ["k" * _tool_call_log._MAX_KEY_LEN]
        assert long_ev["meta_keys_truncated"] is True
        assert "meta_keys_truncated" not in plain_ev
        assert _SECRET not in repr(logs)

    def test_one_event_per_call(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
            _call(server, "echo", {"x": 1})
        assert [e["meta_keys"] for e in _events(logs)] == [[], ["x"]]

    def test_raising_logger_does_not_break_the_call(self, monkeypatch):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)

        seen: list[object] = []

        def _boom(req):
            seen.append(req)
            raise RuntimeError("logging backend down")

        monkeypatch.setattr(_tool_call_log, "_emit", _boom)
        result = _call(server, "echo", {"a": 1})
        assert len(seen) == 1  # the hook ran, raised, and the call still completed
        assert result.is_error is False
        assert result.content[0].text == _SECRET

    def test_install_degrades_on_unverifiable_shape(self):
        class _NoLow:
            pass

        status = _tool_call_log.install_tool_call_log(_NoLow())
        assert status.startswith("unavailable")

    def test_install_is_idempotent(self):
        server = _mk_server()
        assert _tool_call_log.install_tool_call_log(server) == "applied"
        assert _tool_call_log.install_tool_call_log(server) == "already applied"
        with capture_logs() as logs:
            _call(server, "echo")
        assert len(_events(logs)) == 1


class TestComposesWithStaleHost:
    """core wraps this hook at import and ``_stale_host`` wraps it again in
    main(); the catalog server is wired in main() the same way. Both orders
    must keep one event per call and the stale host's pass-through."""

    @staticmethod
    def _patch_install(monkeypatch, tmp_path):
        from nexus import upgrade_finish

        missing = tmp_path / "conexus-fake.dist-info"  # never created
        monkeypatch.setattr(
            upgrade_finish, "install_dist_info", lambda: (1000.0, "6.14.0", missing)
        )
        monkeypatch.setattr(
            upgrade_finish, "install_mtime_and_version", lambda: (1000.0, "6.14.0")
        )

    @pytest.mark.parametrize("log_first", [True, False], ids=["log-inner", "log-outer"])
    def test_both_orders_log_once_and_pass_the_call(self, monkeypatch, tmp_path, log_first):
        from nexus.mcp import _stale_host

        self._patch_install(monkeypatch, tmp_path)
        server = _mk_server()
        if log_first:
            assert _tool_call_log.install_tool_call_log(server) == "applied"
            assert _stale_host.install_stale_host_hook(server) is True
        else:
            assert _stale_host.install_stale_host_hook(server) is True
            assert _tool_call_log.install_tool_call_log(server) == "applied"
        with capture_logs() as logs:
            result = _call(server, "echo", {"claudecode/toolUseId": "t1"})
        assert result.is_error is False
        assert result.content[0].text == _SECRET
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["meta_keys"] == ["claudecode/toolUseId"]
        assert events[0]["pid"] == os.getpid()


class TestCoreWiring:
    def test_nexus_server_middleware_is_installed(self):
        """Asserted by behaviour: whatever else main() layered on (the stale-host
        entry, installed after this one in production order) the chain must still
        emit the event for a ``tools/call`` and ignore other methods."""
        from nexus.mcp.core import _SDK_PATCH_RESULTS, mcp

        assert _SDK_PATCH_RESULTS["tool_call_log"] == "applied"
        with capture_logs() as logs:
            _through_middleware(mcp, "nexus_no_such_tool_probe", {"claudecode/toolUseId": "t1"})
        events = _events(logs)
        assert len(events) == 1, logs
        assert events[0]["tool"] == "nexus_no_such_tool_probe"
        assert events[0]["meta_keys"] == ["claudecode/toolUseId"]

    def test_only_tools_call_is_logged(self):
        """The chain sees every inbound message; ``tools/list`` must log nothing."""
        from types import SimpleNamespace

        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        entry = next(m for m in server.middleware if getattr(m, "_nx_tool_call_log", False))

        async def _next(_ctx):
            return {}

        with capture_logs() as logs:
            anyio.run(entry, SimpleNamespace(method="tools/list", params={}), _next)
        assert _events(logs) == []

    def test_core_reports_pid_with_the_patch_statuses(self):
        assert _report_call_passes_pid("core.py")


class TestCatalogWiring:
    def test_catalog_server_middleware_is_installed_and_reported_as_its_own(self):
        """The catalog MCPServer is a different object from core's: reporting
        core's tool_call_log status for it said "applied" over an unwrapped
        server."""
        from nexus.mcp import catalog, core

        assert catalog.mcp is not core.mcp
        entries = [m for m in catalog.mcp.middleware if getattr(m, "_nx_tool_call_log", False)]
        assert len(entries) == 1
        assert catalog._TOOL_CALL_LOG_STATUS == "applied"

    def test_catalog_middleware_logs_calls(self):
        from nexus.mcp import catalog

        with capture_logs() as logs:
            _through_middleware(catalog.mcp, "no_such_tool", {"claudecode/toolUseId": "t"})
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["tool"] == "no_such_tool"
        assert events[0]["pid"] == os.getpid()

    def test_catalog_reports_pid_with_the_patch_statuses(self):
        assert _report_call_passes_pid("catalog.py")


def _report_call_passes_pid(module: str) -> bool:
    """main() hands report_sdk_patches ``pid=os.getpid()``, so the patch status
    line and the mcp_tool_call lines can be tied to one process."""
    import ast
    import pathlib

    src = pathlib.Path(_tool_call_log.__file__).parent / module
    tree = ast.parse(src.read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "main":
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "report_sdk_patches"
                ):
                    return any(
                        kw.arg == "pid" and ast.unparse(kw.value) == "os.getpid()"
                        for kw in node.keywords
                    )
    raise AssertionError(module + ": main() never calls report_sdk_patches")


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="Windows refuses to delete a process's cwd")
class TestDeletedCwdHeal:
    """nexus-x6qc7 (shakeout F19): Claude Desktop deletes a plugin's install
    directory on update while its MCP servers run there. The wrap ahead of every
    tool call moves the process off it, so no tool's subprocess inherits it."""

    def test_a_call_moves_the_process_off_a_deleted_cwd_and_says_so_once(self, tmp_path, monkeypatch):
        from nexus.util.stable_cwd import stable_daemon_cwd

        from nexus.util import stable_cwd

        monkeypatch.setattr(stable_cwd, "_healed_from_deleted", False)
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        gone = tmp_path / "plugin_x.old.1"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        again = tmp_path / "plugin_x.old.2"
        with capture_logs() as logs:
            result = _call(server, "echo")
            # A second deletion heals again; the event still logs once per process.
            again.mkdir()
            os.chdir(again)
            again.rmdir()
            _call(server, "echo")
        assert result.is_error is False
        assert os.getcwd() == stable_daemon_cwd()
        healed = [e for e in logs if e["event"] == "mcp_cwd_deleted_healed"]
        assert len(healed) == 1, logs

    def test_an_existing_cwd_is_left_alone(self, tmp_path, monkeypatch):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        monkeypatch.chdir(tmp_path)
        with capture_logs() as logs:
            _call(server, "echo")
        assert os.getcwd() == str(tmp_path.resolve())
        assert not [e for e in logs if e["event"] == "mcp_cwd_deleted_healed"]

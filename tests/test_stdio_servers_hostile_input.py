# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plugin's two stdlib-only MCP servers survive hostile stdio without a word on stderr.

nexus-ing7a: ``conexus/mcp/sequential_thinking.py`` and the setup stand-in
(``conexus/launch/nx_standin.py``, served by ``nx_launch.py`` when there is no
CLI) share one stdio loop, ``conexus/launch/_stdio_loop.py``. One suite, run
against both, replaces the copies this repo kept for each (nexus-rwv5i,
nexus-sv2wx.6). Claude Code records every stderr line of a stdio MCP server as an
error, and a traceback also ends the server. Each case runs the server in a
subprocess, as Claude Code does.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests._platform import scrubbed_env

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "conexus"
_SEQ = _PLUGIN / "mcp" / "sequential_thinking.py"
_LAUNCH = _PLUGIN / "launch" / "nx_launch.py"
_STANDIN = _PLUGIN / "launch" / "nx_standin.py"
_LOOP = _PLUGIN / "launch" / "_stdio_loop.py"

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="closes a standard descriptor through sh")


def _not_json(constant: str) -> float:
    raise ValueError(f"{constant} is not JSON")


def _strict_loads(line: str):
    """``json.loads`` as a client reads a reply: ``NaN`` and ``Infinity`` are
    Python's extension, not JSON, and a strict parser drops the line."""
    return json.loads(line, parse_constant=_not_json)


def _env(tmp_path: Path) -> dict[str, str]:
    """A minimal child environment: an empty ``PATH`` directory and tools root, so
    the launcher finds no ``nx-mcp`` (it serves the stand-in) and never the
    developer's own install, and no case can start a browser."""
    dirs = {name: tmp_path / name for name in ("home", "tools", "bin", "config", "project")}
    for d in dirs.values():
        d.mkdir(exist_ok=True)
    return scrubbed_env(
        PATH=str(dirs["bin"]),
        NX_TOOLS_DIR=str(dirs["tools"]),
        NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]),
        USERPROFILE=str(dirs["home"]),
        CLAUDE_PROJECT_DIR=str(dirs["project"]),
        NX_SETUP_OPEN_RECORD=str(tmp_path / "open.record"),
        NX_NO_TELEMETRY="1",
    )


@dataclass(frozen=True)
class Server:
    """One way to start one of the two servers: its argv, its environment, and the
    ``(script, main arguments)`` a wrapper needs to run it with a handler replaced."""

    name: str
    argv: tuple[str, ...]
    env: dict[str, str]
    script: Path
    server_name: str
    main_args: tuple[str, ...] = ()

    def run(self, payload: bytes) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(list(self.argv), input=payload, capture_output=True, env=self.env, timeout=60, check=False)

    def popen(self, **kwargs) -> subprocess.Popen[bytes]:
        return subprocess.Popen(list(self.argv), env=self.env, **kwargs)


def _sequential_thinking(tmp_path: Path) -> Server:
    return Server("sequential-thinking", (sys.executable, str(_SEQ)), _env(tmp_path), _SEQ, "sequential-thinking-server")


def _stand_in(tmp_path: Path) -> Server:
    return Server("stand-in", (sys.executable, str(_LAUNCH), "nx-mcp"), _env(tmp_path), _STANDIN, "nexus-not-installed", ("nx-mcp",))


@pytest.fixture(params=[_sequential_thinking, _stand_in], ids=["sequential-thinking", "stand-in"])
def server(request: pytest.FixtureRequest, tmp_path: Path) -> Server:
    return request.param(tmp_path)


_PING = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode() + b"\n"

_HOSTILE = {
    "bytes that are not UTF-8": b'{"jsonrpc":"2.0","id":1,"method":"pi\xffng"}\n',
    "nesting past the recursion limit": b"[" * 200_000 + b"\n",
    "an integer too long to parse": b'{"jsonrpc":"2.0","id":' + b"9" * 5000 + b',"method":"ping"}\n',
    "a lone surrogate in the id": b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n',
    "an id that is not JSON": b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
}


def _replies(r: subprocess.CompletedProcess[bytes]) -> list[dict]:
    return [_strict_loads(reply) for reply in r.stdout.decode("utf-8").splitlines()]


def test_each_fixture_starts_the_server_it_names(server: Server) -> None:
    """The hostile-input cases prove nothing about a server that was not the one
    started: the launcher serves the stand-in only when no CLI is found, and a
    developer's own install would be answered instead. ``initialize`` names who
    answered."""
    initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode() + b"\n"
    r = server.run(initialize)
    assert r.returncode == 0
    assert r.stderr == b""
    (reply,) = _replies(r)
    assert reply["result"]["serverInfo"]["name"] == server.server_name, reply


@pytest.mark.parametrize("line", list(_HOSTILE.values()), ids=list(_HOSTILE))
def test_hostile_input_gets_a_reply_and_the_server_lives(server: Server, line: bytes) -> None:
    """Each of these ended a server with a traceback on standard error. The server
    must answer and go on to the next request."""
    r = server.run(line + _PING)
    assert r.returncode == 0
    assert r.stderr == b""
    replies = _replies(r)
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies
    assert len(replies) == 2, replies
    assert b"\r\n" not in r.stdout, "MCP stdio is newline-delimited, with bare newlines"


def test_bytes_that_are_not_utf8_are_a_parse_error_with_a_null_id(server: Server) -> None:
    """Decoding with replacement characters would answer a different request than
    the one sent (here a "Method not found" for id 1); the line must be refused."""
    r = server.run(_HOSTILE["bytes that are not UTF-8"] + _PING)
    assert r.stderr == b""
    replies = _replies(r)
    assert replies[0]["id"] is None, replies
    assert replies[0]["error"]["code"] == -32700, replies
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies


@pytest.mark.parametrize(
    "line",
    [_PING, b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'],
    ids=["a reply", "the fallback reply for an id that is not JSON"],
)
def test_a_client_that_stops_reading_ends_the_server_quietly(server: Server, line: bytes) -> None:
    """BrokenPipeError at the write, and again when the interpreter flushes at exit.
    The NaN id takes the fallback reply, whose write must be under the same handler."""
    proc = server.popen(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    try:
        proc.stdin.write(line * 2000)
        proc.stdin.close()
    except OSError:
        pass  # the server left before reading it all, which is the point
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == b""


@_POSIX_ONLY
@pytest.mark.parametrize("redirect", ["<&-", ">&-"], ids=["stdin closed", "stdout closed"])
def test_a_server_started_with_a_standard_stream_closed_leaves_quietly(server: Server, redirect: str) -> None:
    r = subprocess.run(
        ["/bin/sh", "-c", f'exec "$@" {redirect}', "sh", *server.argv],
        capture_output=True, env=server.env, timeout=30, check=False, stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0
    assert r.stderr == b""


_EXPLODING_WRAPPER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("server_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real = mod.handle
def handle(state, message):
    if isinstance(message, dict) and message.get("method") == "explode":
        raise KeyError("a secret quoted from the client")
    return real(state, message)
mod.handle = handle
raise SystemExit(mod.main(*sys.argv[2:]))
"""


def test_an_unexpected_handler_error_is_an_internal_error_naming_only_its_class(server: Server, tmp_path: Path) -> None:
    """The catch-all keeps the server alive and says which class failed (so a
    client log is diagnosable) without the message text, which may quote input,
    and without a byte on stderr. The wrapper loads the server's script by path, as
    a test does, which also proves the shared loop is found from that context."""
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(_EXPLODING_WRAPPER, encoding="utf-8")
    boom = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "explode"}).encode() + b"\n"
    r = subprocess.run(
        [sys.executable, str(wrapper), str(server.script), *server.main_args],
        input=boom + _PING, capture_output=True, env=server.env, timeout=60, check=False,
    )
    assert r.returncode == 0
    assert r.stderr == b""
    replies = _replies(r)
    assert replies[0] == {"jsonrpc": "2.0", "id": 5, "error": {"code": -32603, "message": "Internal error", "data": {"exception": "KeyError"}}}, replies
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies
    assert b"secret" not in r.stdout


# -- the one handler-specific case: the sequential-thinking tool's own validation ----------------------------

@pytest.mark.parametrize("field", ["thoughtNumber", "totalThoughts"])
def test_an_integer_too_big_for_a_float_is_a_validation_error_not_an_internal_error(tmp_path: Path, field: str) -> None:
    """``float(10**400)`` raises OverflowError, which the catch-all answered as -32603."""
    arguments = {"thought": "t", "nextThoughtNeeded": True, "thoughtNumber": 1, "totalThoughts": 1}
    arguments[field] = int("9" * 400)  # type: ignore[assignment]
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sequentialthinking", "arguments": arguments}}
    r = _sequential_thinking(tmp_path).run(json.dumps(call).encode() + b"\n" + _PING)
    assert r.returncode == 0 and r.stderr == b""
    replies = _replies(r)
    assert "error" not in replies[0], replies
    assert replies[0]["result"]["isError"] is True, replies
    assert field in replies[0]["result"]["content"][0]["text"], replies
    assert replies[1]["id"] == 7, replies


# -- where the shared loop is found from ---------------------------------------------------------------------

def _plugin_copy(tmp_path: Path) -> Path:
    """The plugin laid out as an install holds it, minus what these servers never read."""
    root = tmp_path / "installed" / "conexus"
    for sub in ("mcp", "launch", "hooks/scripts"):
        shutil.copytree(_PLUGIN / sub, root / sub, ignore=shutil.ignore_patterns("__pycache__"))
    return root


_FORMS = [
    "sequential-thinking as .mcp.json starts it",
    "sequential-thinking with no script directory on sys.path",
    "stand-in through the launcher",
    "stand-in run as a script with no script directory on sys.path",
]


def _launch_forms(tmp_path: Path) -> dict[str, tuple[tuple[str, ...], Path]]:
    """Every way a server is started from an installed plugin: name -> (argv, plugin root)."""
    root = _plugin_copy(tmp_path)
    seq, launcher, standin = root / "mcp" / "sequential_thinking.py", root / "launch" / "nx_launch.py", root / "launch" / "nx_standin.py"
    py = sys.executable
    return {
        "sequential-thinking as .mcp.json starts it": ((py, str(seq)), root),
        "sequential-thinking with no script directory on sys.path": ((py, "-I", str(seq)), root),
        "stand-in through the launcher": ((py, str(launcher), "nx-mcp"), root),
        "stand-in run as a script with no script directory on sys.path": ((py, "-I", str(standin), "nx-mcp"), root),
    }


@pytest.mark.parametrize("form", _FORMS)
def test_each_server_finds_the_shared_loop_in_an_installed_plugin_and_leaves_no_bytecode(tmp_path: Path, form: str) -> None:
    """Run from a copy of the plugin, from an unrelated working directory: the servers
    locate ``_stdio_loop.py`` from their own file, not from the working directory or
    ``sys.path[0]``, and importing it writes no ``.pyc`` into the plugin directory."""
    argv, root = _launch_forms(tmp_path)[form]
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run(
        list(argv), input=_HOSTILE["bytes that are not UTF-8"] + _PING, capture_output=True,
        env=_env(tmp_path), cwd=elsewhere, timeout=60, check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""
    replies = _replies(r)
    assert [reply["id"] for reply in replies] == [None, 7], replies
    assert replies[0]["error"]["code"] == -32700, replies
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


def test_the_shared_loop_is_standard_library_only() -> None:
    """Both servers run before the CLI is installed, so nothing they import may need it.
    ``_cli_lookup`` is the plugin's own standard-library-only sibling (hooks/scripts)."""
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    for script, extra in ((_LOOP, set()), (_SEQ, {"_stdio_loop"}), (_STANDIN, {"_stdio_loop", "_cli_lookup"})):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, script
                found.add((node.module or "").split(".")[0])
        assert found, f"no imports found in {script.name}; the scan examined nothing"
        assert found <= allowed | extra, (script.name, found - allowed - extra)

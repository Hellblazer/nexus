# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sequential-thinking server survives hostile stdio without a word on stderr.

nexus-rwv5i: Claude Code records every stderr line of a stdio MCP server as an
error, and a traceback also ends the server. The RDR-228 stand-in copied this
server's loop and its review (nexus-sv2wx.6) found the same cases; the fixes
and these tests carry over from ``tests/hooks/test_nx_standin.py``. Each case
runs the script in a subprocess, as Claude Code does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "conexus" / "mcp" / "sequential_thinking.py"

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="closes a standard descriptor through sh")


def _not_json(constant: str) -> float:
    raise ValueError(f"{constant} is not JSON")


def _strict_loads(line: str):
    """``json.loads`` as a client reads a reply: ``NaN`` and ``Infinity`` are
    Python's extension, not JSON, and a strict parser drops the line."""
    return json.loads(line, parse_constant=_not_json)


_PING = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode() + b"\n"

_HOSTILE = {
    "bytes that are not UTF-8": b'{"jsonrpc":"2.0","id":1,"method":"pi\xffng"}\n',
    "nesting past the recursion limit": b"[" * 200_000 + b"\n",
    "an integer too long to parse": b'{"jsonrpc":"2.0","id":' + b"9" * 5000 + b',"method":"ping"}\n',
    "a lone surrogate in the id": b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n',
    "an id that is not JSON": b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
}


def _run(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, str(_SCRIPT)], input=payload, capture_output=True, timeout=60, check=False)


@pytest.mark.parametrize("line", list(_HOSTILE.values()), ids=list(_HOSTILE))
def test_hostile_input_gets_a_reply_and_the_server_lives(line: bytes) -> None:
    """Each of these ended the server with a traceback on standard error. The
    server must answer and go on to the next request."""
    r = _run(line + _PING)
    assert r.returncode == 0
    assert r.stderr == b""
    replies = [_strict_loads(reply) for reply in r.stdout.decode("utf-8").splitlines()]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies
    assert len(replies) == 2, replies
    assert b"\r\n" not in r.stdout, "MCP stdio is newline-delimited, with bare newlines"


def _replies(r: subprocess.CompletedProcess[bytes]) -> list[dict]:
    return [_strict_loads(reply) for reply in r.stdout.decode("utf-8").splitlines()]


def test_bytes_that_are_not_utf8_are_a_parse_error_with_a_null_id() -> None:
    """Decoding with replacement characters would answer a different request than
    the one sent (here a "Method not found" for id 1); the line must be refused."""
    r = _run(_HOSTILE["bytes that are not UTF-8"] + _PING)
    assert r.stderr == b""
    replies = _replies(r)
    assert replies[0]["id"] is None, replies
    assert replies[0]["error"]["code"] == -32700, replies
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies


@pytest.mark.parametrize("field", ["thoughtNumber", "totalThoughts"])
def test_an_integer_too_big_for_a_float_is_a_validation_error_not_an_internal_error(field: str) -> None:
    """``float(10**400)`` raises OverflowError, which the catch-all answered as -32603."""
    arguments = {"thought": "t", "nextThoughtNeeded": True, "thoughtNumber": 1, "totalThoughts": 1}
    arguments[field] = int("9" * 400)  # type: ignore[assignment]
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sequentialthinking", "arguments": arguments}}
    r = _run(json.dumps(call).encode() + b"\n" + _PING)
    assert r.returncode == 0 and r.stderr == b""
    replies = _replies(r)
    assert "error" not in replies[0], replies
    assert replies[0]["result"]["isError"] is True, replies
    assert field in replies[0]["result"]["content"][0]["text"], replies
    assert replies[1]["id"] == 7, replies


_EXPLODING_WRAPPER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("seq_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real = mod.handle
def handle(state, message):
    if isinstance(message, dict) and message.get("method") == "explode":
        raise KeyError("a secret quoted from the client")
    return real(state, message)
mod.handle = handle
raise SystemExit(mod.main())
"""


def test_an_unexpected_handler_error_is_an_internal_error_naming_only_its_class(tmp_path: Path) -> None:
    """The catch-all keeps the server alive and says which class failed (so a
    client log is diagnosable) without the message text, which may quote input,
    and without a byte on stderr."""
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(_EXPLODING_WRAPPER, encoding="utf-8")
    boom = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "explode"}).encode() + b"\n"
    r = subprocess.run([sys.executable, str(wrapper), str(_SCRIPT)], input=boom + _PING, capture_output=True, timeout=60, check=False)
    assert r.returncode == 0
    assert r.stderr == b""
    replies = _replies(r)
    assert replies[0] == {"jsonrpc": "2.0", "id": 5, "error": {"code": -32603, "message": "Internal error", "data": {"exception": "KeyError"}}}, replies
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}, replies
    assert b"secret" not in r.stdout


@pytest.mark.parametrize(
    "line",
    [_PING, b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'],
    ids=["a reply", "the fallback reply for an id that is not JSON"],
)
def test_a_client_that_stops_reading_ends_the_server_quietly(line: bytes) -> None:
    """BrokenPipeError at the write, and again when the interpreter flushes at exit."""
    proc = subprocess.Popen([sys.executable, str(_SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
def test_a_server_started_with_a_standard_stream_closed_leaves_quietly(redirect: str) -> None:
    r = subprocess.run(
        ["sh", "-c", f'exec "$0" "$1" {redirect}', sys.executable, str(_SCRIPT)],
        capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0
    assert r.stderr == b""

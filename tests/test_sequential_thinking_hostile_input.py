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

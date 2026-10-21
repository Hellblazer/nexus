# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stdio JSON-RPC loop the plugin's two stdlib-only MCP servers share.

``conexus/mcp/sequential_thinking.py`` and ``conexus/launch/nx_standin.py`` (the
server the launcher serves when there is no CLI) each used to carry their own
copy of this loop, and each copy was fixed by hand twice (nexus-rwv5i); this is
the one copy (nexus-ing7a). A server supplies only its handler: a function from
one parsed JSON-RPC message to a reply dict, or ``None`` for a notification.

What the loop guarantees, for every input a client can send:

* Each stdin line is decoded as strict UTF-8. A replacement character would answer
  a different request (another method name, id or argument) than the one sent, so
  a line that is not UTF-8 is a ``-32700`` parse error with a null id.
* JSON that is nested past the recursion limit, or holds an integer too long to
  parse, is the same parse error.
* An exception out of the handler is ``-32603`` naming only the exception class
  (its message may quote the client's input), and the loop goes on.
* Replies are ASCII-escaped JSON: a lone surrogate echoed in an id cannot be
  encoded, and U+2028 inside a string must not read as a line end. An id that is
  NaN or Infinity is not JSON; the reply to it is ``-32600`` with a null id.
* A client that stops reading ends the server quietly: at the failed write, and
  again at interpreter exit, when the flush of the dead pipe would report it.
* Standard input or output closed at start ends the server quietly.
* Nothing is written to standard error. Claude Code records each stderr line of
  a stdio server as an ``error`` entry in its MCP log, and a traceback also ends
  the server.

Standard library only, and no ``nexus`` import: it runs from the plugin directory
before the CLI is installed. Both servers reach it as a sibling of ``nx_standin.py``
(``conexus/launch/``); ``sequential_thinking.py`` adds that directory to its
``sys.path`` from its own location.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

#: One parsed JSON-RPC message in; the reply dict out, or ``None`` for no reply.
Handler = Callable[[Any], "dict[str, Any] | None"]


def result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def answer(handle: Handler, line: bytes) -> dict[str, Any] | None:
    """The reply to one input line, as the bytes read. Never raises: an uncaught
    error would be a traceback on standard error and the end of the server."""
    try:
        message = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # not UTF-8 (UnicodeDecodeError), bad JSON, a 5000-digit integer, deep nesting
        return error(None, -32700, "Parse error")
    try:
        return handle(message)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        request_id = message.get("id") if isinstance(message, dict) else None
        # The class name only: the message may quote the client's input, and
        # nothing goes to stderr.
        return error(request_id, -32603, "Internal error", {"exception": type(exc).__name__})


def send(stdout: Any, reply: dict[str, Any]) -> None:
    """Write one reply line. The write and flush stay under the caller's ``OSError`` handler."""
    try:
        line = json.dumps(reply, allow_nan=False)
    except ValueError:  # an id of NaN or Infinity is not JSON
        line = json.dumps(error(None, -32600, "Invalid Request"))
    stdout.write(line + "\n")
    stdout.flush()


def client_gone() -> int:
    """Leave quietly after a failed write: point fd 1 at the null device so the
    interpreter's own flush at exit does not report the dead pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    except OSError:
        pass
    return 0


def serve(handle: Handler, stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin (a binary stream, one request per
    line) until EOF, or until the client stops reading."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        reply = answer(handle, line)
        if reply is None:
            continue
        try:
            send(stdout, reply)
        except OSError:  # the client went away
            return client_gone()
    return 0


def serve_stdio(handle: Handler) -> int:
    """Serve *handle* on this process's stdio; the exit status of the server."""
    if sys.stdin is None or sys.stdout is None or getattr(sys.stdin, "buffer", None) is None:  # a stream closed
        return 0
    # Text-mode stdout on Windows would translate "\n" to "\r\n" and encode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines. Standard input
    # is read as bytes and each line decoded strictly in answer(): bytes that are
    # not UTF-8 are a parse error for their line, not the end of the server.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        return serve(handle)
    except KeyboardInterrupt:
        return 0

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging pass-through proxy for Claude Code API requests (nexus-4mptw).

Claude Code reaches it through ANTHROPIC_BASE_URL=http://127.0.0.1:<port>.
Every request is forwarded unchanged (method, path, body, headers including
auth) to https://api.anthropic.com. Two things change on the wire, neither in
the body: Accept-Encoding is forced to identity so the SSE stream can be read,
and the response is relayed close-delimited (Connection: close).

Each request appends one JSONL row to the log: time, path, a header
allowlist (never authorization, x-api-key or cookie), the parsed request body,
the status, and the usage block (message_start + message_delta for SSE, or
the JSON body's usage).

Usage: proxy.py <port> <log.jsonl>
"""
from __future__ import annotations

import http.client
import json
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST: str = "api.anthropic.com"
#: Request headers copied into the log. Auth-bearing headers are never here.
LOGGED_HEADERS: tuple[str, ...] = (
    "anthropic-beta", "anthropic-version", "user-agent", "x-app",
    "content-type", "x-claude-code-session-id", "x-stainless-retry-count",
)
_HOP: frozenset[str] = frozenset({
    "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "upgrade", "te", "trailer", "host", "content-length", "accept-encoding",
})
_lock = threading.Lock()
_seq = 0


def _usage_from_sse(raw: bytes) -> dict:
    usage: dict = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            msg = ev.get("message", {})
            usage["model"] = msg.get("model")
            usage["start"] = msg.get("usage")
        elif ev.get("type") == "message_delta":
            usage["delta"] = ev.get("usage")
            usage["stop_reason"] = (ev.get("delta") or {}).get("stop_reason")
    return usage


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path: str = ""

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _handle(self) -> None:
        global _seq
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        fwd["Host"] = UPSTREAM_HOST
        fwd["Accept-Encoding"] = "identity"
        if body:
            fwd["Content-Length"] = str(len(body))
        conn = http.client.HTTPSConnection(UPSTREAM_HOST, 443, timeout=600,
                                           context=ssl.create_default_context())
        t0 = time.time()
        conn.request(self.command, self.path, body=body or None, headers=fwd)
        resp = conn.getresponse()
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        captured = bytearray()
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            captured += chunk
            self.wfile.write(chunk)
            self.wfile.flush()
        conn.close()
        self.close_connection = True
        ctype = resp.getheader("content-type", "")
        if "event-stream" in ctype:
            usage = _usage_from_sse(bytes(captured))
        else:
            try:
                j = json.loads(captured)
                usage = {"json_usage": j.get("usage"), "model": j.get("model")} if isinstance(j, dict) else {}
            except ValueError:
                usage = {}
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"_unparsed_len": len(body)}
        with _lock:
            _seq += 1
            row = {
                "seq": _seq, "t": t0, "dt": round(time.time() - t0, 3),
                "method": self.command, "path": self.path, "status": resp.status,
                "headers": {k: v for k, v in self.headers.items()
                            if k.lower() in LOGGED_HEADERS},
                "usage": usage, "body": parsed,
            }
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    do_POST = _handle
    do_GET = _handle
    do_PUT = _handle
    do_DELETE = _handle


def main() -> None:
    port, log_path = int(sys.argv[1]), sys.argv[2]
    Handler.log_path = log_path
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()

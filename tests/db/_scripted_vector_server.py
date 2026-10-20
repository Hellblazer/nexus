# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scripted loopback HTTP server for the vector client's request-identity tests (nexus-yye8l.11/.12).

Each request is matched to a route key (``"POST /v1/vectors/gc/quarantine-orphans"``,
``"GET /v1/requests/"`` -- a prefix match for the status route) and answered from that route's
script, one entry per request, the last entry repeating. Every request is recorded with its
headers and raw body, so a test can assert what went over the wire, not what a mock was called with.

A script entry is a :class:`Reply` or the string ``"hang"`` (read the request, then say nothing
until the client gives up, so the client's read times out). A route with no script answers 404
(the shape of an engine that has no such route).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: dict | bytes = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


def ok(body: dict, **headers: str) -> Reply:
    return Reply(200, body, headers)


def err(status: int, error: str, **headers: str) -> Reply:
    return Reply(status, {"error": error}, headers)


class ScriptedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self) -> None:
        self.scripts: dict[str, list[Reply | str]] = {}
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        super().__init__(("127.0.0.1", 0), _make_handler())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def script(self, route: str, *entries: Reply | str) -> None:
        self.scripts[route] = list(entries)

    def release_hangs(self) -> None:
        self._release.set()

    def next_entry(self, method: str, path: str) -> Reply | str | None:
        with self._lock:
            for route, entries in self.scripts.items():
                m, _, prefix = route.partition(" ")
                if m == method and (path == prefix or (prefix.endswith("/") and path.startswith(prefix))):
                    return entries.pop(0) if len(entries) > 1 else entries[0]
        return None

    def calls(self, method: str, prefix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)]


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self.server.requests.append({  # type: ignore[attr-defined]
                "method": self.command, "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw,
            })
            entry = self.server.next_entry(self.command, self.path)  # type: ignore[attr-defined]
            if entry == "hang":
                self.server._release.wait(timeout=30)  # type: ignore[attr-defined]
                self.close_connection = True
                return
            reply = entry if isinstance(entry, Reply) else Reply(404, {"error": "no such route"})
            payload = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in reply.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815

        def log_message(self, *_a) -> None:
            pass

    return _Handler


def start() -> ScriptedServer:
    srv = ScriptedServer()
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


def stop(srv: ScriptedServer) -> None:
    srv.release_hangs()
    srv.shutdown()
    srv.server_close()

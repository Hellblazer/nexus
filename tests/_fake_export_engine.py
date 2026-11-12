# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for the engine's tenant export routes, over a real HTTP socket (nexus-csayy.3).

The failure scenarios of ``nx backup`` in cloud mode (a token that expires mid-export, a page that fails N
times, a session the engine forgot, a 503 with and without ``Retry-After``) cannot be provoked on demand against
the real engine, so a test serves the documented contract itself and scripts the faults. The happy path against
the real engine is ``tests/test_nx_backup_cloud_roundtrip.py``.

The contract it serves is ``docs/wire-contract-pending.md`` entry bbb8d7b2b and ``TenantExportHandler``'s class
javadoc: ``GET /version``, ``POST /v1/tenant/export``, ``GET /v1/tenant/export/{id}/rows?table=&after=``,
``DELETE /v1/tenant/export/{id}``.
"""

from __future__ import annotations

import hashlib
import json
import struct
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_SIG = b"PGCOPY\n\xff\r\n\0"


def copy_page(rows: list[list[bytes | None]]) -> bytes:
    out = bytearray(_SIG + struct.pack(">ii", 0, 0))
    for row in rows:
        out += struct.pack(">h", len(row))
        for f in row:
            out += struct.pack(">i", -1) if f is None else struct.pack(">i", len(f)) + f
    out += struct.pack(">h", -1)
    return bytes(out)


@dataclass
class Req:
    method: str
    path: str
    query: dict[str, str]
    bearer: str
    #: how many requests of this kind (open, rows, delete, version) came before this one
    nth: int
    kind: str


@dataclass
class Reply:
    status: int = 200
    body: bytes | dict = b""
    headers: dict[str, str] = field(default_factory=dict)
    drop: bool = False  # close the connection without answering


@dataclass
class TableSpec:
    name: str
    table_class: str
    columns: list[tuple[str, str]]
    #: pages of rows: pages[i] is a list of rows; an empty list of pages is an empty table
    pages: list[list[list[bytes | None]]]


class FakeEngine:
    """Serves the export routes for *tables* on 127.0.0.1:0. ``script`` entries see every request first and may
    answer it (return a :class:`Reply`) or let it through (return None)."""

    def __init__(
        self, tables: list[TableSpec], *, accepted: set[str] | None = None, release: str = "0.1.191",
        supported: bool = True, format_version: int = 1, changesets: int | None = 572, tenant: str = "acme",
        expires_in_seconds: int = 3600,
    ) -> None:
        self.tables = {t.name: t for t in tables}
        self.order = [t.name for t in tables]
        self.accepted = accepted if accepted is not None else {"tok"}
        self.release, self.supported, self.format_version = release, supported, format_version
        self.changesets, self.tenant, self.expires_in = changesets, tenant, expires_in_seconds
        self.script: list[Callable[[Req], Reply | None]] = []
        self.log: list[Req] = []
        self.sessions: set[str] = set()
        self.opened: list[str] = []
        self.deleted: list[str] = []
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence
                pass

            def _serve(self) -> None:
                engine._handle(self)

            do_GET = do_POST = do_DELETE = _serve

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self) -> FakeEngine:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- what the tests read ---------------------------------------------------------------------

    def requests(self, kind: str) -> list[Req]:
        return [r for r in self.log if r.kind == kind]

    # -- the contract ----------------------------------------------------------------------------

    @staticmethod
    def _key(table: str, i: int) -> str:
        return f"{table}:{i}"

    def _send(self, h: BaseHTTPRequestHandler, reply: Reply) -> None:
        if reply.drop:
            h.close_connection = True
            h.connection.close()
            return
        body = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
        h.send_response(reply.status)
        ctype = "application/octet-stream" if isinstance(reply.body, bytes) else "application/json"
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in reply.headers.items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path
        if path == "/version":
            kind = "version"
        elif h.command == "POST" and path == "/v1/tenant/export":
            kind = "open"
        elif h.command == "GET" and path.endswith("/rows"):
            kind = "rows"
        elif h.command == "DELETE":
            kind = "delete"
        else:
            kind = "other"
        length = int(h.headers.get("Content-Length") or 0)
        if length:
            h.rfile.read(length)
        bearer = (h.headers.get("Authorization") or "").removeprefix("Bearer ")
        with self._lock:
            nth = self._counts.get(kind, 0)
            self._counts[kind] = nth + 1
            req = Req(h.command, path, query, bearer, nth, kind)
            self.log.append(req)
            scripted = None
            for entry in list(self.script):
                scripted = entry(req)
                if scripted is not None:
                    break
        if scripted is not None:
            self._send(h, scripted)
            return
        self._send(h, self._answer(req))

    def _answer(self, req: Req) -> Reply:
        if req.kind == "version":
            return Reply(body={
                "release_version": self.release, "app_version": "1.0-SNAPSHOT",
                **({"tenant_export_supported": True, "tenant_export_format_version": self.format_version}
                   if self.supported else {}),
            })
        if req.bearer not in self.accepted:
            return Reply(401, {"error": "unauthorized"})
        if req.kind == "open":
            return self._open()
        sid = req.path.removeprefix("/v1/tenant/export/").removesuffix("/rows")
        if req.kind == "delete":
            if sid not in self.sessions:
                return Reply(404, {"error": "no_such_session", "reason": "no_such_session"})
            self.sessions.discard(sid)
            self.deleted.append(sid)
            return Reply(body={"closed": True})
        if req.kind == "rows":
            if sid not in self.sessions:
                return Reply(404, {"error": "no_such_session", "reason": "no_such_session"})
            return self._rows(req)
        return Reply(404, {"error": "no such route", "reason": "not_found"})

    def _open(self) -> Reply:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        sid = str(uuid.uuid4())
        self.sessions.add(sid)
        self.opened.append(sid)
        now = datetime.now(UTC)
        return Reply(body={
            "session_id": sid, "format_version": self.format_version, "engine_version": self.release,
            "schema_changeset_count": self.changesets, "tenant_id": self.tenant,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "expires_at": (now + timedelta(seconds=self.expires_in)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "idle_timeout_seconds": 600, "total_timeout_seconds": self.expires_in,
            "tables": [
                {"name": t.name, "class": t.table_class, "primary_key": [t.columns[0][0]],
                 "columns": [{"name": n, "type": ty} for n, ty in t.columns]}
                for t in self.tables.values()
            ],
            "embedding_models": [],
        })

    def _rows(self, req: Req) -> Reply:
        table = self.tables.get(req.query.get("table", ""))
        if table is None:
            return Reply(400, {"error": "unknown table", "reason": "unknown_table"})
        after = req.query.get("after")
        index = 0
        if after is not None:
            index = next((i + 1 for i in range(len(table.pages)) if self._key(table.name, i) == after), None)
            if index is None:
                return Reply(400, {"error": "malformed after", "reason": "malformed_after"})
        rows = table.pages[index] if index < len(table.pages) else []
        body = copy_page(rows)
        done = index >= len(table.pages) - 1
        headers = {
            "X-Nexus-Export-Table": table.name,
            "X-Nexus-Export-Rows": str(len(rows)),
            "X-Nexus-Export-Done": "true" if done else "false",
            "X-Nexus-Export-Sha256": hashlib.sha256(body).hexdigest(),
        }
        if rows:
            headers["X-Nexus-Export-Last-Key"] = self._key(table.name, index)
        if after is None:
            headers["X-Nexus-Export-Table-Rows"] = str(sum(len(p) for p in table.pages))
        return Reply(200, body, headers)


def standard_tables(pages: int = 3) -> list[TableSpec]:
    """memory (data, 2 columns, *pages* pages of 2 rows), frecency (derived, 1 column, one page), and an empty plans."""
    return [
        TableSpec("memory", "data", [("id", "bigint"), ("content", "text")],
                  [[[b"%d" % (p * 2 + r), b"row %d-%d" % (p, r)] for r in range(2)] for p in range(pages)]),
        TableSpec("frecency", "derived", [("key", "text")], [[[b"a"], [b"b"]]]),
        TableSpec("plans", "data", [("id", "bigint")], []),
    ]

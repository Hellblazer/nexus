# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for the engine's tenant import routes, over a real HTTP socket (nexus-csayy.6).

The failure scenarios of ``nx backup restore`` (a part that fails N times, a part answered ``part_in_flight``, a
commit answered ``restore_busy``, a lost connection, an engine that restarted) cannot be provoked on demand against
the real engine, so a test serves the documented contract itself and scripts the faults. The happy path and the
round trip against the REAL engine are ``tests/test_nx_backup_cloud_roundtrip.py``.

The contract is ``docs/wire-contract-pending.md`` (the csayy.6 entry) and ``TenantImportHandler``'s class javadoc:
``GET /version``, ``POST /v1/tenant/import``, ``PUT /v1/tenant/import/{id}/rows?table=&part=``,
``POST .../close?table=&rows=``, ``POST .../commit``, ``GET /v1/tenant/import/{id}``, ``DELETE ...`` and
``GET /v1/tenant/import-marker``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from nexus.backup.tenant_file import count_copy_rows
from tests._fake_export_engine import Reply


@dataclass
class IReq:
    method: str
    path: str
    query: dict[str, str]
    bearer: str
    headers: dict[str, str]
    body: bytes
    #: ``version``, ``open``, ``part``, ``close``, ``commit``, ``status``, ``delete``, ``marker``
    kind: str
    #: how many requests of this kind came before this one
    nth: int


@dataclass
class _Session:
    id: str
    header: dict
    tables: list[str]
    staged: dict[str, list[tuple[int, bytes]]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    closed: dict[str, int] = field(default_factory=dict)
    state: str = "staging"
    status: str = "staging"
    polls: int = 0


class FakeImportEngine:
    """Serves the import routes on 127.0.0.1:0. ``script`` entries see every request first and may answer it
    (return a :class:`~tests._fake_export_engine.Reply`) or let it through (return None)."""

    def __init__(
        self, *, accepted: set[str] | None = None, release: str = "0.1.192", supported: bool = True,
        part_bytes: int = 8 * 1024 * 1024, load_max_seconds: int = 3600, skipped: dict[str, str] | None = None,
        load_polls: int = 1, busy_commits: int = 0, report: dict | None = None, notes: list[str] | None = None,
    ) -> None:
        self.accepted = accepted if accepted is not None else {"tok"}
        self.release, self.supported = release, supported
        self.part_bytes, self.load_max_seconds = part_bytes, load_max_seconds
        self.skipped = skipped or {}
        #: status reads that answer ``loading`` before the load reads ``committed``
        self.load_polls = load_polls
        #: commits answered 503 restore_busy before one is accepted
        self.busy_commits = busy_commits
        self.report = report if report is not None else {"tables": [], "soft_references": [], "skipped_tables": {}, "skipped_columns": []}
        self.notes = notes or []
        self.script: list[Callable[[IReq], Reply | None]] = []
        self.log: list[IReq] = []
        self.sessions: dict[str, _Session] = {}
        self.deleted: list[str] = []
        #: sessions that ended: id -> status text kept for the status route (a 200 that can carry ``failed: ...``)
        self.ended: dict[str, str] = {}
        self.marker: dict | None = None
        self.committed_header_sha: str | None = None
        self.tenant_has_export_session: str | None = None
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence
                pass

            def _serve(self) -> None:
                engine._handle(self)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self) -> FakeImportEngine:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- what the tests read ---------------------------------------------------------------------

    def requests(self, kind: str) -> list[IReq]:
        return [r for r in self.log if r.kind == kind]

    def open_session(self) -> _Session | None:
        return next(iter(self.sessions.values()), None)

    # -- plumbing ----------------------------------------------------------------------------------

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, reply: Reply) -> None:
        if reply.drop:
            h.close_connection = True
            h.connection.close()
            return
        body = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
        h.send_response(reply.status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        for k, v in reply.headers.items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        if path == "/version":
            kind = "version"
        elif path == "/v1/tenant/import-marker":
            kind = "marker"
        elif h.command == "POST" and path == "/v1/tenant/import":
            kind = "open"
        elif h.command == "PUT" and path.endswith("/rows"):
            kind = "part"
        elif h.command == "POST" and path.endswith("/close"):
            kind = "close"
        elif h.command == "POST" and path.endswith("/commit"):
            kind = "commit"
        elif h.command == "GET":
            kind = "status"
        elif h.command == "DELETE":
            kind = "delete"
        else:
            kind = "other"
        bearer = (h.headers.get("Authorization") or "").removeprefix("Bearer ")
        with self._lock:
            nth = self._counts.get(kind, 0)
            self._counts[kind] = nth + 1
            req = IReq(h.command, path, query, bearer, {k: v for k, v in h.headers.items()}, body, kind, nth)
            self.log.append(req)
            scripted = None
            for entry in list(self.script):
                scripted = entry(req)
                if scripted is not None:
                    break
            reply = scripted if scripted is not None else self._answer(req)
        self._send(h, reply)

    def _sid(self, req: IReq) -> str:
        rest = req.path.removeprefix("/v1/tenant/import/")
        return rest.split("/")[0]

    def _gone(self) -> Reply:
        return Reply(404, {"error": "no such import session", "reason": "no_such_session"})

    # -- the contract ------------------------------------------------------------------------------

    def _answer(self, req: IReq) -> Reply:
        if req.kind == "version":
            return Reply(body={"release_version": self.release, **({"tenant_import_supported": True} if self.supported else {})})
        if req.bearer not in self.accepted:
            return Reply(401, {"error": "unauthorized"})
        if req.kind == "open":
            return self._open(req)
        if req.kind == "marker":
            if self.marker is None:
                return Reply(body={"present": False})
            return Reply(body={"present": True, "marker": self.marker})
        sid = self._sid(req)
        if req.kind == "status":
            if sid in self.ended:
                return Reply(body={"session_id": sid, "state": "failed", "status": self.ended[sid], "notes": [], "staged_bytes": 0})
            s = self.sessions.get(sid)
            return self._gone() if s is None else Reply(body=self._status(s))
        if req.kind == "delete":
            s = self.sessions.pop(sid, None)
            if s is None:
                return self._gone()
            self.deleted.append(sid)
            return Reply(body={"closed": True})
        s = self.sessions.get(sid)
        if s is None:
            return self._gone()
        if req.kind == "part":
            return self._part(s, req)
        if req.kind == "close":
            return self._close(s, req)
        return self._commit(s)

    def _open(self, req: IReq) -> Reply:
        body = json.loads(req.body)
        for stale in self.sessions.values():
            return Reply(409, {"error": "the tenant has a session", "reason": "session_exists", "session_id": stale.id,
                               "session_kind": "import"})
        if self.tenant_has_export_session:
            return Reply(409, {"error": "the tenant has a session", "reason": "session_exists",
                               "session_id": self.tenant_has_export_session, "session_kind": "export"})
        sid = str(uuid.uuid4())
        names = [t["name"] for t in body["tables"] if t["name"] not in self.skipped]
        self.sessions[sid] = _Session(sid, body, names)
        return Reply(body={
            "session_id": sid, "part_bytes": self.part_bytes, "idle_timeout_seconds": 600, "total_timeout_seconds": 3600,
            "load_max_seconds": self.load_max_seconds, "max_bytes": 8 << 30, "tables": names,
            "skipped_tables": self.skipped, "notes": self.notes,
        })

    def _part(self, s: _Session, req: IReq) -> Reply:
        table, part = req.query["table"], int(req.query["part"])
        sha = req.headers.get("X-Nexus-Part-Sha256", "")
        if hashlib.sha256(req.body).hexdigest() != sha:
            return Reply(400, {"error": "the part's SHA-256 is not the one sent", "reason": "part_sha256_mismatch"})
        if table in self.skipped:
            return Reply(body={"skipped": True})
        staged = s.staged.setdefault(table, [])
        last = staged[-1][0] if staged else -1
        if part == last:
            return Reply(body={"repeat": True})
        if part < last:
            return Reply(409, {"error": "already staged", "reason": "part_already_staged"})
        if part > last + 1:
            return Reply(409, {"error": "gap", "reason": "part_gap"})
        try:
            rows = count_copy_rows(req.body)
        except ValueError as exc:
            return Reply(422, {"error": str(exc), "reason": "part_failed"})
        staged.append((part, req.body))
        s.rows[table] = s.rows.get(table, 0) + rows
        return Reply(body={"staged": True})

    def _close(self, s: _Session, req: IReq) -> Reply:
        table, rows = req.query["table"], int(req.query["rows"])
        have = s.rows.get(table, 0)
        if have != rows:
            return Reply(409, {"error": "row count", "reason": "row_count_mismatch", "staged": have, "expected": rows})
        s.closed[table] = rows
        return Reply(body={"closed": True})

    def _commit(self, s: _Session) -> Reply:
        if s.state != "staging":
            return Reply(body=self._status(s))
        open_tables = [t for t in s.tables if t not in s.closed]
        if open_tables:
            return Reply(409, {"error": "tables open", "reason": "tables_open", "tables": open_tables})
        if self.busy_commits > 0:
            self.busy_commits -= 1
            return Reply(503, {"error": "another restore is loading", "reason": "restore_busy"}, {"Retry-After": "60"})
        s.state, s.status = "loading", "loading"
        return Reply(202, self._status(s))

    def _status(self, s: _Session) -> dict:
        if s.state == "loading":
            s.polls += 1
            if s.polls > self.load_polls:
                s.state, s.status = "committed", "committed"
                self.committed_header_sha = s.header["header_sha256"]
                self.marker = {"header_sha256": self.committed_header_sha, "session_id": s.id,
                               "committed_at": "2026-11-13T00:00:00Z", "row_counts": dict(s.rows)}
        body: dict = {"session_id": s.id, "state": s.state, "status": s.status, "notes": self.notes,
                      "staged_bytes": sum(len(b) for p in s.staged.values() for _, b in p)}
        if s.state == "committed":
            body["report"] = self.report
        return body

    # -- faults tests apply -------------------------------------------------------------------------

    def restart(self) -> None:
        """The engine restarted: every in-memory session and its outcome is gone; the marker (data) survives."""
        self.sessions.clear()
        self.ended.clear()

    def staged_rows(self, table: str, session: _Session | None = None) -> list[tuple[int, bytes]]:
        s = session or self.open_session()
        return s.staged.get(table, []) if s else []

# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small stateful fake of the engine's catalog routes the link generators touch.

Shared by ``test_link_generator_service_mode.py`` and
``test_link_generator_scoped_reads.py`` (RDR-193 Step 7, nexus-u5m5g.8). Both drive a REAL
``HttpCatalogClient`` over this fake server, so a request count is a count of HTTP requests, not
of calls into a mock.

Routes (wire-faithful to ``CatalogHandler.java`` / ``CatalogLinksHandler.java`` /
``CatalogLinksRepository.java``): GET ``/list`` (the plain content_type and unfiltered branches and
the combined owner / content_type / source_uri_prefix branch), ``/show``, ``/owners/show``,
``/owners/list``, ``/link_query``; POST ``/link``, ``/unlink``, ``/resolve_many``,
``/links/upsert_many`` and ``/links/same_content``.

Every request is recorded in ``State.requests`` as ``(method, op, params-or-body)``, so a test can
count requests per route.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_BATCH = 1000


class State:
    """Server-side fixture state (documents, links, owners) and the request log, reset per test."""

    documents: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    owners: dict[str, dict[str, Any]] = {}
    #: every request, in order: (method, op, query params as a dict of lists, or the JSON body)
    requests: list[tuple[str, str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.documents = {}
        cls.links = []
        cls.requests = []
        # Default repo owner for the "1.1" prefix every fixture tumbler sits under. ``repo_root`` is
        # empty because most documents in the service-mode file carry an ABSOLUTE file_path, which
        # resolve_path returns before consulting repo_root; a test needing the relative-path
        # recombination (or the engine's path-endpoint identity arm) calls set_owner() with one.
        cls.owners = {
            "1.1": {
                "tumbler_prefix": "1.1", "owner_type": "repo", "name": "fake-repo",
                "repo_root": "", "repo_hash": "", "head_hash": "",
            },
        }

    @classmethod
    def set_owner(cls, tumbler_prefix: str, **fields: Any) -> None:
        base = dict(cls.owners.get(tumbler_prefix) or {})
        base.setdefault("tumbler_prefix", tumbler_prefix)
        base.setdefault("owner_type", "repo")
        base.setdefault("repo_root", "")
        base.update(fields)
        cls.owners[tumbler_prefix] = base

    @classmethod
    def add_document(cls, tumbler: str, **fields: Any) -> str:
        base: dict[str, Any] = {
            "tumbler": tumbler, "title": tumbler, "content_type": "", "file_path": "",
            "source_uri": "", "chunk_count": 0, "head_hash": "", "metadata": {},
            "source_mtime": 0.0, "bib_year": 0, "bib_authors": "", "bib_venue": "",
            "bib_citation_count": 0,
        }
        base.update(fields)
        cls.documents[tumbler] = base
        return tumbler

    @classmethod
    def links_matching(cls, from_t: str, to_t: str, link_type: str) -> list[dict[str, Any]]:
        return [
            lnk for lnk in cls.links
            if (not from_t or lnk["from_tumbler"] == from_t)
            and (not to_t or lnk["to_tumbler"] == to_t)
            and (not link_type or lnk["link_type"] == link_type)
        ]

    @classmethod
    def count(cls, method: str, op: str, pred: Any = None) -> int:
        return sum(
            1 for m, o, p in cls.requests
            if m == method and o == op and (pred is None or pred(p))
        )

    @classmethod
    def lists(cls) -> list[dict[str, list[str]]]:
        return [p for m, o, p in cls.requests if m == "GET" and o == "/list"]

    @classmethod
    def posts(cls, op: str) -> list[dict[str, Any]]:
        return [p for m, o, p in cls.requests if m == "POST" and o == op]


def _doc_key(d: dict[str, Any]) -> str:
    """The same-content key: index_content_hash, else metadata.content_hash, never head_hash."""
    return d.get("index_content_hash") or (d.get("metadata") or {}).get("content_hash") or ""


def _settled_key(d: dict[str, Any]) -> str:
    meta_key = (d.get("metadata") or {}).get("content_hash") or ""
    idx = d.get("index_content_hash") or ""
    if idx and meta_key and idx != meta_key:
        return ""
    return _doc_key(d)


def _is_live_pdf(t: str) -> bool:
    d = State.documents.get(t)
    return d is not None and d.get("content_type") in ("pdf", "paper")


def _repo_uri(owner_prefix: str, file_path: str) -> str | None:
    """The source_uri the engine derives for a path under the owner's repo_root, or None when the
    owner has no root or the path leaves it (the reconcile commit's containment rule)."""
    root = (State.owners.get(owner_prefix) or {}).get("repo_root") or ""
    if not root:
        return None
    joined = os.path.normpath(os.path.join(root, file_path))
    root_n = os.path.normpath(root)
    if joined != root_n and not joined.startswith(root_n + os.sep):
        return None
    return f"file://{joined}"


def _resolve_endpoint(end: dict[str, Any]) -> tuple[str | None, str]:
    """``(tumbler, reason)``: the live document an edge endpoint names, or ``(None, reason)``.

    A path endpoint resolves by identity first (the row whose source_uri is the URI derived from the
    named owner's repo_root and the path, whichever owner registered it), then to the named owner's
    row with that file_path. An absolute path under an owner with no root, or a path that leaves the
    root, is ``no_path``."""
    if end.get("tumbler"):
        t = end["tumbler"]
        return (t, "") if t in State.documents else (None, "dangling")
    owner, file_path = end["owner"], end["file_path"]
    root = (State.owners.get(owner) or {}).get("repo_root") or ""
    if os.path.isabs(file_path) and not root:
        return None, "no_path"
    if root:
        uri = _repo_uri(owner, file_path)
        if uri is None:
            return None, "no_path"
        by_uri = [d["tumbler"] for d in State.documents.values() if d.get("source_uri") == uri]
        if by_uri:
            return sorted(by_uri)[-1], ""
    own = [
        d["tumbler"] for d in State.documents.values()
        if d["tumbler"].startswith(owner + ".") and d.get("file_path") == file_path
    ]
    if own:
        return sorted(own)[-1], ""
    return None, "no_path"


class FakeLinkEngineHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        op = parsed.path.removeprefix("/v1/catalog")
        multi = parse_qs(parsed.query)
        State.requests.append(("GET", op, multi))
        params = {k: v[0] for k, v in multi.items()}

        if op == "/list":
            self._list(multi, params)
        elif op == "/show":
            doc = State.documents.get(params.get("tumbler", ""))
            if doc is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(doc)
        elif op == "/owners/show":
            owner = State.owners.get(params.get("tumbler_prefix", ""))
            if owner is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(owner)
        elif op == "/owners/list":
            self._send_json({"owners": list(State.owners.values())})
        elif op == "/link_query":
            matches = State.links_matching(
                params.get("from_tumbler", ""), params.get("to_tumbler", ""), params.get("link_type", ""),
            )
            created_by = params.get("created_by", "")
            if created_by:
                matches = [m for m in matches if m.get("created_by") == created_by]
            offset = int(params.get("offset", 0))
            limit = int(params.get("limit", 0)) or len(matches) or 1
            matches = matches[offset:offset + limit]
            self._send_json({"links": matches, "count": len(matches)})
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def _list(self, multi: dict[str, list[str]], params: dict[str, str]) -> None:
        docs = sorted(State.documents.values(), key=lambda d: d["tumbler"])
        owner = params.get("owner", "")
        types = [t for t in multi.get("content_type", []) if t]
        uri_prefix = params.get("source_uri_prefix", "")
        combined = bool(uri_prefix) or (bool(owner) and bool(types))
        if combined:
            out = docs
            if owner:
                out = [d for d in out if d["tumbler"].startswith(owner + ".")]
            if types:
                out = [d for d in out if d.get("content_type") in types]
            if uri_prefix:
                out = [d for d in out if d.get("source_uri", "").startswith(uri_prefix)]
            self._send_json({"documents": out, "count": len(out)})
            return
        if types:
            matched = [d for d in docs if d.get("content_type") == types[0]]
            self._send_json({"documents": matched, "count": len(matched)})
            return
        if owner:
            matched = [d for d in docs if d["tumbler"].startswith(owner + ".")]
            self._send_json({"documents": matched, "count": len(matched)})
            return
        limit = int(params.get("limit", 0)) or (len(docs) or 1)
        offset = int(params.get("offset", 0))
        page = docs[offset:offset + limit]
        self._send_json({"documents": page, "count": len(page)})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        op = urlparse(self.path).path.removeprefix("/v1/catalog")
        body = self._read_body()
        State.requests.append(("POST", op, body))

        if op == "/link":
            from_t, to_t, lt = body.get("from_tumbler", ""), body.get("to_tumbler", ""), body.get("link_type", "")
            existing = State.links_matching(from_t, to_t, lt)
            if existing:
                existing[0].update({
                    "created_by": body.get("created_by", ""),
                    "from_span": body.get("from_span", ""),
                    "to_span": body.get("to_span", ""),
                })
                self._send_json({"ok": True, "created": False})
            else:
                State.links.append({
                    "from_tumbler": from_t, "to_tumbler": to_t, "link_type": lt,
                    "created_by": body.get("created_by", ""),
                    "from_span": body.get("from_span", ""), "to_span": body.get("to_span", ""),
                })
                self._send_json({"ok": True, "created": True})
        elif op == "/resolve_many":
            entries = {d: State.documents[d] for d in body.get("doc_ids", []) if d in State.documents}
            self._send_json({"entries": entries})
        elif op == "/list_by_file_paths":
            # CatalogHandler.handleListByFilePaths: owner-agnostic, exact file_path, a path with no
            # live document is absent from the map
            wanted = [fp for fp in body.get("file_paths", []) if isinstance(fp, str)]
            found = {
                fp: sorted(
                    (d for d in State.documents.values() if d.get("file_path") == fp), key=lambda d: d["tumbler"],
                )
                for fp in wanted
            }
            self._send_json({"documents": {fp: docs for fp, docs in found.items() if docs}})
        elif op == "/unlink":
            gone = State.links_matching(
                body.get("from_tumbler", ""), body.get("to_tumbler", ""), body.get("link_type", ""),
            )
            State.links = [lnk for lnk in State.links if lnk not in gone]
            self._send_json({"deleted": len(gone)})
        elif op == "/links/upsert_many":
            self._upsert_many(body)
        elif op == "/links/same_content":
            self._same_content(body)
        else:
            self._send_json({"ok": True})

    def _upsert_many(self, body: dict) -> None:
        edges = body.get("edges")
        if not isinstance(edges, list):
            self._send_json({"error": "'edges' must be a list"}, 400)
            return
        if len(edges) > MAX_BATCH:
            self._send_json({"error": f"too many edges (max {MAX_BATCH})"}, 400)
            return
        created = skipped = 0
        unresolved: list[dict[str, Any]] = []
        for i, edge in enumerate(edges):
            ends: dict[str, str] = {}
            for side in ("from", "to"):
                t, reason = _resolve_endpoint(edge[side])
                if t is None:
                    unresolved.append({"index": i, "side": side, "reason": reason})
                    break
                ends[side] = t
            else:
                if State.links_matching(ends["from"], ends["to"], edge["link_type"]):
                    skipped += 1
                    continue
                State.links.append({
                    "from_tumbler": ends["from"], "to_tumbler": ends["to"], "link_type": edge["link_type"],
                    "created_by": edge.get("created_by", ""),
                    "from_span": edge.get("from_span", ""), "to_span": edge.get("to_span", ""),
                })
                created += 1
        self._send_json({"created": created, "skipped": skipped, "unresolved": unresolved})

    def _same_content(self, body: dict) -> None:
        tumblers = body.get("tumblers")
        if not isinstance(tumblers, list):
            self._send_json({"error": "'tumblers' must be a list"}, 400)
            return
        if len(tumblers) > MAX_BATCH:
            self._send_json({"error": f"too many tumblers (max {MAX_BATCH})"}, 400)
            return
        prune = bool(body.get("prune"))
        given = set(tumblers)
        retracted = 0
        if prune:
            candidates = [
                lnk for lnk in State.links
                if lnk["link_type"] == "same-as" and lnk["created_by"] == "content_hash_dedup"
                and (lnk["from_tumbler"] in given or lnk["to_tumbler"] in given)
                and not (lnk.get("metadata") or {}).get("co_discovered_by")
            ]
            live_pdfs = [t for t in State.documents if _is_live_pdf(t)]
            if candidates and not live_pdfs:
                self._send_json({"created": 0, "retracted": 0, "refused": "empty_fetch"})
                return
            for lnk in candidates:
                a, b = lnk["from_tumbler"], lnk["to_tumbler"]
                stale = not (_is_live_pdf(a) and _is_live_pdf(b))
                if not stale:
                    ka, kb = _settled_key(State.documents[a]), _settled_key(State.documents[b])
                    stale = bool(ka) and bool(kb) and ka != kb
                if stale:
                    State.links.remove(lnk)
                    retracted += 1
                    given.add(a)
        groups: dict[str, list[str]] = {}
        for t, d in State.documents.items():
            if d.get("content_type") in ("pdf", "paper") and _doc_key(d):
                groups.setdefault(_doc_key(d), []).append(t)
        created = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            anchor = min(members)
            anchor_given = anchor in given
            for m in members:
                if m == anchor or not (anchor_given or m in given):
                    continue
                if State.links_matching(m, anchor, "same-as"):
                    continue
                State.links.append({
                    "from_tumbler": m, "to_tumbler": anchor, "link_type": "same-as",
                    "created_by": "content_hash_dedup", "from_span": "", "to_span": "",
                })
                created += 1
        self._send_json({"created": created, "retracted": retracted})


def start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), FakeLinkEngineHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""A mock catalog engine for the RDR-254 move-hint tests: the read routes a hint asks, over an ``httpx.MockTransport``.

No tenant, no substrate: ``HintEngine.client()`` is a real ``HttpCatalogClient`` whose transport answers from the
owners and documents a test added. Every request is on record in ``requests`` as ``(method, path, query, body)``, so a
test can say what a hint asked (and that it asked nothing that writes). A route this mock does not know answers 404,
which is how an unlisted write would show."""
from __future__ import annotations

import json
from typing import Any

import httpx

from nexus.catalog.http_catalog_client import HttpCatalogClient

#: The only routes a hint may touch: ``/version``, the owner reads, one ``/list`` page and the metadata route.
READ_ROUTES = frozenset({
    ("GET", "/version"), ("GET", "/v1/catalog/owners/by_repo"), ("GET", "/v1/catalog/owners/show"),
    ("POST", "/v1/catalog/owners/by_type"), ("GET", "/v1/catalog/list"), ("GET", "/v1/catalog/docs/by-meta"),
})


class HintEngine:
    def __init__(self, *, move_supported: bool | None = True, by_meta_status: int = 200) -> None:
        self.version: dict[str, Any] = {"release_version": "0.1.200", "catalog_docs_by_meta_supported": True}
        if move_supported is not None:
            self.version["catalog_move_supported"] = move_supported
        self.by_meta_status = by_meta_status
        self.owners: dict[str, dict] = {}
        self.docs: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str, dict, dict]] = []

    # ── state ───────────────────────────────────────────────────────────────

    def add_owner(self, prefix: str, root: str, repo_hash: str, *, name: str = "", deactivated: bool = False) -> None:
        self.owners[prefix] = {
            "tumbler_prefix": prefix, "name": name or f"repo-{prefix}", "owner_type": "repo", "repo_hash": repo_hash,
            "repo_root": root, "head_hash": "", "deactivated_at": "2026-01-01T00:00:00Z" if deactivated else None,
        }
        self.docs.setdefault(prefix, [])

    def add_doc(self, prefix: str, file_path: str, source_uri: str, *, content_hash: str | None = None,
                alias_of: str = "") -> str:
        n = len(self.docs.setdefault(prefix, [])) + 1
        tumbler = f"{prefix}.{n}"
        meta = {"content_hash": content_hash} if content_hash else {}
        self.docs[prefix].append({"tumbler": tumbler, "file_path": file_path, "source_uri": source_uri, "meta": meta,
                                  "alias_of": alias_of, "title": file_path})
        return tumbler

    # ── transport ───────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, query, body))
        key = (request.method, request.url.path)
        if key == ("GET", "/version"):
            return httpx.Response(200, json=self.version)
        if key == ("GET", "/v1/catalog/owners/by_repo"):
            hit = next((o for o in self.owners.values() if o["repo_hash"] == query.get("repo_hash")), None)
            return httpx.Response(200, json={"tumbler_prefix": hit["tumbler_prefix"]}) if hit else httpx.Response(404, json={})
        if key == ("GET", "/v1/catalog/owners/show"):
            hit = self.owners.get(query.get("tumbler_prefix", ""))
            return httpx.Response(200, json=hit) if hit else httpx.Response(404, json={})
        if key == ("POST", "/v1/catalog/owners/by_type"):
            rows = [o for o in self.owners.values() if o["owner_type"] == body.get("owner_type")
                    and (body.get("include_deactivated") or not o["deactivated_at"])]
            return httpx.Response(200, json={"owners": rows})
        if key == ("GET", "/v1/catalog/list"):
            rows = self.docs.get(query.get("owner", ""), [])
            if "limit" in query:
                rows = rows[: int(query["limit"])]
            return httpx.Response(200, json={"documents": rows, "count": len(rows)})
        if key == ("GET", "/v1/catalog/docs/by-meta"):
            if self.by_meta_status != 200:
                return httpx.Response(self.by_meta_status, json={"error": "boom"})
            wanted = query.get("meta.content_hash")
            hits = [d for rows in self.docs.values() for d in rows if (d["meta"] or {}).get("content_hash") == wanted]
            hits.sort(key=lambda d: d["tumbler"])
            limit, offset = int(query["limit"]), int(query.get("offset", 0))
            page = hits[offset: offset + limit]
            return httpx.Response(200, json={"count": len(hits), "limit": limit, "offset": offset, "documents": page})
        return httpx.Response(404, json={"error": request.url.path})

    def client(self) -> HttpCatalogClient:
        cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
        cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(self.handle))
        return cat

    # ── what a test asks ────────────────────────────────────────────────────

    def routes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _q, _b in self.requests]

    def asked_by_meta(self) -> list[str]:
        return [q["meta.content_hash"] for m, p, q, _b in self.requests if p == "/v1/catalog/docs/by-meta"]

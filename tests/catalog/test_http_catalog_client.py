# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.18 (P2.7): HttpCatalogClient + factory seam tests.

Tests:
1. _resolve_config raises cleanly when NX_SERVICE_PORT/TOKEN are absent
2. Constructor produces correct base_url + headers from override args
3. Each major category of HTTP verbs (GET/POST) routes correctly
4. Factory seam: make_catalog_reader returns HttpCatalogClient when env set
5. Factory seam: make_catalog_writer returns _ServiceCatalogWriter when env set
6. _ServiceCatalogWriter enforces CATALOG_WRITE_OPS whitelist
7. Guarded methods raise NotImplementedError (rebuild, defrag, compact, sync, pull)
8. Fake server round-trip exercising the REAL routes from CatalogHandler

Route alignment verified against CatalogHandler.java switch cases (bead nexus-gmiaf.18).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from nexus.db.service_endpoint import resolve_service_config as _resolve_config

# Wave review (the h8rf6.3 -> 49523e16 lesson): fixture chashes must be
# REPRESENTATIVE -- real 64-char sha256 digests, never short literals that
# make width bugs a structural no-op. RDR-180 (nexus-p78a0): the catalog
# chash IS the full digest — CHASH_* equal CHUNK_SHA_* now; the 32-char
# prefix era (and its [:32] wire normalization) is retired.
CHUNK_SHA_A = "2ccea837b4713a233eea0914ad7adda8bcbbbeccd9ac45e217cab14843229eb2"  # sha256("fake-chunk-A")
CHUNK_SHA_B = "6756d390c50dd95257ad481c8ab3669f93838ed7e8f3cf334a8bbf1281d8e3b2"  # sha256("fake-chunk-B")
CHASH_A = CHUNK_SHA_A
CHASH_B = CHUNK_SHA_B
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_tumbler() -> str:
    return "1.1.1"


def _entry_dict(**kwargs: Any) -> dict:
    """Minimal server response dict that _to_entry accepts.

    nexus-8y1tm: ``file_path``/``source_uri`` defaults are the literal
    values ``tests/catalog/test_shape_parity_tripwire.py`` seeds onto its
    local ``doc_a`` fixture — this lets client-side exact-match filters
    (``by_file_path``, ``by_source_uri``, ``find_by_file_path``,
    ``resolve_path``) find a match against the fake server too, without a
    stateful fake.

    nexus-u26b4: ``metadata``/``source_mtime``/``bib_*`` added so the
    ``descendants()`` parity entry (which does NOT go through
    ``_to_entry()`` — it forwards the raw wire dict) sees the same
    Java-normalized document-row shape (metadata as a parsed nested dict,
    the full ``bib_*`` field set) as local ``Catalog.descendants()``'s now
    -normalized rows. Harmless to every other ``_entry_dict()`` consumer:
    they all go through ``_to_entry()``, which collapses to a
    ``CatalogEntry`` dataclass regardless of which wire keys were present.
    """
    base = {
        "tumbler": _fake_tumbler(),
        "title": "Test Doc",
        "content_type": "paper",
        "chunk_count": 0,
        "file_path": "src/alpha.py",
        "source_uri": "file:///tmp/nexus-test/alpha.py",
        "metadata": {"key": "value"},
        "source_mtime": 0.0,
        "bib_year": 0,
        "bib_authors": "",
        "bib_venue": "",
        "bib_citation_count": 0,
    }
    base.update(kwargs)
    return base


# ── _to_entry fence-field parsing (nexus-5xn3k.3 review item 3) ─────────────
#
# Pins the load-bearing None-vs-'' ASYMMETRY: ``index_state`` is the one
# fence field that must hydrate as ``None`` (never ``''``) when absent or
# explicitly null on the wire — that ``None`` IS the "unknown, fall through
# to manifest/verify" signal the RUNFENCE three-way (`_index_run_fresh`)
# branches on. A future "make it consistent with the other three" edit
# coercing it to ``""`` would silently collapse "unknown" into a string that
# is neither ``'complete'``/``'indexing'``/``'failed'`` NOR ``None`` — still
# functionally "unknown" today, but a landmine for any later equality check
# against ``None`` specifically (e.g. a straight ``is None`` probe).


class TestToEntryFenceFields:
    def test_missing_keys_hydrate_index_state_as_none(self) -> None:
        from nexus.catalog.http_catalog_client import _to_entry

        d = _entry_dict()
        for key in (
            "index_state", "index_content_hash", "index_run_id", "index_started_at",
        ):
            d.pop(key, None)
        entry = _to_entry(d)
        assert entry.index_state is None
        assert entry.index_content_hash == ""
        assert entry.index_run_id == ""
        assert entry.index_started_at == ""

    def test_explicit_json_null_hydrates_index_state_as_none(self) -> None:
        from nexus.catalog.http_catalog_client import _to_entry

        d = _entry_dict(
            index_state=None, index_content_hash=None,
            index_run_id=None, index_started_at=None,
        )
        entry = _to_entry(d)
        assert entry.index_state is None
        assert entry.index_content_hash == ""
        assert entry.index_run_id == ""
        assert entry.index_started_at == ""

    def test_complete_state_passes_through_verbatim(self) -> None:
        from nexus.catalog.http_catalog_client import _to_entry

        d = _entry_dict(
            index_state="complete", index_content_hash="a" * 64,
            index_run_id="run-1", index_started_at="2026-08-02T00:00:00Z",
        )
        entry = _to_entry(d)
        assert entry.index_state == "complete"
        assert entry.index_content_hash == "a" * 64
        assert entry.index_run_id == "run-1"
        assert entry.index_started_at == "2026-08-02T00:00:00Z"


class FakeCatalogHandler(BaseHTTPRequestHandler):
    """Routes matching the real CatalogHandler.java switch cases exactly."""

    #: nexus-gaou3: last body POSTed to /collections/rename (for cross_model assertions).
    last_rename_body: dict[str, Any] = {}
    #: nexus-gaou3: when True, /collections/rename 409s a plain (cross_model-absent)
    #: rename, mirroring the server's collision guard so the client's error
    #: propagation can be asserted.
    rename_conflicts: bool = False

    #: RDR-168 P3 wire-semantics regression coverage.
    get_ops: list[str] = []          # ops seen by do_GET, in order
    post_ops: list[str] = []         # ops seen by do_POST, in order
    last_link_body: dict[str, Any] = {}
    #: from_tumbler value for which /link_query reports NO existing link (absent path).
    link_absent_from: str = "9.9.9"
    #: when set, /list returns this many docs for a content_type-filtered request
    #: (CatalogHandler returns ALL matching rows ignoring limit/offset — used to prove
    #: the client issues a single request and does not loop).
    list_content_type_count: int = 0
    #: how many rows /descendants returns (all under the requested prefix).
    #: Default 2 matches /list's seeded pair so existing descendants-parity
    #: expectations carry over unchanged; raise it to prove completeness past
    #: the old 500-row single-page cap.
    descendants_count: int = 2
    #: /link response shape: None omits the key (old-JAR skew), bool sets created (njrcn.3).
    link_created: "bool | None" = True

    #: nexus-fguo5: single-hop alias_of map for /show's follow_alias arm,
    #: mirroring CatalogRepository.resolveAliasTarget's chain-walk. Empty by
    #: default so /show is an identity lookup unless a test seeds it.
    show_alias_map: dict[str, str] = {}
    #: last follow_alias value /show actually decoded (boolParam semantics:
    #: "1"/"true"/"yes" case-insensitively true, everything else — including
    #: absence — false). Lets tests assert the WIRE value the client sent,
    #: not just the client-side kwarg.
    last_show_follow_alias: "bool | None" = None

    # manifest_verify_response/last_manifest_verify_doc_id REMOVED (RDR-191
    # Phase 6, nexus-o8dil.33) alongside the client's manifest_verify method
    # and its only test consumer.
    #: nexus-5xn3k.3: /index-run/complete response — override to exercise the
    #: 409 IndexRunVerifyRefused branch (set complete_index_run_conflict=True).
    complete_index_run_conflict: bool = False
    #: last bodies POSTed to the 3 index-run routes, for call-site assertions.
    last_begin_index_run_body: dict[str, Any] = {}
    last_begin_index_run_many_body: dict[str, Any] = {}
    last_complete_index_run_body: dict[str, Any] = {}
    last_fail_index_run_body: dict[str, Any] = {}

    #: nexus-cw262: last bodies POSTed to /owners/deactivate, /owners/reactivate.
    last_owner_deactivate_body: dict[str, Any] = {}
    last_owner_reactivate_body: dict[str, Any] = {}
    #: nexus-cw262 round-3 critique: last body POSTed to /owners/by_type.
    last_owners_by_type_body: dict[str, Any] = {}

    @classmethod
    def reset_log(cls) -> None:
        cls.get_ops = []
        cls.post_ops = []
        cls.last_link_body = {}
        cls.list_content_type_count = 0
        cls.descendants_count = 2
        cls.show_alias_map = {}
        cls.last_show_follow_alias = None
        cls.complete_index_run_conflict = False
        cls.last_begin_index_run_body = {}
        cls.last_begin_index_run_many_body = {}
        cls.last_complete_index_run_body = {}
        cls.last_fail_index_run_body = {}
        cls.last_owner_deactivate_body = {}
        cls.last_owner_reactivate_body = {}
        cls.last_owners_by_type_body = {}

    def log_message(self, *args: Any) -> None:
        pass  # suppress test noise

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _query_params(self) -> dict[str, str]:
        qs = urlparse(self.path).query
        return {k: v[0] for k, v in parse_qs(qs).items()} if qs else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        FakeCatalogHandler.get_ops.append(op)

        if op == "/stats":
            # nexus-8y1tm: full CatalogRepository.stats() shape (7 keys) —
            # doc_count/link_count/owner_count were the only 3 pre-existing
            # keys here; collection_count/chunk_count/links_by_type/
            # by_content_type added so HttpCatalogClient.stats() (a pure
            # passthrough of this response) shape-matches Catalog.stats().
            self._send_json({
                "doc_count": 7, "link_count": 3, "owner_count": 2,
                "collection_count": 2, "chunk_count": 5,
                "links_by_type": {"cites": 1, "relates": 1},
                "by_content_type": {"code": 5, "prose": 2},
            })
        elif op == "/show":
            # nexus-fguo5: mirror CatalogHandler.handleShow exactly —
            # follow_alias defaults FALSE (boolParam semantics: "1"/"true"/
            # "yes" case-insensitively true, anything else including
            # absence false), and only when true does the fake walk
            # show_alias_map to a target and return THAT tumbler, mirroring
            # CatalogRepository.getDocument(tenant, tumbler, followAlias).
            params = self._query_params()
            tumbler = params.get("tumbler", _fake_tumbler())
            raw = params.get("follow_alias", "")
            follow_alias = raw.lower() in ("1", "true", "yes")
            FakeCatalogHandler.last_show_follow_alias = follow_alias
            target = tumbler
            if follow_alias:
                seen: set[str] = set()
                while (
                    target in FakeCatalogHandler.show_alias_map
                    and target not in seen
                ):
                    seen.add(target)
                    target = FakeCatalogHandler.show_alias_map[target]
            self._send_json(_entry_dict(tumbler=target))
        elif op == "/list":
            params = self._query_params()
            if params.get("content_type") and FakeCatalogHandler.list_content_type_count:
                # Mirror CatalogHandler: the content_type branch ignores limit/offset and
                # returns ALL matching rows in one response.
                n = FakeCatalogHandler.list_content_type_count
                self._send_json({"documents": [_entry_dict() for _ in range(n)], "count": n})
            else:
                # nexus-u26b4: second doc has EMPTY metadata (vs the first's
                # populated dict) — descendants() parity needs this
                # heterogeneity to mirror local Catalog.descendants()'s
                # seeded mix (doc_a has a real ``meta=``, every other seeded
                # descendant does not); _to_entry()-based consumers
                # (by_content_type, all_documents, ...) are unaffected since
                # they collapse to a CatalogEntry dataclass regardless.
                self._send_json({
                    "documents": [
                        _entry_dict(),
                        _entry_dict(title="Second", metadata={}),
                    ],
                    "count": 2,
                })
        elif op == "/descendants":
            # T2 nexus/chroma-residue-plan-2026-08-10 §C1: the dedicated
            # descendants route. Mirrors CatalogHandler.handleDescendants —
            # `prefix` is REQUIRED (400 when absent/blank) and the response
            # envelope is /list's ({"documents": [...], "count": N}).
            #
            # The filtering is done HERE, server-side, exactly as the real
            # route does it (SQL `tumbler LIKE prefix || '.%'`). That is
            # load-bearing for this fake: the pre-fix client pulled one
            # unfiltered /list page and filtered client-side, so a fake that
            # returned unfiltered rows would let a client that never filters
            # at all pass just as happily as a correct one.
            #
            # Absent this branch, do_GET's catchall answers 404 — which is
            # precisely the status the client's paginated fallback triggers
            # on, so every fake-server test would have silently exercised the
            # fallback and NEVER this route.
            params = self._query_params()
            prefix = params.get("prefix", "")
            if not prefix.strip():
                self._send_json({"error": "prefix query param required"}, code=400)
            else:
                n = FakeCatalogHandler.descendants_count
                # Same metadata heterogeneity as /list (nexus-u26b4): the
                # first row carries populated metadata, the rest empty.
                docs = [
                    _entry_dict(tumbler=f"{prefix}.{i + 1}")
                    if i == 0
                    else _entry_dict(tumbler=f"{prefix}.{i + 1}", title=f"Desc {i + 1}", metadata={})
                    for i in range(n)
                ]
                self._send_json({"documents": docs, "count": len(docs)})
        elif op == "/search":
            self._send_json({"documents": [_entry_dict()], "count": 1})
        elif op == "/resolve":
            self._send_json({"documents": [_entry_dict()]})
        elif op == "/links":
            params = self._query_params()
            direction = params.get("direction", "both")
            # njrcn.5: mirror the server-side type filter (single link_type or link_types IN).
            requested = None
            if params.get("link_types"):
                requested = {t for t in params["link_types"].split(",") if t}
            elif params.get("link_type"):
                requested = {params["link_type"]}
            out_row = {"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}
            # nexus-u26b4: the in-direction row was previously hardcoded empty
            # (see the links_to EXCLUSIONS/REGISTRY history in
            # test_shape_parity_tripwire.py) — a real inbound-link row so
            # direction=in|both are wire-faithful like direction=out already was.
            in_row = {"from_tumbler": "1.1.3", "to_tumbler": "1.1.2", "link_type": "cites"}
            match = requested is None or "cites" in requested
            if direction == "out":
                self._send_json({"links_from": [out_row] if match else [], "links_to": []})
            elif direction == "in":
                self._send_json({"links_from": [], "links_to": [in_row] if match else []})
            else:
                self._send_json({
                    "links_from": [out_row] if match else [],
                    "links_to": [in_row] if match else [],
                })
        elif op == "/links/orphaned":
            # nexus-ysrwi review (2026-07-25): the route census EXCLUDED this
            # route with the reason "no Python caller exists yet ... this
            # exclusion must be removed" when one lands. HttpCatalogClient
            # .orphaned_links() then landed one commit later and the exclusion
            # was not removed -- the census cannot see its own trigger
            # condition, since it only asks "is there a fake branch?", never
            # "is there a caller?". This branch mirrors
            # CatalogRepository.orphanedLinks(): id / from_tumbler / to_tumbler
            # / link_type / created_by / side, under a {"links": [...]} envelope.
            self._send_json({"links": [{
                "id": 1,
                "from_tumbler": "1.1.1",
                "to_tumbler": "9.9.9",
                "link_type": "cites",
                "created_by": "user",
                "side": "to",
            }]})
        elif op == "/link_query":
            params = self._query_params()
            if params.get("from_tumbler") == FakeCatalogHandler.link_absent_from:
                self._send_json({"links": [], "count": 0})
            else:
                self._send_json({"links": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}], "count": 1})
        elif op == "/manifest/get":
            self._send_json({"rows": [{"position": 0, "chash": CHASH_A}], "count": 1})
        elif op == "/manifest/chashes":
            # nexus-ir6eh: the real CatalogHandler emits count alongside
            # chashes (truncation defence, v0.1.55+); the client reconciles
            # len(chashes) == count and fails loud on deviation.
            self._send_json({"chashes": [CHASH_A, CHASH_B], "count": 2})
        elif op == "/manifest/null_collection":
            # T2 nexus/chroma-residue-plan-2026-08-10 §C2: GET
            # /manifest/null_collection -> {total, backfillable} — mirrors
            # CatalogRepository.manifestNullCollectionReport's shape.
            self._send_json({"total": 0, "backfillable": 0})
        elif op == "/chash/conformance":
            # RDR-180 (nexus-du2dw): GET /chash/conformance?dim= ->
            # {dim, tables: [{table_name, total, non_conformant, sample_chashes}]}
            params = self._query_params()
            dim = int(params.get("dim", "0"))
            self._send_json({
                "dim": dim,
                "tables": [
                    {"table_name": f"nexus.chunks_{dim}", "total": 10,
                     "non_conformant": 0, "sample_chashes": []},
                    {"table_name": "nexus.catalog_document_chunks", "total": 10,
                     "non_conformant": 0, "sample_chashes": []},
                ],
            })
        # /manifest/verify and /manifest/verify_all route branches REMOVED
        # (RDR-191 Phase 6, nexus-o8dil.33) alongside the client's
        # manifest_verify/manifest_verify_all methods and their only test
        # consumers. (nexus.manifest_verify(text) itself is kept server-side
        # for completeIndexRun — see /index-run/complete below — but this
        # fake never needed a route for that internal call.)
        elif op == "/collections/list":
            # nexus-8y1tm: full CatalogRepository.collRow() shape (10 keys) —
            # owner_id added so collections_by_owner's client-side filter
            # (c.get("owner_id") == owner_id) has something to match ("1.1" is
            # the tumbler_prefix every fixture owner in this file uses).
            # legacy_grandfathered is an int (0/1) on the wire (collRow's
            # ``legcy`` param is a boxed Integer column, not a boolean) —
            # deliberately NOT coerced to a Python bool here (see
            # nexus-8y1tm KNOWN DRIFT note on get_collection/list_collections).
            self._send_json({"collections": [{
                "name": "code__test__voyage-code-3__v1", "content_type": "code",
                "owner_id": "1.1", "embedding_model": "voyage-code-3",
                "model_version": "1", "display_name": "code__test__voyage-code-3__v1",
                "legacy_grandfathered": 0, "superseded_by": "", "superseded_at": "",
                "created_at": "2026-07-01T00:00:00+00:00",
            }]})
        elif op == "/collections/get":
            # nexus-8y1tm: echo the requested name; full collRow shape.
            params = self._query_params()
            name = params.get("name")
            if not name:
                self._send_json({"error": "name required"}, 400)
            else:
                self._send_json({
                    "name": name,
                    "owner_id": "1.1",
                    "content_type": "code",
                    "embedding_model": "voyage-code-3",
                    "model_version": "1",
                    "display_name": name,
                    # legacy_grandfathered: int (0/1) on the wire, matching
                    # CatalogRepository.collRow's boxed-Integer column — see
                    # the KNOWN DRIFT note where this is registered/excluded.
                    "legacy_grandfathered": 0 if "__" in name else 1,
                    "superseded_by": "", "superseded_at": "",
                    "created_at": "2026-07-01T00:00:00+00:00",
                })
        elif op == "/collections/for_tuple":
            self._send_json({"name": "code__test__voyage-code-3__v1"})
        elif op == "/collections/owner-root":
            params = self._query_params()
            name = params.get("name")
            if not name:
                self._send_json({"error": "name query param required"}, 400)
            else:
                self._send_json({"owner_id": "1.1", "repo_root": "/tmp/nexus-test"})
        elif op == "/collections/health":
            params = self._query_params()
            coll = params.get("collection")
            if not coll:
                self._send_json({"error": "collection query param required"}, 400)
            else:
                self._send_json({
                    "last_indexed": "2026-07-01T00:00:00+00:00",
                    "orphan_count": 1,
                    "stale_source_ratio": 0.0,
                })
        elif op == "/coverage":
            self._send_json({"coverage": [{"content_type": "code", "total": 1, "linked": 1}]})
        elif op == "/docs/distinct-collections":
            self._send_json({"collections": ["code__test__voyage-code-3__v1"]})
        elif op == "/docs/collection-counts":
            self._send_json({"counts": {"code__test__voyage-code-3__v1": 2}})
        elif op == "/docs/orphaned":
            # nexus-8y1tm: CatalogRepository.orphanedDocs() narrow 4-key shape
            # (tumbler/title/content_type/file_path, all str) — NOT the full
            # doc-row shape _entry_dict() produces.
            self._send_json({"documents": [{
                "tumbler": "1.1.9", "title": "Orphan",
                "content_type": "code", "file_path": "src/orphan.py",
            }]})
        elif op == "/docs/absolute-paths":
            self._send_json({"documents": [{
                "tumbler": "1.1.8",
                "file_path": "/abs/path/doc.txt",
                "physical_collection": "code__test__voyage-code-3__v1",
            }]})
        elif op == "/owners/all-with-roots":
            self._send_json({"owners": [{
                "tumbler_prefix": "1.1", "name": "myrepo", "owner_type": "repo",
                "repo_hash": "fakehash", "description": "", "repo_root": "/tmp/nexus-test",
                "head_hash": "",
            }]})
        elif op == "/owners/list":
            self._send_json({"owners": [{"tumbler_prefix": "1.1", "name": "myrepo"}]})
        elif op == "/owners/by_repo":
            self._send_json({"tumbler_prefix": "1.1", "name": "myrepo"})
        elif op == "/owners/by_name":
            # nexus-8y1tm: owner_type "curator" so curator_owner_tumbler_by_name's
            # client-side filter (o.get("owner_type") == "curator") has a match.
            self._send_json({"owners": [
                {"tumbler_prefix": "1.1", "name": "myrepo", "owner_type": "curator"},
            ]})
        elif op == "/owners/show":
            params = self._query_params()
            prefix = params.get("tumbler_prefix")
            if not prefix:
                self._send_json({"error": "tumbler_prefix required"}, 400)
            else:
                self._send_json({
                    "tumbler_prefix": prefix, "name": "myrepo", "owner_type": "repo",
                    "repo_hash": "fakehash", "description": "", "repo_root": "/tmp/nexus-test",
                    "head_hash": "",
                })
        elif op == "/resolve_span":
            params = self._query_params()
            chash = params.get("span_chash", "")
            coll  = params.get("collection", "")
            # RDR-180 (nexus-p78a0): the client sends the citation's FULL
            # width — the fake routes key on the 64-hex wire values.
            if chash == "deadbeef" * 8 and coll == "knowledge__o__bge-768__v1":
                self._send_json({
                    "chunk_text": "hello span world",
                    "metadata":   {"lang": "en"},
                    "chunk_hash": chash,
                })
            elif chash == "feeded00" * 8:  # _MISSING_CHASH
                self.send_response(404)
                self.end_headers()
            else:
                self._send_json({
                    "chunk_text": "generic chunk text",
                    "metadata":   {},
                    "chunk_hash": chash,
                })
        elif op == "/v1/chash/lookup":
            # nexus-84tr4: the alias-aware route. Echoes the CANONICAL 64-hex
            # it resolved — identity for canonical input, alias-chained for a
            # legacy 32-hex ref. Unknown refs echo nothing.
            params = self._query_params()
            chash = params.get("chash", "")
            if chash == _LEGACY_CHASH_32:
                self._send_json({"chash": _GLOBAL_CHASH_FULL, "rows": [
                    {"collection": "knowledge__o__bge-768__v1"},
                ]})
            elif chash == "00000000" * 8:
                self._send_json({"rows": []})
            else:
                self._send_json({"chash": chash, "rows": []})
        elif op == "/resolve_chash":
            params = self._query_params()
            chash = params.get("chash", "")
            # A legacy-width ref has no row here: /resolve_chash carries no
            # alias fallback, which is precisely the asymmetry nexus-84tr4 fixes.
            if chash == "00000000" * 8 or chash == _LEGACY_CHASH_32:
                self.send_response(404)
                self.end_headers()
            else:
                self._send_json({
                    "chash":               chash,
                    "chunk_hash":          chash,
                    "physical_collection": "knowledge__o__bge-768__v1",
                    "doc_id":              "1.2.3",
                    "chunk_text":          "resolved chunk body",
                    "metadata":            {"source": "test"},
                })
        elif op == "/resolve_chunk":
            # nexus-gc2ze: mirrors CatalogHandler.handleResolveChunk exactly —
            # split on ".", >= 4 segments required, 4th segment must parse as
            # an int, doc prefix = first 3 segments. Stateless fake: any
            # well-formed chunk address "resolves" (real 404/400 semantics
            # depend on a live Postgres document row, exercised by the Java
            # integration tests instead); "9.9.999.0" stands in for a missing
            # document so client-side 404-handling has something to hit.
            params = self._query_params()
            tumbler = params.get("tumbler", "")
            segments = tumbler.split(".")
            if len(segments) < 4:
                self._send_json({"error": "tumbler is not a chunk address (need >= 4 segments)"}, 400)
            else:
                try:
                    chunk_index = int(segments[3])
                except ValueError:
                    self._send_json({"error": "invalid chunk segment"}, 400)
                else:
                    doc_tumbler = ".".join(segments[:3])
                    if doc_tumbler == "9.9.999":
                        self.send_response(404)
                        self.end_headers()
                    else:
                        self._send_json({
                            "document_tumbler": doc_tumbler,
                            "chunk_index": chunk_index,
                            "physical_collection": "code__test__voyage-code-3__v1",
                            "title": "Test Doc",
                            "content_type": "code",
                        })
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()
        FakeCatalogHandler.post_ops.append(op)

        if op == "/doc/register":
            self._send_json({"tumbler": _fake_tumbler()})
        elif op == "/register":
            self._send_json({"ok": True})
        elif op == "/update":
            self._send_json({"updated": 1})
        elif op == "/delete":
            self._send_json({"deleted": 1})
        elif op == "/link":
            FakeCatalogHandler.last_link_body = body
            resp: dict = {"ok": True}
            if FakeCatalogHandler.link_created is not None:
                resp["created"] = FakeCatalogHandler.link_created
            self._send_json(resp)
        elif op == "/unlink":
            self._send_json({"deleted": 1})
        elif op == "/traverse":
            self._send_json({"nodes": [_entry_dict()], "edges": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}]})
        elif op == "/manifest/write":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/append":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/purge":
            self._send_json({"deleted": 1})
        elif op == "/manifest/get_many":
            manifests = {
                "1.1.1": [{"position": 0, "chash": CHASH_A, "line_start": 1, "line_end": 9}],
            }
            self._send_json({"manifests": manifests, "count": len(manifests)})
        elif op == "/manifest/chashes_many":
            # nexus-eslkl / T2 nexus/design-eslkl-hook-lock-narrowing §8.1:
            # mirrors CatalogHandler.handleManifestChashesMany's
            # {"chashes": {doc_id: [chash, ...]}, "count": N} shape — the
            # chash-only twin of /manifest/get_many above. No client
            # consumer yet (get_manifests already serves the sweep's
            # before-read); faked for census parity / future callers.
            chashes = {"1.1.1": [CHASH_A]}
            self._send_json({"chashes": chashes, "count": len(chashes)})
        elif op == "/manifest/docs_for_chashes":
            # Real server: {"tumblers": [tumbler_string, ...], "count": N}
            # (flat list, SELECT DISTINCT) — count reconciled client-side
            # since v0.1.61 (nexus-ocf52).
            tumblers = ["1.1.1"]
            self._send_json({"tumblers": tumblers, "count": len(tumblers)})
        # /manifest/backfill route branch REMOVED (RDR-191 Phase 6,
        # nexus-o8dil.33) alongside the client's manifest_backfill method
        # and its only test consumer.
        elif op == "/index-run/begin":
            # nexus-5xn3k.3: mirrors CatalogHandler.handleIndexRunBegin.
            FakeCatalogHandler.last_begin_index_run_body = body
            self._send_json({"ok": True})
        elif op == "/index-run/begin-many":
            # nexus-vw594 F1: mirrors CatalogHandler.handleIndexRunBeginMany's
            # {docs, failed_doc_ids} success shape.
            FakeCatalogHandler.last_begin_index_run_many_body = body
            docs = body.get("docs") or []
            self._send_json({"docs": len(docs), "failed_doc_ids": []})
        elif op == "/index-run/complete":
            # nexus-5xn3k.3: mirrors CatalogHandler.handleIndexRunComplete's
            # 200 {referenced, present, missing, flagged} success shape and
            # its 409 {error, doc_id, referenced, present, missing,
            # chunk_count} fail-closed refusal (CatalogRepository.
            # IndexRunVerifyRefused), toggled via complete_index_run_conflict.
            FakeCatalogHandler.last_complete_index_run_body = body
            if FakeCatalogHandler.complete_index_run_conflict:
                self._send_json({
                    "error": "completeIndexRun refused", "doc_id": body.get("doc_id", ""),
                    "referenced": 3, "present": 1, "missing": 2,
                    "chunk_count": body.get("chunk_count", 0),
                }, 409)
            else:
                self._send_json({"referenced": 2, "present": 2, "missing": 0, "flagged": False})
        elif op == "/index-run/fail":
            # nexus-5xn3k.3: mirrors CatalogHandler.handleIndexRunFail.
            FakeCatalogHandler.last_fail_index_run_body = body
            self._send_json({"ok": True})
        elif op == "/owners/upsert":
            self._send_json({"ok": True})
        elif op == "/owners/head_hash":
            self._send_json({"updated": 1})
        elif op == "/owners/deactivate":
            # nexus-cw262: mirrors CatalogHandler.handleOwnerDeactivate's
            # {"deactivated": 0|1} envelope.
            FakeCatalogHandler.last_owner_deactivate_body = body
            self._send_json({"deactivated": 1})
        elif op == "/owners/reactivate":
            # nexus-cw262: mirrors CatalogHandler.handleOwnerReactivate's
            # {"reactivated": 0|1} envelope.
            FakeCatalogHandler.last_owner_reactivate_body = body
            self._send_json({"reactivated": 1})
        elif op == "/collections/upsert":
            self._send_json({"ok": True})
        elif op == "/collections/supersede":
            self._send_json({"updated": 5})
        elif op == "/collections/rename":
            # RDR-164 P3: consolidated endpoint returns per-table re-home counts.
            # nexus-gaou3: stash the body so tests can assert cross_model threading.
            FakeCatalogHandler.last_rename_body = body
            if FakeCatalogHandler.rename_conflicts and body.get("cross_model") is not True:
                self._send_json({"error": "target collection already exists"}, code=409)
            else:
                # nexus-cecqy: the canonical branch RETIRES the old registry row
                # as a superseded tombstone; it stopped DELETEing it, so the key
                # is catalog_collections_superseded.
                self._send_json({"renamed": {"catalog_documents": 3,
                                             "catalog_collections_inserted": 1,
                                             "catalog_collections_superseded": 1}})
        elif op == "/import/owner":
            self._send_json({"imported": 1})
        elif op == "/import/document":
            self._send_json({"imported": 1})
        elif op == "/import/link":
            self._send_json({"imported": 1})
        elif op == "/verify/relation-counts":
            # echo a count for each requested whitelisted relation
            rels = body.get("relations", [])
            self._send_json({"counts": {r: 42 for r in rels}})
        elif op == "/docs/chunk-counts":
            ids = body.get("doc_ids", [])
            self._send_json({i: 3 for i in ids} if ids else {})
        elif op == "/links/from-batch":
            tumblers = body.get("tumblers", [])
            self._send_json(
                {t: [{"from_tumbler": t, "link_type": "cites"}] for t in tumblers}
                if tumblers else {}
            )
        elif op == "/resolve_many":
            ids = body.get("doc_ids", [])
            if not ids:
                self._send_json({"entries": {}})
            else:
                self._send_json({"entries": {i: _entry_dict(tumbler=i) for i in ids}})
        elif op == "/owners/by_type":
            # nexus-cw262 round-3 critique (T2 21467 Significant-4): stash
            # the whole body (not just owner_type) so a test can assert the
            # ACTUAL wire value of include_deactivated the client sent,
            # rather than a response shape that would pass identically
            # whether the client serialized it correctly, wrong, or dropped
            # it entirely.
            FakeCatalogHandler.last_owners_by_type_body = body
            owner_type = body.get("owner_type")
            if not owner_type:
                self._send_json({"error": "owner_type required"}, 400)
            else:
                owner_row = {"tumbler_prefix": "1.1", "name": "myrepo", "owner_type": owner_type}
                if body.get("include_deactivated"):
                    owner_row["deactivated_at"] = "2026-08-05T00:00:00Z"
                self._send_json({"owners": [owner_row]})
        elif op == "/purge-trash":
            # nexus-3ck2g E3: mirrors CatalogHandler.handlePurgeTrash —
            # {older_than_days: int >= 1 (default 30), dry_run: bool
            # (default true)} in; CatalogRepository.purgeTrashPreview /
            # .purgeTrash out, echoing dry_run plus documents_purged and
            # per-dim chunks_<dim>_stranded counts in BOTH modes.
            dry_run = body.get("dry_run", True)
            self._send_json({
                "dry_run": dry_run,
                "documents_purged": 3,
                "chunks_384_stranded": 0,
                "chunks_768_stranded": 12,
                "chunks_1024_stranded": 0,
            })
        else:
            self._send_json({"ok": True})


def start_fake_server() -> tuple[HTTPServer, str]:
    """Start a local fake catalog HTTP server; return (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), FakeCatalogHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)  # brief wait for the thread to reach serve_forever
    return server, f"http://127.0.0.1:{port}"


# ── _resolve_config tests ─────────────────────────────────────────────────────

class TestResolveConfig:
    def test_error_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Missing port
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            _resolve_config()

        # Non-integer port
        monkeypatch.setenv("NX_SERVICE_PORT", "not_a_port")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT must be an integer"):
            _resolve_config()

        # Missing token
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            _resolve_config()

    def test_valid_config_returns_tuple_with_default_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok123")
        host, port, token = _resolve_config()
        assert host == "10.0.0.1", "explicit host must be honored"
        assert port == 9090
        assert token == "tok123"

        # Default host applies when NX_SERVICE_HOST is unset.
        monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        host, _, _ = _resolve_config()
        assert host == "127.0.0.1", "default host must be 127.0.0.1 when unset"


# ── HttpCatalogClient round-trip tests ───────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    server, url = start_fake_server()
    yield url
    server.shutdown()


@pytest.fixture
def client(fake_server: str):
    with HttpCatalogClient(
        base_url=fake_server,
        tenant="tenant_abc",
        _token="test_tok",
    ) as c:
        yield c


class TestHttpCatalogClientRoundTrip:
    def test_client_basic_info_reads(self, client: HttpCatalogClient) -> None:
        assert client.is_initialized() is True
        s = client.stats()
        assert s["doc_count"] == 7
        assert client.doc_count() == 7

    def test_register_returns_tumbler_and_has_no_bib_fields(
        self, client: HttpCatalogClient
    ) -> None:
        # Positional owner+title as in Catalog.register signature
        t = client.register("1.1", "My Paper", content_type="paper")
        assert isinstance(t, Tumbler)
        assert str(t) == "1.1.1"

        # register() must NOT accept bib_year/bib_authors — CatalogEntry has none.
        import inspect
        from nexus.catalog.http_catalog_client import HttpCatalogClient as HCC
        sig = inspect.signature(HCC.register)
        assert "bib_year" not in sig.parameters
        assert "bib_authors" not in sig.parameters

    def test_register_many_pages_and_falls_back_per_doc_on_page_failure(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-9dvqy: 2500 docs page at 1000 (1000+1000+500); tumblers come
        # back aligned 1:1 with docs in input order across the concatenation.
        docs = [{"title": f"d{i}", "file_path": f"{i}.py"} for i in range(2500)]
        page_sizes: list[int] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/doc/register_many"
            assert body["owner_prefix"] == "1.1"
            page = body["docs"]
            base = sum(page_sizes)  # tumblers continue where prior pages left off
            page_sizes.append(len(page))
            return {"tumblers": [f"1.1.{base + j + 1}" for j in range(len(page))]}

        client._post = _fake_post  # type: ignore[method-assign]
        out = client.register_many("1.1", docs)

        assert page_sizes == [1000, 1000, 500]
        assert all(p <= 1000 for p in page_sizes)
        assert len(out) == 2500
        assert all(isinstance(t, Tumbler) for t in out)
        assert [str(t) for t in out[:3]] == ["1.1.1", "1.1.2", "1.1.3"]
        assert str(out[-1]) == "1.1.2500"

        # nexus-9dvqy: a whole-page failure must not sink the run — it falls
        # back to per-doc register() (POST /doc/register), preserving per-file
        # isolation and still returning aligned tumblers.
        docs2 = [{"title": f"d{i}", "file_path": f"{i}.py"} for i in range(3)]
        single_calls: list[str] = []

        def _fake_post_fallback(path: str, body: dict | None = None) -> Any:
            if path == "/doc/register_many":
                raise RuntimeError("500 batch failed")
            assert path == "/doc/register"
            single_calls.append(body["file_path"])
            return {"tumbler": f"1.1.{len(single_calls)}"}

        client._post = _fake_post_fallback  # type: ignore[method-assign]
        out2 = client.register_many("1.1", docs2)

        assert single_calls == ["0.py", "1.py", "2.py"]
        assert [str(t) for t in out2] == ["1.1.1", "1.1.2", "1.1.3"]
        assert all(isinstance(t, Tumbler) for t in out2)

    def test_update_many_pages_falls_back_and_handles_empty(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-xedhp: 2500 updates page at 1000 (1000+1000+500); counts come
        # back aligned 1:1 with updates in input order across the concatenation.
        updates = [{"tumbler": f"1.1.{i}", "head_hash": "abc"} for i in range(2500)]
        page_sizes: list[int] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/update_many"
            page = body["updates"]
            page_sizes.append(len(page))
            return {"updated": [1] * len(page)}

        client._post = _fake_post  # type: ignore[method-assign]
        out = client.update_many(updates)

        assert page_sizes == [1000, 1000, 500]
        assert len(out) == 2500
        assert all(c == 1 for c in out)

        # a whole-page failure must not sink the run — it falls back to
        # per-doc update() (POST /update), preserving per-file isolation.
        updates2 = [{"tumbler": f"1.1.{i}", "head_hash": "abc"} for i in range(3)]
        single_calls: list[str] = []

        def _fake_post_fallback(path: str, body: dict | None = None) -> Any:
            if path == "/update_many":
                raise RuntimeError("500 batch failed")
            assert path == "/update"
            single_calls.append(body["tumbler"])
            return {"updated": 1}

        client._post = _fake_post_fallback  # type: ignore[method-assign]
        out2 = client.update_many(updates2)

        assert single_calls == ["1.1.0", "1.1.1", "1.1.2"]
        assert out2 == [1, 1, 1]

        # Empty input short-circuits with no request.
        assert client.update_many([]) == []

    def test_delete_many_pages_falls_back_and_handles_empty(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-xedhp: 2500 tumblers page at 1000 (1000+1000+500).
        tumblers = [f"1.1.{i}" for i in range(2500)]
        page_sizes: list[int] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/delete_many"
            page = body["tumblers"]
            page_sizes.append(len(page))
            return {"deleted": page}

        client._post = _fake_post  # type: ignore[method-assign]
        out = client.delete_many(tumblers)

        assert page_sizes == [1000, 1000, 500]
        assert out == set(tumblers)

        # a whole-page failure must not sink the run — it falls back to
        # per-doc delete_document() (POST /delete).
        tumblers2 = ["1.1.0", "1.1.1", "1.1.2"]
        single_calls: list[str] = []

        def _fake_post_fallback(path: str, body: dict | None = None) -> Any:
            if path == "/delete_many":
                raise RuntimeError("500 batch failed")
            assert path == "/delete"
            single_calls.append(body["tumbler"])
            return {"deleted": 1}

        client._post = _fake_post_fallback  # type: ignore[method-assign]
        out2 = client.delete_many(tumblers2)

        assert single_calls == tumblers2
        assert out2 == set(tumblers2)

        # Empty input short-circuits with no request.
        assert client.delete_many([]) == set()

    def test_resolve_returns_entry_or_none_on_404(
        self, client: HttpCatalogClient, fake_server: str,
    ) -> None:
        entry = client.resolve("1.1.1")
        assert entry is not None
        assert entry.title == "Test Doc"

        # resolve() must return None (not raise) for 404.
        with HttpCatalogClient(base_url=fake_server, _token="test_tok") as c:
            def _fake_get(path, **params):
                resp = httpx.Response(404, json={"error": "not found"})
                raise httpx.HTTPStatusError("not found", request=None, response=resp)
            c._get = _fake_get
            result = c.resolve("9.9.9")
            assert result is None

    def test_resolve_alias_following_journey(
        self, client: HttpCatalogClient,
    ) -> None:
        """nexus-fguo5: resolve() declared follow_alias=True but never sent
        it on the wire. The fake decodes the SAME boolParam semantics the
        real engine's handleShow uses, so the wire-value assertions below
        fail red against the pre-fix client (which sent no follow_alias
        param at all -> the fake's raw="" -> decoded False, not True)."""
        # Default is follow_alias=True, sent on the wire.
        FakeCatalogHandler.last_show_follow_alias = None
        try:
            client.resolve("1.1.1")
            assert FakeCatalogHandler.last_show_follow_alias is True
        finally:
            FakeCatalogHandler.last_show_follow_alias = None

        # follow_alias=False is sent verbatim.
        FakeCatalogHandler.last_show_follow_alias = None
        try:
            client.resolve("1.1.1", follow_alias=False)
            assert FakeCatalogHandler.last_show_follow_alias is False
        finally:
            FakeCatalogHandler.last_show_follow_alias = None

        # With follow_alias=True (the default) and a seeded alias chain, the
        # returned entry's tumbler is the RESOLVED target, mirroring
        # CatalogRepository.getDocument(..., followAlias=True).
        FakeCatalogHandler.show_alias_map = {"2.2.2": "3.3.3"}
        try:
            entry = client.resolve("2.2.2")
            assert entry is not None
            assert str(entry.tumbler) == "3.3.3"
        finally:
            FakeCatalogHandler.show_alias_map = {}

        # follow_alias=False is byte-identical to a pre-fix client: the
        # alias is never followed, even though one is registered.
        FakeCatalogHandler.show_alias_map = {"2.2.2": "3.3.3"}
        try:
            entry = client.resolve("2.2.2", follow_alias=False)
            assert entry is not None
            assert str(entry.tumbler) == "2.2.2"
        finally:
            FakeCatalogHandler.show_alias_map = {}

        # resolve_alias() was an accidental identity function before
        # nexus-fguo5 (resolve() dropped follow_alias on the floor). With
        # the wire fixed, it returns the resolved target tumbler.
        FakeCatalogHandler.show_alias_map = {"2.2.2": "3.3.3"}
        try:
            target = client.resolve_alias("2.2.2")
            assert str(target) == "3.3.3"
        finally:
            FakeCatalogHandler.show_alias_map = {}

        # No alias registered -> resolve_alias() is the identity.
        target = client.resolve_alias("1.1.1")
        assert str(target) == "1.1.1"

    def test_find_and_all_documents(self, client: HttpCatalogClient) -> None:
        results = client.find("test query")
        assert len(results) >= 1
        assert results[0].title == "Test Doc"

        docs = client.all_documents()
        assert len(docs) == 2
        assert docs[1].title == "Second"

    def test_link_create_semantics(self, client: HttpCatalogClient) -> None:
        # Canonical Catalog.link() takes positional created_by and returns bool.
        result = client.link("1.1.1", "1.1.2", "cites", "test-suite")
        assert isinstance(result, bool)

        FakeCatalogHandler.link_created = True
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is True
        finally:
            FakeCatalogHandler.link_created = True

        # njrcn.3: created=False (ON CONFLICT merged an existing link) → link()
        # returns False, mirroring canonical (True=new, False=merged). This is
        # the branch that changed meaning (was result['ok'], now result['created']).
        FakeCatalogHandler.link_created = False
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is False
        finally:
            FakeCatalogHandler.link_created = True

        # Version-skew lock: a service that omits 'created' (old JAR) →
        # bool(None) → False.
        FakeCatalogHandler.link_created = None  # omit the key
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is False
        finally:
            FakeCatalogHandler.link_created = True

    # ── RDR-168 P3 wire-semantics regression (substantive-critic Critical) ────

    def test_all_documents_content_type_does_not_loop(
        self, client: HttpCatalogClient
    ) -> None:
        """all_documents(content_type=X, limit=0) issues ONE /list, never loops.

        The service's content_type branch ignores limit/offset and returns every row.
        A pagination loop would re-fetch the full (>=page) set forever. Regression guard
        for the infinite-loop Critical: assert a single /list request and all rows back.
        """
        FakeCatalogHandler.reset_log()
        FakeCatalogHandler.list_content_type_count = 1500  # >= the 1000 page size
        docs = client.all_documents(content_type="code")  # limit defaults to 0 (unbounded)
        assert len(docs) == 1500
        assert FakeCatalogHandler.get_ops.count("/list") == 1

    def test_descendants_uses_the_dedicated_route_not_a_list_page(
        self, client: HttpCatalogClient
    ) -> None:
        """descendants() hits GET /descendants — never a /list page.

        T2 nexus/chroma-residue-plan-2026-08-10 §C1: the pre-fix client issued
        ONE unfiltered ``GET /list?limit=500`` and filtered client-side, so
        every subtree not wholly inside that first page came back silently
        short (measured against the live 19,824-document catalog: 0% coverage
        on 11 of the 12 largest subtrees, no error raised).

        Pinning the ROUTE, not just the result, is the point. The client keeps
        a paginated /list fallback for engines predating the route, and that
        fallback is triggered by a 404 — which is exactly what the fake's
        catchall returns for an unrecognized GET. Asserting only the returned
        rows would therefore pass identically whether the route was used or
        silently skipped.
        """
        FakeCatalogHandler.reset_log()
        docs = client.descendants("1.2")
        assert FakeCatalogHandler.get_ops.count("/descendants") == 1
        assert FakeCatalogHandler.get_ops.count("/list") == 0
        assert [d["tumbler"] for d in docs] == ["1.2.1", "1.2.2"]

    def test_descendants_is_complete_past_the_old_single_page_cap(
        self, client: HttpCatalogClient
    ) -> None:
        """A subtree larger than the old 500-row page comes back WHOLE.

        The defect was not slowness, it was silent truncation: 4,797 documents
        under a prefix returned as 0 with no error. This pins the property that
        actually regressed — completeness — at a size the old implementation
        could not have satisfied, in a single request.
        """
        FakeCatalogHandler.reset_log()
        FakeCatalogHandler.descendants_count = 1500  # >> the old 500 cap
        docs = client.descendants("1.2")
        assert len(docs) == 1500
        assert FakeCatalogHandler.get_ops.count("/descendants") == 1
        assert all(d["tumbler"].startswith("1.2.") for d in docs)

    def test_link_if_absent_journey(self, client: HttpCatalogClient) -> None:
        """Existing link → skip (return False), NO /link write (no overwrite).

        Canonical link_if_absent is INSERT-OR-SKIP; the service POST /link is an UPSERT
        that would overwrite created_by/spans/meta. The pre-flight must short-circuit.
        """
        FakeCatalogHandler.reset_log()
        result = client.link_if_absent("1.1.1", "1.1.2", "cites", "indexer")
        assert result is False
        assert "/link" not in FakeCatalogHandler.post_ops

        # Absent link → write, with every caller param serialized onto the payload.
        FakeCatalogHandler.reset_log()
        result = client.link_if_absent(
            FakeCatalogHandler.link_absent_from, "1.1.2", "cites", "indexer",
            from_span="chash:aa", to_span="chash:bb", allow_dangling=True,
        )
        assert result is True
        assert "/link" in FakeCatalogHandler.post_ops
        body = FakeCatalogHandler.last_link_body
        assert body["created_by"] == "indexer"
        assert body["from_span"] == "chash:aa"
        assert body["to_span"] == "chash:bb"
        assert body["allow_dangling"] is True

    def test_bulk_unlink_journey(self, client: HttpCatalogClient) -> None:
        # dry_run=True returns the would-delete count via link_query, no /unlink POST.
        FakeCatalogHandler.reset_log()
        n = client.bulk_unlink(link_type="cites", dry_run=True)
        assert n == 1  # the fake /link_query reports one matching link
        assert "/unlink" not in FakeCatalogHandler.post_ops

        # Canonical parity: no filter and not dry_run → ValueError (guard against
        # mass delete).
        with pytest.raises(ValueError, match="at least one filter"):
            client.bulk_unlink()

        # bulk_unlink POSTs to /unlink (the same handler as unlink).
        n = client.bulk_unlink(link_type="cites")
        assert n == 1  # fake server returns {"deleted": 1}

    def test_links_query_journey(self, client: HttpCatalogClient) -> None:
        # GET /links?tumbler=X&direction=out
        links = client.links_from("1.1.1")
        assert len(links) == 1
        # Return-type parity: typed CatalogLink (attribute access), like local Catalog.
        assert links[0].link_type == "cites"
        assert str(links[0].to_tumbler) == "1.1.2"

        # njrcn.5: link_types is forwarded to the server-side IN filter (the fake
        # mirrors it), so a matching set returns the link and a non-matching set
        # returns nothing — no client-side over-fetch-then-filter.
        assert len(client.links_from("1.1.1", link_types=["cites", "relates"])) == 1
        assert client.links_from("1.1.1", link_types=["implements"]) == []

        # GET /links?tumbler=X&direction=in
        links_to = client.links_to("1.1.2")
        assert len(links_to) == 1
        assert links_to[0].link_type == "cites"
        assert str(links_to[0].from_tumbler) == "1.1.3"

        link_query_results = client.link_query(link_type="cites")
        assert len(link_query_results) == 1
        assert link_query_results[0].link_type == "cites"  # typed CatalogLink, not dict

    def test_manifest_write_get_and_chash_lookup(
        self, client: HttpCatalogClient
    ) -> None:
        # Return-type parity: batch get_manifests yields list[ManifestRow] per doc_id
        # (search_engine.py prefers this over the per-doc loop in service mode).
        by_doc = client.get_manifests(["1.1.1"])
        assert "1.1.1" in by_doc
        assert by_doc["1.1.1"][0].chash == CHASH_A
        assert by_doc["1.1.1"][0].position == 0

        # Must send 'rows' key not 'chunks'.
        client.write_manifest(
            "1.1.1", [{"position": 0, "chash": CHASH_A}], collection="code__test__v1")

        # GET /manifest/get?doc_id=X → response key 'rows'
        rows = client.get_manifest("1.1.1")
        assert len(rows) == 1
        # Return-type parity: typed ManifestRow (attribute access), like local Catalog.
        assert rows[0].chash == CHASH_A
        assert rows[0].position == 0

        # Pulls chashes from manifest rows (not a separate endpoint).
        chashes = client.get_chunk_chashes("1.1.1")
        assert CHASH_A in chashes

        chashes2 = client.chashes_for_collection("code__test__v1")
        assert CHASH_A in chashes2

    def test_get_manifests_pages_over_1000_doc_ids(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-gui8a: the service 400s on >1000 doc_ids per POST, so
        # get_manifests must page at _MANIFEST_GET_MANY_PAGE (1000) and merge.
        # 2500 ids -> 3 POSTs (1000 + 1000 + 500), each body <= 1000 doc_ids,
        # and the merged result must contain entries from every page.
        doc_ids = [f"doc-{i}" for i in range(2500)]
        posts: list[list[str]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/manifest/get_many"
            batch = body["doc_ids"]
            posts.append(batch)
            manifests = {
                did: [{"chash": CHASH_A, "position": 0}] for did in batch
            }
            return {"manifests": manifests, "count": len(manifests)}

        client._post = _fake_post  # type: ignore[method-assign]
        merged = client.get_manifests(doc_ids)

        assert len(posts) == 3
        assert [len(p) for p in posts] == [1000, 1000, 500]
        assert all(len(p) <= 1000 for p in posts)
        assert len(merged) == 2500
        assert "doc-0" in merged
        assert "doc-1500" in merged
        assert "doc-2499" in merged
        assert merged["doc-2499"][0].chash == CHASH_A

    def test_get_manifests_page_failure_fails_loud(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-gui8a follow-up: a page failure must propagate, not yield
        # a silent partial. Every caller handles the exception in its own
        # safe direction (staleness cache -> full re-index; doctor -> hard
        # error instead of phantom corruption).
        doc_ids = [f"doc-{i}" for i in range(2500)]
        calls: list[int] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            calls.append(len(body["doc_ids"]))
            if len(calls) == 2:  # second page (doc-1000..doc-1999) fails
                raise RuntimeError("transient 502")
            manifests = {
                did: [{"chash": CHASH_A, "position": 0}]
                for did in body["doc_ids"]
            }
            return {"manifests": manifests, "count": len(manifests)}

        client._post = _fake_post  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="transient 502"):
            client.get_manifests(doc_ids)
        assert calls == [1000, 1000]  # stopped at the failing page

    def test_get_manifests_missing_count_raises(
        self, client: HttpCatalogClient,
    ) -> None:
        # nexus-b9puj: the engine emits `count` unconditionally (floor >=
        # v0.1.61) — a response without it means a field-stripping hop
        # interposed, so the client refuses to merge an unverifiable page.
        def _fake_post(path: str, body: dict | None = None) -> Any:
            return {"manifests": {"1.1.1": [{"chash": CHASH_A, "position": 0}]}}

        client._post = _fake_post  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="count"):
            client.get_manifests(["1.1.1"])

    def test_get_manifests_fewer_than_requested_with_honest_count_is_legal(
        self, client: HttpCatalogClient,
    ) -> None:
        # substantive-critic pin (ocf52 wave, 2026-08-02): `count` is the
        # size of the RETURNED map (CatalogHandler.java builds it from
        # manifests.size() at serialization), NOT the size of the requested
        # batch — doc_ids with no manifest rows are legitimately absent.
        # A "simplification" that compares count against len(batch) would
        # pass every 1:1 test in this file and break six production call
        # sites the first time a batch contains a manifest-less doc. This
        # test is the tripwire: fewer-than-requested + honest count must
        # NOT raise and must return exactly what the engine sent.
        def _fake_post(path: str, body: dict | None = None) -> Any:
            return {
                "manifests": {"1.1.1": [{"chash": CHASH_A, "position": 0}]},
                "count": 1,
            }

        client._post = _fake_post  # type: ignore[method-assign]
        result = client.get_manifests(["1.1.1", "1.1.2"])
        assert set(result) == {"1.1.1"}

    def test_get_manifests_mismatched_count_raises(
        self, client: HttpCatalogClient,
    ) -> None:
        # A truncated page (fewer manifests than the server's own count)
        # must never be merged silently.
        def _fake_post(path: str, body: dict | None = None) -> Any:
            return {
                "manifests": {"1.1.1": [{"chash": CHASH_A, "position": 0}]},
                "count": 2,
            }

        client._post = _fake_post  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="doc_ids"):
            client.get_manifests(["1.1.1", "1.1.2"])

    def test_resolve_many_pages_over_1000_doc_ids(
        self, client: HttpCatalogClient
    ) -> None:
        # nexus-gui8a: /resolve_many enforces the same MAX_BATCH_DOC_IDS
        # = 1000 server cap as /manifest/get_many, so it pages identically.
        doc_ids = [f"doc-{i}" for i in range(2500)]
        posts: list[list[str]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/resolve_many"
            posts.append(body["doc_ids"])
            return {
                "entries": {
                    did: {"tumbler": f"1.9.{i}", "title": did}
                    for i, did in enumerate(body["doc_ids"])
                }
            }

        client._post = _fake_post  # type: ignore[method-assign]
        entries = client.resolve_many(doc_ids)

        assert [len(p) for p in posts] == [1000, 1000, 500]
        assert len(entries) == 2500
        assert str(entries["doc-0"].tumbler) == "1.9.0"
        assert str(entries["doc-2499"].tumbler) == "1.9.499"

    def test_graph_and_graph_many(self, client: HttpCatalogClient) -> None:
        # graph() must POST /traverse (not GET)
        result = client.graph("1.1.1")
        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result

        result_many = client.graph_many(["1.1.1", "1.1.2"])
        assert isinstance(result_many, dict)
        assert "nodes" in result_many

    def test_docs_for_chashes_journey(self, client: HttpCatalogClient) -> None:
        # nexus-h8rf6.3: the wire response is {"tumblers": [tumbler_string, ...]}
        # — a flat list from SELECT DISTINCT doc_id WHERE chash IN (...), NOT a
        # per-chash map. The client reconstructs the dict shape (matching local
        # Catalog.docs_for_chashes) via a second get_manifests() round-trip that
        # intersects each candidate doc's manifest chashes against the request.
        # Pre-fix this returned the flat list directly, which crashed every
        # ``by_chash.items()`` consumer (build_staleness_cache et al.) with
        # AttributeError, silently degrading every service-mode index run to a
        # full re-chunk + re-embed.
        result = client.docs_for_chashes([CHASH_A])
        assert isinstance(result, dict)
        assert result == {CHASH_A: ["1.1.1"]}

        assert client.docs_for_chashes([]) == {}

    # ── nexus-h8rf6.3: return-type regression pins ───────────────────────────
    #
    # Three call sites previously returned the WRONG wire-adjacent type
    # (bool instead of int, None instead of int, None instead of ""),
    # each silently breaking a caller's truthiness/count/sentinel check.

    def test_h8rf6_return_type_regression_pins(
        self, client: HttpCatalogClient
    ) -> None:
        # POST /delete with body {tumbler: ...} → {"deleted": 1}
        result = client.delete_document("1.1.1")
        assert result is True

        # pre-fix this returned a bool (deleted > 0), so
        # commands/catalog_cmds/links.py's "Removed {removed} link(s)" echoed
        # "Removed True link(s)" and mcp/catalog.py's {"removed": removed}
        # returned a bool instead of a count.
        removed = client.unlink("1.1.1", "1.1.2", "cites")
        assert removed == 1
        assert type(removed) is int

        # pre-fix this returned None, so indexer.py's
        # ``if rowcount == 0: _log.warning(...)`` (lost-write detector) could
        # never fire in service mode.
        updated = client.set_owner_head_hash("1.1", "deadbeef")
        assert updated == 1
        assert type(updated) is int

        # local Catalog's documented contract is "" (never None) on no-match;
        # align the service client to match.
        def _fake_get(path: str, **params: object) -> dict:
            return {"documents": []}
        client._get = _fake_get  # type: ignore[method-assign]
        result2 = client.lookup_doc_id_by_collection_and_path("code__x", "missing.py")
        assert result2 == ""

    def test_deactivate_owner_and_reactivate_owner_wire_shape(
        self, client: HttpCatalogClient
    ) -> None:
        """nexus-cw262: POST /owners/deactivate -> {"deactivated": N} and
        POST /owners/reactivate -> {"reactivated": N}, unwrapped to bool
        (mirrors delete_document's ``deleted`` > 0 -> bool contract)."""
        assert client.deactivate_owner("1.1") is True
        assert FakeCatalogHandler.last_owner_deactivate_body == {"tumbler_prefix": "1.1"}

        assert client.reactivate_owner("1.1") is True
        assert FakeCatalogHandler.last_owner_reactivate_body == {"tumbler_prefix": "1.1"}

    def test_list_owners_by_type_include_deactivated_threads_through_body(
        self, client: HttpCatalogClient
    ) -> None:
        """nexus-cw262: default False is NOT omitted from the POST body (unlike
        the GET-based list_owners' query-param omission) -- /owners/by_type is a
        POST, so a plain JSON `false` round-trips fine and CatalogHandler's
        ``includeDeactivatedRaw instanceof Boolean b && b`` reads it correctly
        either way.

        nexus-cw262 round-3 critique (T2 21467 Significant-4): the original
        version of this test asserted only ``isinstance(owners, list)``, which
        passes identically whether ``include_deactivated`` serialized
        correctly, wrong, or was silently dropped -- a vacuous non-regression
        guard. Assert the ACTUAL wire body FakeCatalogHandler captured
        (mirrors the file's own deactivate/reactivate wire-shape tests,
        which already capture+assert posted bodies) and, on the response
        side, that a True round-trip actually changes what comes back --
        the ``deactivated_at`` key FakeCatalogHandler only adds when it
        received a truthy ``include_deactivated``.
        """
        owners = client.list_owners_by_type("repo")
        assert FakeCatalogHandler.last_owners_by_type_body == {
            "owner_type": "repo", "include_deactivated": False,
        }
        assert "deactivated_at" not in owners[0]

        owners_audited = client.list_owners_by_type("repo", include_deactivated=True)
        assert FakeCatalogHandler.last_owners_by_type_body == {
            "owner_type": "repo", "include_deactivated": True,
        }
        assert owners_audited[0]["deactivated_at"] == "2026-08-05T00:00:00Z"

    def test_relation_counts_journey(self, client: HttpCatalogClient) -> None:
        # RDR-159 P-1a: POST /verify/relation-counts → {"counts": {rel: n}};
        # client unwraps the "counts" key and casts to int.
        counts = client.relation_counts(["nexus.memory", "nexus.plans"])
        assert counts == {"nexus.memory": 42, "nexus.plans": 42}

        assert client.relation_counts([]) == {}

    # test_manifest_backfill_and_verify_journey and test_manifest_orphans_journey
    # DELETED (RDR-191 Phase 6, nexus-o8dil.33): manifest_backfill,
    # manifest_verify, manifest_verify_all, and manifest_orphans client
    # methods are all retired — the manifest-chunk FK makes the dangling
    # state they detected/fixed unreachable. FakeCatalogHandler's route
    # branches for these are removed alongside these tests, its only
    # consumers.

    def test_chash_conformance_report_returns_per_table_counts(
        self, client: HttpCatalogClient,
    ) -> None:
        # RDR-180 (nexus-du2dw): GET /chash/conformance?dim= -> {dim, tables}
        result = client.chash_conformance_report(384)
        assert result["dim"] == 384
        assert len(result["tables"]) == 2
        names = {row["table_name"] for row in result["tables"]}
        assert names == {"nexus.chunks_384", "nexus.catalog_document_chunks"}
        assert all(row["non_conformant"] == 0 for row in result["tables"])

    def test_chash_conformance_report_rejects_unsupported_dim(
        self, client: HttpCatalogClient,
    ) -> None:
        import pytest as _pytest
        with _pytest.raises(ValueError, match="dim must be one of"):
            client.chash_conformance_report(512)

    def test_index_run_lifecycle_happy_path(
        self, client: HttpCatalogClient
    ) -> None:
        FakeCatalogHandler.reset_log()
        client.begin_index_run("1.1.1", "abc123", "run-1", "docs__o__v1")
        assert FakeCatalogHandler.last_begin_index_run_body == {
            "doc_id": "1.1.1", "content_hash": "abc123",
            "run_id": "run-1", "collection": "docs__o__v1",
        }

        # nexus-vw594 F1: the batch begin-many round trip.
        docs = [
            {"doc_id": "1.1.1", "content_hash": "abc", "run_id": "run-1"},
            {"doc_id": "1.1.2", "content_hash": "def", "run_id": "run-1"},
        ]
        result = client.begin_index_run_many(docs, "code__o__v1")
        assert FakeCatalogHandler.last_begin_index_run_many_body == {
            "docs": docs, "collection": "code__o__v1",
        }
        assert result == {"docs": 2, "failed_doc_ids": []}

        result_complete = client.complete_index_run("1.1.1", "abc123", 2)
        assert result_complete == {"referenced": 2, "present": 2, "missing": 0, "flagged": False}

        client.fail_index_run("1.1.1", "MinerU OOM")
        assert FakeCatalogHandler.last_fail_index_run_body == {
            "doc_id": "1.1.1", "error": "MinerU OOM",
        }

    def test_complete_index_run_409_raises_typed_exception_with_counts(
        self, client: HttpCatalogClient,
    ) -> None:
        from nexus.errors import IndexRunVerifyRefused

        FakeCatalogHandler.reset_log()
        FakeCatalogHandler.complete_index_run_conflict = True
        try:
            with pytest.raises(IndexRunVerifyRefused) as excinfo:
                client.complete_index_run("1.1.1", "abc123", 3)
            exc = excinfo.value
            assert exc.doc_id == "1.1.1"
            assert exc.referenced == 3
            assert exc.present == 1
            assert exc.missing == 2
            assert exc.chunk_count == 3
            # nexus-5xn3k.3 review item 5: the counts summary is ALWAYS in
            # the message; the fake's server-supplied "error" string is
            # APPENDED as supplementary detail, never a replacement.
            message = str(exc)
            assert "referenced=3" in message
            assert "present=1" in message
            assert "missing=2" in message
            assert "claimed_chunk_count=3" in message
            assert "completeIndexRun refused" in message
            # the fake's {"error": "completeIndexRun refused"} body is
            # appended verbatim as supplementary engine detail, not swapped
            # in for the counts summary above.
            assert "(engine: completeIndexRun refused)" in message
        finally:
            FakeCatalogHandler.complete_index_run_conflict = False

    def test_service_catalog_writer_dispatches_index_run_ops_end_to_end(
        self, fake_server: str,
    ) -> None:
        """nexus-kgos1 trap, at the call site: a REAL _ServiceCatalogWriter
        wrapping a REAL HttpCatalogClient (never a MagicMock) must dispatch
        the 3 new write ops through to the wire — proving both that they are
        in the CATALOG_WRITE_OPS whitelist (an omission raises AttributeError
        here, not in some unit test of the whitelist tuple alone) AND that
        the whole call path (attribute resolution -> HTTP POST -> fake
        server) actually works.
        """
        from nexus.catalog.factory import _ServiceCatalogWriter

        FakeCatalogHandler.reset_log()
        client = HttpCatalogClient(base_url=fake_server, _token="test_tok")
        writer = _ServiceCatalogWriter(client)
        try:
            writer.begin_index_run("1.1.1", "abc123", "run-1", "docs__o__v1")
            assert FakeCatalogHandler.last_begin_index_run_body["doc_id"] == "1.1.1"

            result = writer.complete_index_run("1.1.1", "abc123", 2)
            assert result["missing"] == 0

            writer.fail_index_run("1.1.1", "boom")
            assert FakeCatalogHandler.last_fail_index_run_body["error"] == "boom"
        finally:
            writer.close()

    def test_collections_list_and_supersede(self, client: HttpCatalogClient) -> None:
        colls = client.list_collections()
        assert len(colls) == 1

        # nexus-cecqy: the engine returns {"updated": N} and the client used to
        # DISCARD it, asserting only `result is None` — wire shape, not behaviour.
        #
        # That discard is what let `nx catalog rename-collection` announce
        # "Emitted CollectionSuperseded(...)" after an UPDATE that touched ZERO
        # rows: service-mode rename DELETEs the old registry row, so the
        # follow-up `WHERE name = old` matches nothing. ZERO is the meaningful
        # value, and it was the one being thrown away.
        #
        # (The fake has been returning {"updated": 5} the whole time — the
        # count was available and unused.)
        assert client.supersede_collection("old__coll", "new__coll") == 5

    @pytest.mark.parametrize(
        ("status", "detail"),
        [
            (404, "collection not found: old__coll"),
            (404, "superseded_by names an unregistered collection: new__coll"),
            (409, "collection old__coll is already superseded by other__coll"),
        ],
    )
    def test_supersede_refusal_raises_value_error_carrying_the_reason(
        self, client: HttpCatalogClient, status: int, detail: str,
    ) -> None:
        """nexus-g8z8n: the engine's three precondition refusals must surface as
        ValueError, not as a silent zero or a bare HTTPStatusError.

        The reason has to travel with it — an operator who typoed a collection
        name needs to be told WHICH endpoint did not resolve, and the engine
        already says so in the response body.
        """
        request = httpx.Request("POST", "http://engine/collections/supersede")
        response = httpx.Response(status, json={"error": detail}, request=request)

        def _fake_post(path: str, body: dict) -> dict:
            raise httpx.HTTPStatusError("refused", request=request, response=response)

        client._post = _fake_post  # type: ignore[method-assign]

        with pytest.raises(ValueError) as excinfo:
            client.supersede_collection("old__coll", "new__coll")
        # Non-vacuity: the message must name the operands AND carry the engine's
        # own explanation, not just the status code.
        assert "old__coll" in str(excinfo.value)
        assert detail in str(excinfo.value)

    def test_supersede_other_http_errors_are_not_swallowed_as_value_error(
        self, client: HttpCatalogClient,
    ) -> None:
        """Only the two precondition statuses map. A 500 is an engine fault, not
        a caller mistake, and must keep raising what it raises — otherwise an
        outage reads to every caller as 'you passed a bad name'."""
        request = httpx.Request("POST", "http://engine/collections/supersede")
        response = httpx.Response(500, json={"error": "boom"}, request=request)

        def _fake_post(path: str, body: dict) -> dict:
            raise httpx.HTTPStatusError("boom", request=request, response=response)

        client._post = _fake_post  # type: ignore[method-assign]

        with pytest.raises(httpx.HTTPStatusError):
            client.supersede_collection("old__coll", "new__coll")

    # ── nexus-cecqy: legacy_grandfathered is DERIVED, not defaulted False ────

    def _upsert_body(self, client: HttpCatalogClient, *a: object, **kw: object) -> dict:
        """Call register_collection and return the body it POSTed."""
        sent: dict = {}

        def _fake_post(path: str, body: dict) -> dict:
            assert path == "/collections/upsert", path
            sent.update(body)
            return {}

        client._post = _fake_post  # type: ignore[method-assign]
        client.register_collection(*a, **kw)  # type: ignore[arg-type]
        return sent

    def test_legacy_grandfathered_derivation_journey(
        self, client: HttpCatalogClient,
    ) -> None:
        from nexus.corpus import is_conformant_collection_name

        # The BARE ``register_collection(name)`` shape — by construction the
        # non-conformant branch of every call site's conformance fork — must
        # send ``legacy_grandfathered=True``. It used to send the kwarg's
        # False default, so non-conformant names landed un-flagged in
        # service mode. The engine infers nothing (``upsertCollection`` binds
        # the caller's value), so whatever this sends IS the stored flag.
        name = "docs__cecqy-legacy"
        # Non-vacuity: the fix is only observable on a non-conformant name.
        assert not is_conformant_collection_name(name)
        assert self._upsert_body(client, name)["legacy_grandfathered"] is True

        # The other half of the derivation. Pins against a 'fix' that
        # blanket-flags every bare registration True — the conformant case
        # was correct by accident at the old False default, which is what
        # hid the defect.
        conformant_name = "docs__cecqy-conf__stub-docs-1024__v1"
        assert is_conformant_collection_name(conformant_name)
        assert self._upsert_body(client, conformant_name)["legacy_grandfathered"] is False

        # Deriving must not take the override away: a caller that must force
        # the flag still can, in BOTH directions.
        forced_on = self._upsert_body(
            client, conformant_name, legacy_grandfathered=True,
        )
        assert forced_on["legacy_grandfathered"] is True

        forced_off = self._upsert_body(
            client, name, legacy_grandfathered=False,
        )
        assert forced_off["legacy_grandfathered"] is False

    def test_rename_collection_journey(self, client: HttpCatalogClient) -> None:
        # Sends {old_name, new_name} (canonical form)
        FakeCatalogHandler.last_rename_body = {}
        n = client.rename_collection("old__coll", "new__coll")
        assert n == 3
        # nexus-gaou3: default rename omits cross_model (server 409s an existing target).
        assert "cross_model" not in FakeCatalogHandler.last_rename_body

        # nexus-gaou3: the deliberate cross-model repoint sends cross_model:true
        # so the server takes the RDR-162 COPY branch instead of 409ing the
        # existing target.
        FakeCatalogHandler.last_rename_body = {}
        client.rename_collection("old__coll", "new__coll", cross_model=True)
        assert FakeCatalogHandler.last_rename_body.get("cross_model") is True

        FakeCatalogHandler.last_rename_body = {}
        client.rename_collection_cascade("old__coll", "new__coll", cross_model=True)
        assert FakeCatalogHandler.last_rename_body.get("cross_model") is True

        # nexus-gaou3: a plain rename onto an existing target gets a 409 from the
        # server; the client must surface it (not swallow it into a 0 count).
        FakeCatalogHandler.rename_conflicts = True
        try:
            with pytest.raises(httpx.HTTPStatusError):
                client.rename_collection("old__coll", "new__coll")

            # nexus-gaou3: cross_model=True takes the RDR-162 repoint branch
            # even when the server would 409 a plain rename — no exception,
            # repoint count returned.
            n2 = client.rename_collection("old__coll", "new__coll", cross_model=True)
            assert n2 == 3
        finally:
            FakeCatalogHandler.rename_conflicts = False

    def test_owner_registration_and_misc_writes(
        self, client: HttpCatalogClient
    ) -> None:
        # Canonical Catalog.update_documents_collection_batch() takes pairs:
        # list[tuple[str,str]]. Migrated from old client-specific sig
        # (tumblers list + collection string).
        n = client.update_documents_collection_batch(
            [("1.1.1", "new__coll"), ("1.1.2", "new__coll")]
        )
        assert n == 2

        # Uses POST /owners/upsert
        t = client.register_owner(name="acme")
        assert isinstance(t, Tumbler)

        t2 = client.ensure_owner_for_repo(repo="/tmp/myrepo")
        assert isinstance(t2, Tumbler)

        # POST /owners/head_hash {tumbler_prefix, head_hash}
        client.set_owner_head_hash("1.1", "abc123def456")  # must not raise

        # Must not raise
        client.resync_chunk_count_cache("1.1.1")
# ── Guarded methods ───────────────────────────────────────────────────────────

class TestGuardedMethods:
    def test_rebuild_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="rebuild"):
            client.rebuild()

    def test_defrag_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="defrag"):
            client.defrag()

    def test_compact_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="compact"):
            client.compact()

    def test_sync_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="sync"):
            client.sync()

    def test_pull_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="pull"):
            client.pull()

    def test_rebuild_if_stale_noop(self, client: HttpCatalogClient) -> None:
        client.rebuild_if_stale()  # must NOT raise

    def test_catalog_path_is_none(self, client: HttpCatalogClient) -> None:
        assert client.catalog_path is None


# ── Factory seam tests ────────────────────────────────────────────────────────

class TestFactorySeam:
    @pytest.fixture(autouse=True)
    def _reset_shared_service_catalog_client(self):
        """nexus-5en9j: service-mode readers/writers now share ONE
        process-lifetime HttpCatalogClient (module-global state in
        catalog/factory.py), not a fresh instance per call. Reset before
        AND after each test so this test class's real (unmocked)
        HttpCatalogClient construction never leaks into a sibling test."""
        from nexus.catalog.factory import reset_shared_service_catalog_client_for_tests

        reset_shared_service_catalog_client_for_tests()
        yield
        reset_shared_service_catalog_client_for_tests()

    def test_make_catalog_reader_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nexus-5en9j: service-mode readers share a process-lifetime
        HttpCatalogClient behind a proxy handle, not a fresh instance per
        call -- so this asserts duck-typed behavior (has the client's
        read surface, forwards to a real HttpCatalogClient under the
        hood) rather than isinstance, which the proxy deliberately
        breaks."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import (
            _SharedServiceCatalogHandle,
            make_catalog_reader,
            reset_shared_service_catalog_client_for_tests,
        )

        reader = make_catalog_reader()
        try:
            assert isinstance(reader, _SharedServiceCatalogHandle)
            assert isinstance(reader.catalog_path, type(None))  # forwards to the underlying HttpCatalogClient property
            reader.close()  # deliberately a no-op; must not raise
        finally:
            reset_shared_service_catalog_client_for_tests()

    def test_make_catalog_writer_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer

        writer = make_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter)
        assert writer.routed is True
        writer.close()

    def test_service_catalog_writer_whitelist_blocks_read_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        with pytest.raises(AttributeError, match="not a catalog write op"):
            _ = writer.resolve  # read op — must be blocked
        writer.close()

    def test_service_catalog_writer_whitelist_allows_write_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        for op in CATALOG_WRITE_OPS:
            attr = getattr(writer, op, None)
            assert attr is not None, f"write op {op!r} missing from _ServiceCatalogWriter"
        writer.close()

    def test_is_interactive_write_pending_false_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_ServiceCatalogWriter.is_interactive_write_pending() returns False.

        Correct in service mode: the write-pending state is maintained server-side,
        not in the Python process.  The Python writer is a stateless RPC proxy.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        assert writer.is_interactive_write_pending() is False
        writer.close()

    def test_mcp_path_routes_to_http_catalog_client_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP catalog server (mcp_infra.get_catalog_writer) routes to
        HttpCatalogClient when NX_STORAGE_BACKEND_CATALOG=service.

        This is the critical seam: mcp/catalog.py calls _get_catalog_writer() which
        calls mcp_infra.get_catalog_writer() which calls make_catalog_writer() from
        factory.py.  If any step in this chain bypasses the factory, the service
        routing would be silently skipped.  This test locks the full chain.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok-mcp-test")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer
        from nexus.mcp_infra import get_catalog_writer

        writer = get_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter), (
            f"MCP catalog path returned {type(writer)!r} instead of "
            f"_ServiceCatalogWriter; factory seam broken"
        )
        assert writer.routed is True
        writer.close()

    def test_no_production_bypass_of_factory(self) -> None:
        """No Python source file in src/ should bare-construct HttpCatalogClient
        outside of factory.py (seam audit).
        """
        import subprocess
        from pathlib import Path
        # Repo root derived from this file's location, not a hardcoded
        # absolute path — the latter breaks on CI runners (the dir does
        # not exist there). tests/catalog/test_http_catalog_client.py
        # → parents[2] is the repo root.
        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["grep", "-rn", "HttpCatalogClient(", "--include=*.py", "src/"],
            cwd=str(repo_root),
            capture_output=True, text=True,
        )
        hits = [
            line for line in result.stdout.splitlines()
            if "factory.py" not in line
            and "http_catalog_client.py" not in line
        ]
        assert hits == [], (
            "Production code constructs HttpCatalogClient directly, bypassing factory:\n"
            + "\n".join(hits)
        )


# ── resolve_span / resolve_chash unit tests (nexus-njrcn.4) ─────────────────

# 64-char hex chash for test fixtures (all must be valid [0-9a-f]{64})
_FULL_CHASH = "deadbeef" * 8               # 64 hex chars
_CHASH_32   = "deadbeef" * 4               # first 32 chars (server key)
_MISSING_CHASH = "feeded00" * 8            # 64-char hex for 404 path
_MISSING_32 = "feeded00" * 4              # first 32 chars
_GLOBAL_CHASH_FULL = "aabbccdd" * 8        # 64-char hex for global lookup
_GLOBAL_CHASH_32   = "aabbccdd" * 4        # first 32 chars
_MISS_GLOBAL_FULL  = "00000000" * 8        # 64-char hex — missing in server
_LEGACY_CHASH_32   = "aabbccdd" * 4        # pre-RDR-180 32-hex ref, aliases to _GLOBAL_CHASH_FULL
_MISS_GLOBAL_32    = "00000000" * 4        # first 32 chars


class TestResolveSpan:
    """Unit tests for HttpCatalogClient.resolve_span (nexus-njrcn.4)."""

    def test_resolve_span_returns_chunk_text(self) -> None:
        """Happy path: correct dict shape with chunk_text and metadata."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_FULL_CHASH}",
                "knowledge__o__bge-768__v1",
            )
            assert result is not None
            assert result["chunk_text"] == "hello span world"
            assert result["metadata"] == {"lang": "en"}
            # chunk_hash carries the full 64-char hex (from parse_chash_span), not the 32-char server key
            assert result["chunk_hash"] == _FULL_CHASH
            assert "char_range" not in result
        finally:
            server.shutdown()

    def test_resolve_span_applies_char_range(self) -> None:
        """char_range slices chunk_text and is included in the output dict."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            # generic chash (not deadbeef) to hit the "generic chunk text" branch
            generic_chash = "cafebabe" * 8
            result = client.resolve_span(
                f"chash:{generic_chash}:8-13",
                "knowledge__o__bge-768__v1",
            )
            assert result is not None
            # "generic chunk text"[8:13] == "chunk"
            assert result["chunk_text"] == "chunk"
            assert result["char_range"] == (8, 13)
        finally:
            server.shutdown()

    def test_resolve_span_non_chash_returns_none(self) -> None:
        """Non-chash span (e.g. line-range) returns None without HTTP call."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span("42-57", "knowledge__o__bge-768__v1")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_404_returns_none(self) -> None:
        """A 404 from the server maps to None (chunk not found)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_MISSING_CHASH}",
                "knowledge__o__bge-768__v1",
            )
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_malformed_chash_returns_none(self) -> None:
        """Malformed chash span returns None (ValueError caught gracefully)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span("chash:not-a-hex", "knowledge__o__bge-768__v1")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_t3_ignored(self) -> None:
        """t3 kwarg is accepted (conformance) and silently ignored."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_FULL_CHASH}",
                "knowledge__o__bge-768__v1",
                t3=object(),  # arbitrary non-None value
            )
            assert result is not None
            assert result["chunk_text"] == "hello span world"
        finally:
            server.shutdown()


class TestResolveChash:
    """Unit tests for HttpCatalogClient.resolve_chash (nexus-njrcn.4)."""

    def test_resolve_chash_returns_full_dict(self) -> None:
        """Happy path: correct dict shape with all expected keys."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_GLOBAL_CHASH_FULL}")
            assert result is not None
            # Canonical contract: chash/chunk_hash are the FULL 64-char parsed hex,
            # not the 32-char wire key the service stores (njrcn.4 review High).
            assert result["chash"] == _GLOBAL_CHASH_FULL
            assert result["chunk_hash"] == _GLOBAL_CHASH_FULL
            assert result["physical_collection"] == "knowledge__o__bge-768__v1"
            assert result["doc_id"] == "1.2.3"
            assert result["chunk_text"] == "resolved chunk body"
            assert result["metadata"] == {"source": "test"}
            assert "char_range" not in result
        finally:
            server.shutdown()

    def test_resolve_chash_applies_char_range(self) -> None:
        """char_range slices chunk_text and is included in output.

        The span form ``chash:<hex>:<start>-<end>`` passes start/end to the
        client which parses them via parse_chash_span; the client sends the
        FULL-width chash to the server (RDR-180) and slices the returned
        text locally. Server returns "resolved chunk body"; slice [9:14]
        == "chunk".
        """
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_GLOBAL_CHASH_FULL}:9-14")
            assert result is not None
            assert result["chunk_text"] == "chunk"
            assert result["char_range"] == (9, 14)
        finally:
            server.shutdown()

    def test_resolve_chash_404_returns_none(self) -> None:
        """A 404 from the server maps to None."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_MISS_GLOBAL_FULL}")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_chash_alias_chains_a_legacy_width_ref(self) -> None:
        """nexus-84tr4: a legacy 32-hex ref must resolve, not silently MISS.

        /resolve_chash has no alias fallback (that lives on /v1/chash/lookup),
        so resolve_chash used to MISS on exactly the refs
        resolve_chash_globally RESOLVED — two functions disagreeing about one
        identifier space. Harmless on a fully rekeyed store, where callers
        read 64-hex manifest chashes; a silent miss for a user pasting a
        legacy citation, or on any un-rekeyed store.
        """
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_LEGACY_CHASH_32}")
            assert result is not None, "legacy-width ref must alias-chain, not miss"
            assert result["chunk_text"] == "resolved chunk body"
            # The CANONICAL identity comes back, not the legacy ref that went
            # in — the same rewrite the citation resolver performs, so a
            # caller comparing against a 64-char citation matches.
            assert result["chash"] == _GLOBAL_CHASH_FULL
            assert result["chunk_hash"] == _GLOBAL_CHASH_FULL
        finally:
            server.shutdown()

    def test_resolve_chash_alias_retry_preserves_char_range(self) -> None:
        """The span slice must survive the alias retry."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_LEGACY_CHASH_32}:9-14")
            assert result is not None
            assert result["chunk_text"] == "chunk"
            assert result["char_range"] == (9, 14)
        finally:
            server.shutdown()

    def test_resolve_chash_genuine_miss_does_not_loop(self) -> None:
        """A ref unknown to BOTH routes still returns None, after one retry."""
        server, base_url = start_fake_server()
        try:
            FakeCatalogHandler.get_ops.clear()
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            assert client.resolve_chash(f"chash:{_MISS_GLOBAL_FULL}") is None
            resolves = [o for o in FakeCatalogHandler.get_ops if o == "/resolve_chash"]
            assert len(resolves) == 1, (
                "the alias route reported no different canonical identity, so "
                f"there is nothing to retry; got {FakeCatalogHandler.get_ops}"
            )
        finally:
            server.shutdown()

    def test_resolve_chash_prefer_collection_forwarded(self) -> None:
        """prefer_collection kwarg is forwarded as a query param."""
        server, base_url = start_fake_server()
        try:
            FakeCatalogHandler.reset_log()
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(
                f"chash:{_GLOBAL_CHASH_FULL}",
                prefer_collection="knowledge__o__bge-768__v1",
            )
            assert result is not None
            # The server saw the resolve_chash GET
            assert "/resolve_chash" in FakeCatalogHandler.get_ops
        finally:
            server.shutdown()

    def test_resolve_chash_t3_and_chash_index_ignored(self) -> None:
        """t3 and chash_index positional args are accepted and silently ignored."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(
                f"chash:{_GLOBAL_CHASH_FULL}",
                object(),   # t3 — positional, must be accepted
                object(),   # chash_index — positional, must be accepted
            )
            assert result is not None
            assert result["chunk_text"] == "resolved chunk body"
        finally:
            server.shutdown()

    def test_resolve_chash_malformed_returns_none(self) -> None:
        """Malformed chash returns None (ValueError caught gracefully)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash("chash:not-a-valid-hex")
            assert result is None
        finally:
            server.shutdown()


class TestResolveChunk:
    """nexus-gc2ze: HttpCatalogClient.resolve_chunk() real service-mode
    chunk-address resolution (replaces the ``resolve(tumbler).__dict__``
    placeholder that treated any tumbler as a document)."""

    def test_resolve_chunk_returns_full_dict(self) -> None:
        """Happy path: a real 4-segment chunk tumbler resolves to the
        document + chunk metadata dict."""
        from nexus.catalog.tumbler import Tumbler

        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            t = Tumbler(segments=(1, 1, 1, 2))
            result = client.resolve_chunk(t)
            assert result is not None
            assert result["document_tumbler"] == "1.1.1"
            assert result["chunk_index"] == 2
            assert result["physical_collection"] == "code__test__voyage-code-3__v1"
            assert result["title"] == "Test Doc"
            assert result["content_type"] == "code"
        finally:
            server.shutdown()

    def test_resolve_chunk_accepts_string_tumbler(self) -> None:
        """String tumblers are parsed before the chunk-address check."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chunk("1.1.1.0")
            assert result is not None
            assert result["chunk_index"] == 0
        finally:
            server.shutdown()

    def test_resolve_chunk_non_chunk_tumbler_returns_none_without_wire_call(self) -> None:
        """A plain 3-segment document tumbler short-circuits to None locally
        with no wire round-trip — mirroring the local
        ``Catalog.resolve_chunk``'s ``if tumbler.chunk is None: return None``."""
        from nexus.catalog.tumbler import Tumbler

        server, base_url = start_fake_server()
        try:
            FakeCatalogHandler.reset_log()
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chunk(Tumbler(segments=(1, 1, 1)))
            assert result is None
            assert "/resolve_chunk" not in FakeCatalogHandler.get_ops
        finally:
            server.shutdown()

    def test_resolve_chunk_missing_document_returns_none(self) -> None:
        """A 404 from the server (document not found) maps to None."""
        from nexus.catalog.tumbler import Tumbler

        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chunk(Tumbler(segments=(9, 9, 999, 0)))
            assert result is None
        finally:
            server.shutdown()


class TestByFilePathExactMatchGuard:
    """GH #1350 / nexus-h9f1w: by_file_path(owner, fp) must return None for a
    brand-new file even when the service /list ignores file_path under owner and
    returns the FULL owner list. Trusting docs[0] mis-attributed a new file's
    chunks to an unrelated doc, overwriting that doc's manifest (silent data
    corruption, fired twice in prod). The client MUST filter by exact file_path.
    """

    def _client_returning(self, fake_server: str, documents: list[dict]):
        c = HttpCatalogClient(base_url=fake_server, _token="test_tok")

        def _fake_get(path: str, **params: Any) -> dict:
            # Reproduce the buggy server: owner+file_path ignores file_path and
            # returns the entire owner list regardless of the file_path param.
            return {"documents": documents}

        c._get = _fake_get  # type: ignore[method-assign]
        return c

    def test_new_file_under_populated_owner_returns_none(self, fake_server: str) -> None:
        """The corruption trigger: querying a NEW path returns None, not docs[0]."""
        owner_list = [
            {"tumbler": "1.12.1", "title": "Beyond Similarity Search", "file_path": "existing/a.pdf"},
            {"tumbler": "1.12.2", "title": "Other", "file_path": "existing/b.pdf"},
        ]
        c = self._client_returning(fake_server, owner_list)
        assert c.by_file_path("1.12", "brand/new/paper.pdf") is None

    def test_existing_file_returns_its_own_entry_not_docs0(self, fake_server: str) -> None:
        """A real match is selected by exact file_path even when it is NOT docs[0]."""
        owner_list = [
            {"tumbler": "1.12.1", "title": "Beyond Similarity Search", "file_path": "existing/a.pdf"},
            {"tumbler": "1.12.2", "title": "Target", "file_path": "existing/b.pdf"},
        ]
        c = self._client_returning(fake_server, owner_list)
        entry = c.by_file_path("1.12", "existing/b.pdf")
        assert entry is not None
        assert str(entry.tumbler) == "1.12.2"

    def test_empty_owner_returns_none(self, fake_server: str) -> None:
        c = self._client_returning(fake_server, [])
        assert c.by_file_path("1.12", "any/path.pdf") is None


# ── nexus-5i864: resolve_path owner cache ────────────────────────────────────


class TestResolvePathOwnerCache:
    """The owner lookup ``resolve_path`` added is CACHED — and the cache's
    two contracts both carry correctness weight, so both are pinned here.

    HITS are cached because the link generator drives ``resolve_path`` in
    loops over tumblers that overwhelmingly share one owner. MISSES are
    NOT, because ``HttpCatalogClient`` is a process-lifetime singleton
    (catalog/factory.py, nexus-53x7s): a pinned miss would mean an owner
    registered later by another process is never observed, and
    ``resolve_path`` would keep answering None — silently zeroing the
    auto-linker for that repo for the life of the process, which is the
    exact failure shape nexus-5i864 exists to remove.
    """

    def _client(self, monkeypatch: pytest.MonkeyPatch, owners: dict):
        from types import SimpleNamespace

        c = object.__new__(HttpCatalogClient)
        c._resolve_path_owner_cache = {}
        calls: list[str] = []

        def _fake_owner(prefix: str):
            calls.append(prefix)
            return owners.get(prefix)

        monkeypatch.setattr(
            c, "get_owner_by_prefix", _fake_owner, raising=False,
        )
        monkeypatch.setattr(
            c, "resolve",
            lambda t, **kw: SimpleNamespace(file_path="src/a.py", tumbler=t),
            raising=False,
        )
        return c, calls

    def test_hit_is_cached_across_calls_sharing_an_owner(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owners = {"1.1": {"owner_type": "repo", "repo_root": "/repo"}}
        c, calls = self._client(monkeypatch, owners)

        assert c.resolve_path(Tumbler.parse("1.1.1")) == Path("/repo/src/a.py")
        assert c.resolve_path(Tumbler.parse("1.1.2")) == Path("/repo/src/a.py")

        assert calls == ["1.1"], (
            f"owner lookup should happen once for a shared owner; got {calls}"
        )

    def test_miss_is_never_cached_so_a_later_registration_is_observed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The correctness half: an owner that appears AFTER a miss (another
        process registering it) must be picked up, not pinned to None."""
        owners: dict = {}
        c, calls = self._client(monkeypatch, owners)

        assert c.resolve_path(Tumbler.parse("1.1.1")) is None
        assert calls == ["1.1"]

        # Another process registers the owner; this client never saw the write.
        owners["1.1"] = {"owner_type": "repo", "repo_root": "/repo"}

        assert c.resolve_path(Tumbler.parse("1.1.1")) == Path("/repo/src/a.py"), (
            "a cached miss pinned the owner as absent — the auto-linker would "
            "stay silently zeroed for this repo for the life of the process"
        )
        assert calls == ["1.1", "1.1"], "the miss path must re-query, not serve a cached None"

    def test_owner_upsert_invalidates_a_cached_hit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owners = {"1.1": {"owner_type": "repo", "repo_root": "/old"}}
        c, calls = self._client(monkeypatch, owners)
        # The upsert echoes the assigned prefix, so register_owner returns
        # without its /owners/by_name fallback read.
        monkeypatch.setattr(
            c, "_post", lambda *a, **kw: {"tumbler_prefix": "1.1"}, raising=False,
        )

        assert c.resolve_path(Tumbler.parse("1.1.1")) == Path("/old/src/a.py")

        owners["1.1"] = {"owner_type": "repo", "repo_root": "/new"}
        c.register_owner("repo-name", "repo", repo_root="/new")

        assert c.resolve_path(Tumbler.parse("1.1.1")) == Path("/new/src/a.py"), (
            "register_owner must drop the cache so a changed repo_root is seen"
        )

    def test_curator_owner_is_cached_but_still_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A curator hit is a legitimate cache entry; the guard still fires."""
        owners = {"1.1": {"owner_type": "curator", "repo_root": ""}}
        c, calls = self._client(monkeypatch, owners)

        assert c.resolve_path(Tumbler.parse("1.1.1")) is None
        assert c.resolve_path(Tumbler.parse("1.1.2")) is None
        assert calls == ["1.1"]


# ── nexus-ai41v / nexus-9ssih: link audit + dangling-endpoint translation ─────


class TestLinkAuditIsNoLongerAStub:
    """nexus-ai41v: link_audit used to `return {}`.

    Service mode is now EVERY mode and the verb ships on two surfaces, so an
    empty dict was not graceful degradation — `--json` printed `{}`, which
    reads as a CLEAN audit. Everything it reports is computed from reads the
    engine already serves, so no engine change was needed.
    """

    def test_audit_reports_real_totals_not_an_empty_dict(
        self, client: HttpCatalogClient,
    ) -> None:
        audit = client.link_audit()
        assert audit != {}, "an empty audit reads as CLEAN — the false-clean shape"
        for key in (
            "total", "by_type", "by_creator",
            "duplicate_count", "duplicates", "orphaned_count", "orphaned",
        ):
            assert key in audit, f"CLI/MCP consumers read {key!r}; it must be present"
        assert audit["total"] >= 1
        assert audit["by_type"].get("cites", 0) >= 1

    def test_audit_counts_orphans_from_the_engine_route(
        self, client: HttpCatalogClient,
    ) -> None:
        audit = client.link_audit()
        assert audit["orphaned_count"] == len(audit["orphaned"])


class TestDanglingEndpointBecomesValueError:
    """nexus-9ssih CLIENT HALF, landed AHEAD of its engine half.

    auto_linker counts skipped_missing_endpoint inside `except ValueError`, so
    the engine's future 400 must arrive as ValueError or every install takes an
    uncaught httpx.HTTPStatusError on its next index pass.
    """

    def _client_raising(self, monkeypatch: pytest.MonkeyPatch, status: int, body: dict):
        c = object.__new__(HttpCatalogClient)

        def _boom(path, payload):
            request = httpx.Request("POST", "http://svc/v1/catalog/link")
            response = httpx.Response(status, json=body, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

        monkeypatch.setattr(c, "_post", _boom, raising=False)
        return c

    def test_dangling_endpoint_code_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = self._client_raising(
            monkeypatch, 400,
            {"error": "dangling link endpoint", "code": "dangling_endpoint",
             "missing": ["to_tumbler"]},
        )
        with pytest.raises(ValueError, match="dangling link endpoint"):
            c.link("1.1.1", "1.9.9", "cites", "auto-linker")

    def test_other_400s_are_not_swallowed_into_value_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discriminate on `code`, never on the bare status — a malformed-body
        400 must keep raising exactly what it raises today."""
        c = self._client_raising(monkeypatch, 400, {"error": "malformed body"})
        with pytest.raises(httpx.HTTPStatusError):
            c.link("1.1.1", "1.1.2", "cites", "someone")


class TestManifestNullCollectionReport:
    """Substantive critique finding 4 (T2 nexus/chroma-residue-C2-durability-
    critique-2026-08-10): only the 200-with-data path had ANY coverage
    (FakeCatalogHandler's default response) before this class — the
    404/malformed-body 'unavailable' branches every real user hits TODAY
    (no engine tag ships the route yet) had zero test coverage on either
    the client or the health-check side.
    """

    def _client_get_raising(
        self, monkeypatch: pytest.MonkeyPatch, status: int, body: dict,
    ) -> HttpCatalogClient:
        c = object.__new__(HttpCatalogClient)

        def _boom(path, **params):
            request = httpx.Request(
                "GET", "http://svc/v1/catalog/manifest/null_collection",
            )
            response = httpx.Response(status, json=body, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

        monkeypatch.setattr(c, "_get", _boom, raising=False)
        return c

    def _client_get_returning(
        self, monkeypatch: pytest.MonkeyPatch, body: Any,
    ) -> HttpCatalogClient:
        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr(c, "_get", lambda path, **params: body, raising=False)
        return c

    def test_200_with_data_returns_available(self, client: HttpCatalogClient) -> None:
        """The pre-existing, already-covered-by-precedent shape (via
        FakeCatalogHandler's default /manifest/null_collection response) —
        pinned explicitly here so this class documents the full contract in
        one place."""
        report = client.manifest_null_collection_report()
        assert report == {"total": 0, "backfillable": 0, "unavailable": False}

    def test_404_returns_unavailable_not_a_false_clean_zero(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-route engine 404s this route (today's real state — no
        engine tag ships it yet). Must degrade to an HONEST 'cannot
        determine', never a false-clean total=0."""
        c = self._client_get_raising(monkeypatch, 404, {"error": "not found"})
        report = c.manifest_null_collection_report()
        assert report == {"total": 0, "backfillable": 0, "unavailable": True}

    def test_other_http_status_error_also_returns_unavailable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = self._client_get_raising(monkeypatch, 500, {"error": "boom"})
        report = c.manifest_null_collection_report()
        assert report == {"total": 0, "backfillable": 0, "unavailable": True}

    def test_malformed_body_missing_total_returns_unavailable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 200 response stripped of the `total` field must NOT read as a
        clean zero — same fail-honest contract as manifest_orphans' `count`
        field (this method's own docstring)."""
        c = self._client_get_returning(monkeypatch, {"backfillable": 0})
        report = c.manifest_null_collection_report()
        assert report == {"total": 0, "backfillable": 0, "unavailable": True}

    def test_none_body_returns_unavailable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = self._client_get_returning(monkeypatch, None)
        report = c.manifest_null_collection_report()
        assert report == {"total": 0, "backfillable": 0, "unavailable": True}

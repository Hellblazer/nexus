# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xn3fr: recovery bundle core (export/import of the link graph +
store_put-origin knowledge content across reinstall, GH #1419.9).

Design of record: T2 nexus/design-xn3fr-recovery-bundle.md. Runs the REAL
``HttpCatalogClient`` against a stateful fake catalog server (the
``tests/catalog/test_http_catalog_client.py`` pattern) so pagination,
the ``get_manifests`` count contract, and wire-shape parsing are all
exercised for real; only T3 is a duck-typed fake (content fetch/put).

Fixture census mirrors the audited classifier legs (nx_plan_audit rounds
1-2, recorded on nexus-xn3fr):
  chroma-a / chroma-b   chroma:// under knowledge__  -> exported
  residue               chroma:// under code__       -> EXCLUDED (53cae class)
  legacy                empty source_uri, 1-row manifest -> exported (1uekf live-chunk case)
  ghost                 empty source_uri, 0-row manifest -> ghosts_skipped, never silent
  filedoc               file-backed under code__     -> excluded; participates in links
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import nexus.catalog.recovery_bundle as rb
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.note_ttl import row_may_give_tags
from nexus.catalog.recovery_bundle import (
    ExportSummary,
    ImportSummary,
    classify_store_put_origin,
    export_bundle,
    import_bundle,
    link_identity_uri,
    read_bundle,
)
from nexus.catalog.types import CatalogEntry
from nexus.catalog.tumbler import Tumbler
from nexus.db.http_vector_client import VectorServiceError

CH_A = "a" * 64
CH_B = "b" * 64
CH_L = "c" * 64

KNOW = "knowledge__knowledge__bge-base-en-v15-768__v1"
CODE = "code__nexus__bge-base-en-v15-768__v1"


def _doc(tumbler: str, title: str, collection: str, *, file_path: str = "",
         source_uri: str = "", chunk_count: int = 0) -> dict:
    return {
        "tumbler": tumbler,
        "title": title,
        "content_type": "knowledge",
        "chunk_count": chunk_count,
        "file_path": file_path,
        "source_uri": source_uri,
        "physical_collection": collection,
        "metadata": {},
        "source_mtime": 0.0,
        "bib_year": 0, "bib_authors": "", "bib_venue": "", "bib_citation_count": 0,
    }


DOCS = [
    _doc("1.9.1", "title-a", KNOW, source_uri="chroma://knowledge/title-a"),
    _doc("1.9.2", "title-b", KNOW, source_uri="chroma://knowledge/title-b"),
    _doc("1.9.3", "residue", CODE, source_uri="chroma://code/whoops"),
    _doc("1.9.4", "legacy-note", KNOW),                       # empty source_uri, live chunk
    _doc("1.9.5", "ghost-note", KNOW),                        # empty source_uri, no manifest
    _doc("1.9.6", "alpha.py", CODE, file_path="src/alpha.py",
         source_uri="file:///repo/src/alpha.py"),
]

MANIFESTS = {
    "1.9.1": [{"chash": CH_A, "position": 0}],
    "1.9.2": [{"chash": CH_B, "position": 0}],
    "1.9.4": [{"chash": CH_L, "position": 0}],
    # 1.9.5 deliberately ABSENT — get_manifests omits no-manifest docs, and
    # absence must read as ghost, not "unchecked".
}

LINKS = [
    # knowledge doc -> file doc (cross-collection, the common shape)
    {"from_tumbler": "1.9.1", "to_tumbler": "1.9.6", "link_type": "cites",
     "from_span": "", "to_span": "", "created_by": "user", "created_at": "2026-07-01"},
    # legacy (empty-source_uri) doc participates — its endpoint must export
    # the DERIVED uri_for identity, not the empty string (plan-gap fix).
    {"from_tumbler": "1.9.4", "to_tumbler": "1.9.1", "link_type": "relates",
     "from_span": "", "to_span": "", "created_by": "user", "created_at": "2026-07-02"},
]


class _State:
    def __init__(self) -> None:
        self.docs: list[dict] = [dict(d) for d in DOCS]
        self.manifests: dict[str, list[dict]] = {k: list(v) for k, v in MANIFESTS.items()}
        self.links: list[dict] = [dict(l) for l in LINKS]
        self.link_query_requests: list[dict] = []
        self.list_requests = 0
        self.created_links: list[dict] = []
        self.docs_for_chashes_requests: list[list[str]] = []
        self.link_page_size_forced: int | None = None  # test hook

    def manifests_chash_universe(self) -> list[str]:
        return [r["chash"] for rows in self.manifests.values() for r in rows]


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A002
            pass

        def _send(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            op = u.path.removeprefix("/v1/catalog")
            if op == "/list":
                state.list_requests += 1
                docs = state.docs
                if "source_uri" in q:
                    docs = [d for d in docs if d["source_uri"] == q["source_uri"]]
                limit = int(q.get("limit", 0) or 0)
                offset = int(q.get("offset", 0) or 0)
                page = docs[offset: offset + limit] if limit else docs[offset:]
                self._send({"documents": page, "count": len(page)})
                return
            if op == "/link_query":
                state.link_query_requests.append(q)
                limit = int(q.get("limit", 200))
                offset = int(q.get("offset", 0))
                page = state.links[offset: offset + limit]
                self._send({"links": page, "count": len(page)})
                return
            if op == "/resolve":
                self._send({"documents": []})
                return
            self._send({"error": f"unhandled GET {op}"}, 404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            op = urlparse(self.path).path.removeprefix("/v1/catalog")
            if op == "/manifest/get_many":
                out = {
                    did: state.manifests[did]
                    for did in body.get("doc_ids", [])
                    if did in state.manifests
                }
                self._send({"manifests": out, "count": len(out)})
                return
            if op == "/manifest/docs_for_chashes":
                # Real contract: return the DOC IDS holding any requested
                # chash — the client cross-verifies via get_manifests, so
                # fabricated ids map to nothing.
                wanted = set(body.get("chashes", []))
                state.docs_for_chashes_requests.append(sorted(wanted))
                hits = sorted({
                    did for did, rows in state.manifests.items()
                    if any(r["chash"] in wanted for r in rows)
                })
                self._send({"tumblers": hits, "count": len(hits)})
                return
            if op == "/link":
                state.created_links.append(body)
                # duplicate detection: same endpoints+type => merged
                dup = any(
                    l["from_tumbler"] == body["from_tumbler"]
                    and l["to_tumbler"] == body["to_tumbler"]
                    and l["link_type"] == body["link_type"]
                    for l in state.links
                )
                if not dup:
                    state.links.append(dict(body))
                self._send({"created": not dup})
                return
            self._send({"error": f"unhandled POST {op}"}, 404)

    return Handler


@pytest.fixture
def fake_catalog():
    state = _State()
    httpd = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, state
    httpd.shutdown()
    httpd.server_close()


class _FakeT3:
    """Duck-typed T3: content fetch by chash + put recording."""

    def __init__(self) -> None:
        self.rows = {
            CH_A: {"id": CH_A, "content": "note A body", "tags": "t1", "category": ""},
            CH_B: {"id": CH_B, "content": "note B body", "tags": "", "category": "ref"},
            CH_L: {"id": CH_L, "content": "legacy live-chunk body", "tags": "", "category": ""},
        }
        self.puts: list[dict] = []
        self.chunk_rows_requests: list[tuple[str, list[str]]] = []

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        return self.rows.get(doc_id)

    def chunk_rows(self, collection: str, chashes: list[str]) -> dict[str, dict]:
        self.chunk_rows_requests.append((collection, list(chashes)))
        return {c: self.rows[c] for c in chashes if c in self.rows}

    def put(self, **kwargs: Any) -> str:
        self.puts.append(kwargs)
        return "put-" + kwargs.get("title", "")


def _client(url: str) -> HttpCatalogClient:
    return HttpCatalogClient(base_url=url, _token="test_tok")


# ── classifier ──────────────────────────────────────────────────────────────


def _entry(**kw: Any) -> CatalogEntry:
    base = dict(
        tumbler=Tumbler.parse("1.1.1"), title="t", author="", year=0,
        content_type="knowledge", file_path="", corpus="",
        physical_collection=KNOW, chunk_count=0, head_hash="", indexed_at="",
    )
    base.update(kw)
    return CatalogEntry(**base)


def test_classifier_legs():
    assert classify_store_put_origin(_entry(source_uri="chroma://knowledge/x")) == "chroma_uri"
    # RDR-231: a note's stored identity is nx-note:// (the legacy row above is
    # the same note until the engine rewrites it).
    assert classify_store_put_origin(_entry(source_uri="nx-note://knowledge/x")) == "chroma_uri"
    assert classify_store_put_origin(_entry(source_uri="")) == "knowledge_no_path"
    # round-2 bug pin: chroma:// under a file-routed prefix is residue.
    assert classify_store_put_origin(
        _entry(physical_collection=CODE, source_uri="chroma://code/whoops")
    ) is None
    assert classify_store_put_origin(
        _entry(physical_collection=CODE, file_path="src/x.py",
               source_uri="file:///r/src/x.py")
    ) is None
    # non-empty, non-chroma URI without file_path: not store_put-origin.
    assert classify_store_put_origin(
        _entry(source_uri="x-devonthink-item://ABC")
    ) is None


def test_link_identity_uri_derives_for_legacy_docs():
    """Plan-gap fix: a link endpoint at an empty-source_uri knowledge doc
    exports the identity the doc WILL have after import (uri_for), never
    the empty string."""
    legacy = _entry(title="legacy-note", source_uri="")
    derived = link_identity_uri(legacy)
    assert derived.startswith("nx-note://")
    assert "legacy-note" in derived
    normal = _entry(source_uri="chroma://knowledge/x")
    assert link_identity_uri(normal) == "chroma://knowledge/x"


# ── export ──────────────────────────────────────────────────────────────────


def test_enumerate_store_put_origin_documents_excludes_non_knowledge_and_true_ghosts(
    fake_catalog, tmp_path
):
    url, state = fake_catalog
    with _client(url) as reader:
        summary = export_bundle(reader, _FakeT3(), tmp_path / "b.jsonl")
    assert summary.docs_exported == 3          # chroma-a, chroma-b, legacy
    assert summary.ghosts_skipped == 1         # ghost-note, counted not silent
    header, records = read_bundle(tmp_path / "b.jsonl")
    docs = [r for r in records if r["record"] == "knowledge_doc"]
    titles = {d["title"] for d in docs}
    assert titles == {"title-a", "title-b", "legacy-note"}
    assert "residue" not in titles             # round-2 exclusion
    assert "alpha.py" not in titles            # file-backed exclusion
    # content byte-identical to the single stored chunk
    by_title = {d["title"]: d for d in docs}
    assert by_title["title-a"]["content"] == "note A body"
    assert by_title["legacy-note"]["content"] == "legacy live-chunk body"
    # leg-2 record: no invented identity; (collection, title) populated
    assert by_title["legacy-note"]["source_uri"] == ""
    assert by_title["legacy-note"]["collection"] == KNOW


CH_P1 = "d" * 64
CH_P2 = "e" * 64


def _split_note_t3() -> _FakeT3:
    """title-a (1.9.1) written as three pieces (RDR-223). Only the first piece's row is the note's
    own write; the later pieces carry another writer's stamp and keys, which must not reach the
    record."""
    t3 = _FakeT3()
    t3.rows[CH_A] = {"id": CH_A, "content": "first piece, ", "tags": "own-tags", "category": "own-cat",
                     "ttl_days": 9, "catalog_doc_id": "1.9.1", "title": "title-a"}
    for chash, text in ((CH_P1, "second piece, "), (CH_P2, "third piece")):
        t3.rows[chash] = {"id": chash, "content": text, "tags": "other", "category": "other",
                          "ttl_days": 3, "catalog_doc_id": "1.7.7", "title": "someone-else"}
    return t3


def test_a_note_written_as_several_pieces_exports_its_whole_text_in_position_order(fake_catalog, tmp_path):
    """nexus-6pfgb.36: a note put_note split into pieces exports as one record holding every piece,
    joined in manifest position order whatever order the manifest arrives in, with the keyless note's
    keys taken from its own piece's row and never from a piece another note restamped. Every piece of
    the collection is read in one request."""
    url, state = fake_catalog
    state.manifests["1.9.1"] = [
        {"chash": CH_P2, "position": 2}, {"chash": CH_A, "position": 0}, {"chash": CH_P1, "position": 1},
    ]
    t3 = _split_note_t3()
    with _client(url) as reader:
        summary = export_bundle(reader, t3, tmp_path / "b.jsonl")
    _header, records = read_bundle(tmp_path / "b.jsonl")
    doc = {r["title"]: r for r in records if r["record"] == "knowledge_doc"}["title-a"]
    assert doc["content"] == "first piece, second piece, third piece"
    assert (doc["tags"], doc["category"], doc["ttl_days"]) == ("own-tags", "own-cat", 9)
    assert summary.docs_exported == 3
    assert summary.notes_incomplete == 0
    assert len(t3.chunk_rows_requests) == 1, t3.chunk_rows_requests


def test_a_note_missing_a_piece_is_counted_and_never_exported_truncated(fake_catalog, tmp_path):
    """nexus-6pfgb.36: a note one of whose pieces is gone from T3 exports no record (a truncated note
    imported back would overwrite the whole one wherever it still exists) and is counted and named,
    apart from the ghosts, whose every piece is gone."""
    url, state = fake_catalog
    state.manifests["1.9.1"] = [
        {"chash": CH_A, "position": 0}, {"chash": CH_P1, "position": 1}, {"chash": CH_P2, "position": 2},
    ]
    t3 = _split_note_t3()
    del t3.rows[CH_P1]
    with _client(url) as reader:
        summary = export_bundle(reader, t3, tmp_path / "b.jsonl")
    _header, records = read_bundle(tmp_path / "b.jsonl")
    titles = {r["title"] for r in records if r["record"] == "knowledge_doc"}
    assert "title-a" not in titles
    assert titles == {"title-b", "legacy-note"}
    assert summary.notes_incomplete == 1
    assert summary.notes_incomplete_notes == [(KNOW, "title-a")]
    assert summary.ghosts_skipped == 1  # ghost-note only


def test_a_split_note_whose_first_piece_another_note_restamped_takes_its_keys_from_its_own_piece(
    fake_catalog, tmp_path,
):
    """nexus-6pfgb.36: a note sharing its opening piece with another note can find that piece's row
    restamped by the other; the keys come from the first piece that is the note's own write."""
    url, state = fake_catalog
    state.manifests["1.9.1"] = [{"chash": CH_A, "position": 0}, {"chash": CH_P1, "position": 1}]
    t3 = _split_note_t3()
    t3.rows[CH_A] = {**t3.rows[CH_A], "tags": "other", "catalog_doc_id": "1.7.7", "title": "someone-else"}
    t3.rows[CH_P1] = {**t3.rows[CH_P1], "tags": "own-tags", "category": "own-cat", "ttl_days": 9,
                      "catalog_doc_id": "1.9.1", "title": "title-a"}
    with _client(url) as reader:
        summary = export_bundle(reader, t3, tmp_path / "b.jsonl")
    _header, records = read_bundle(tmp_path / "b.jsonl")
    doc = {r["title"]: r for r in records if r["record"] == "knowledge_doc"}["title-a"]
    assert doc["content"] == "first piece, second piece, "
    assert (doc["tags"], doc["category"], doc["ttl_days"]) == ("own-tags", "own-cat", 9)
    assert (KNOW, "title-a") not in summary.note_keys_unrecovered_notes


def test_a_note_with_every_piece_gone_is_a_ghost_not_an_incomplete_note(fake_catalog, tmp_path):
    """nexus-6pfgb.36: a split note none of whose pieces is left, and a one-piece note whose chunk is
    gone, are ghosts; neither is counted incomplete."""
    url, state = fake_catalog
    state.manifests["1.9.1"] = [
        {"chash": CH_A, "position": 0}, {"chash": CH_P1, "position": 1}, {"chash": CH_P2, "position": 2},
    ]
    t3 = _split_note_t3()
    for chash in (CH_A, CH_P1, CH_P2, CH_B):  # CH_B: title-b's only chunk
        del t3.rows[chash]
    with _client(url) as reader:
        summary = export_bundle(reader, t3, tmp_path / "b.jsonl")
    _header, records = read_bundle(tmp_path / "b.jsonl")
    assert {r["title"] for r in records if r["record"] == "knowledge_doc"} == {"legacy-note"}
    assert summary.ghosts_skipped == 3  # ghost-note, title-a, title-b
    assert summary.notes_incomplete == 0
    assert _header["notes_incomplete"] == 0


def test_a_note_whose_manifest_skips_a_position_is_counted_incomplete(fake_catalog, tmp_path):
    """nexus-6pfgb.36: positions 0 and 2 with no 1 mean a manifest row was lost; the pieces that are
    there would join to a shorter text, so the note is left out and named, and the header says so."""
    url, state = fake_catalog
    state.manifests["1.9.1"] = [{"chash": CH_A, "position": 0}, {"chash": CH_P2, "position": 2}]
    with _client(url) as reader:
        summary = export_bundle(reader, _split_note_t3(), tmp_path / "b.jsonl")
    header, records = read_bundle(tmp_path / "b.jsonl")
    assert "title-a" not in {r["title"] for r in records if r["record"] == "knowledge_doc"}
    assert summary.notes_incomplete_notes == [(KNOW, "title-a")]
    assert header["notes_incomplete"] == 1


def test_a_store_read_failure_stops_the_export_and_writes_no_bundle(fake_catalog, tmp_path):
    """nexus-6pfgb.36: a piece the store could not return is not a piece that is gone; the error
    propagates and no file is written."""
    class _Failing(_FakeT3):
        def chunk_rows(self, collection: str, chashes: list[str]) -> dict[str, dict]:
            raise VectorServiceError("503 from store-get")

    url, _state = fake_catalog
    with _client(url) as reader, pytest.raises(VectorServiceError):
        export_bundle(reader, _Failing(), tmp_path / "b.jsonl")
    assert not (tmp_path / "b.jsonl").exists()


def _unregistered(collection: str) -> Exception:
    body = {"error": f"collection {collection} is not registered", "collection": collection,
            "reason": "unregistered_collection"}
    return VectorServiceError("422", code=422, reason="unregistered_collection", engine_body=body)


def test_notes_of_a_collection_the_engine_no_longer_registers_are_ghosts(fake_catalog, tmp_path):
    """nexus-6pfgb.36: the engine's typed 422 for a collection with no catalog row means that
    collection holds no chunks; its notes count as ghosts and the rest of the export goes on, as it
    did when each piece was read alone."""
    class _Unregistered(_FakeT3):
        def chunk_rows(self, collection: str, chashes: list[str]) -> dict[str, dict]:
            raise _unregistered(collection)

    url, _state = fake_catalog
    with _client(url) as reader:
        summary = export_bundle(reader, _Unregistered(), tmp_path / "b.jsonl")
    assert summary.docs_exported == 0
    assert summary.ghosts_skipped == 4  # ghost-note (no manifest), title-a, title-b, legacy-note
    assert summary.notes_incomplete == 0
    assert summary.links_exported == 2


def test_any_other_422_from_the_store_still_stops_the_export(fake_catalog, tmp_path):
    """A 422 with another reason is a store that could not answer, not an empty collection."""
    class _Other(_FakeT3):
        def chunk_rows(self, collection: str, chashes: list[str]) -> dict[str, dict]:
            raise VectorServiceError("422", code=422, reason="model_unavailable",
                                     engine_body={"error": "model unavailable", "collection": collection,
                                                  "reason": "model_unavailable"})

    url, _state = fake_catalog
    with _client(url) as reader, pytest.raises(VectorServiceError):
        export_bundle(reader, _Other(), tmp_path / "b.jsonl")
    assert not (tmp_path / "b.jsonl").exists()


def test_export_links_drives_pagination_to_exhaustion(fake_catalog, tmp_path, monkeypatch):
    url, state = fake_catalog
    # 12 links across page size 5 -> 3 /link_query requests minimum.
    state.links = [
        {"from_tumbler": "1.9.1", "to_tumbler": "1.9.6", "link_type": f"t{i}",
         "from_span": "", "to_span": "", "created_by": "u", "created_at": ""}
        for i in range(12)
    ]
    import nexus.catalog.recovery_bundle as rb
    monkeypatch.setattr(rb, "_LINK_PAGE", 5)
    with _client(url) as reader:
        summary = export_bundle(reader, _FakeT3(), tmp_path / "b.jsonl")
    assert summary.links_exported == 12
    # non-vacuity: the loop actually looped.
    assert len(state.link_query_requests) >= 3


def test_bundle_is_plain_jsonl(fake_catalog, tmp_path):
    url, _ = fake_catalog
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), tmp_path / "b.jsonl")
    lines = (tmp_path / "b.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(l) for l in lines]      # every line is plain JSON
    assert parsed[0]["format"] == "nexus-recovery-bundle"
    assert parsed[0]["format_version"] == 1
    # docs precede links (import resolves links against just-imported docs)
    kinds = [p.get("record") for p in parsed[1:]]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "knowledge_doc" else 1)
    # link records are denormalized to source_uri endpoints, never tumblers
    links = [p for p in parsed if p.get("record") == "link"]
    assert links and all("from_source_uri" in l and "tumbler" not in json.dumps(l) for l in links)
    # the legacy doc's link endpoint carries the DERIVED identity
    derived = [l for l in links if l["link_type"] == "relates"]
    assert derived and derived[0]["from_source_uri"].startswith("nx-note://")


# ── import ──────────────────────────────────────────────────────────────────


def _stub_import_doc(recorder: list, state: "_State | None" = None):
    """Records titles AND models the real chain's sdp0u mint: the store_put
    chain registers the doc with the synthesized uri_for identity, which is
    exactly what makes derived-identity link endpoints resolvable after
    import. A stub that skips the mint diverges from reality (measured:
    the first run of these tests did, and the derived link failed)."""
    from nexus.aspect_readers import uri_for

    def _f(t3: Any, rec: dict) -> None:
        recorder.append(rec["title"])
        if state is not None:
            uri = rec["source_uri"] or uri_for(rec["collection"], rec["title"])
            for d in state.docs:
                if d["title"] == rec["title"] and d["physical_collection"] == rec["collection"]:
                    d["source_uri"] = uri
                    break
            else:
                state.docs.append(_doc(f"1.9.9{len(state.docs)}", rec["title"],
                                       rec["collection"], source_uri=uri))
    return _f


def test_import_bundle_reports_unresolvable_link_endpoint_without_raising(
    fake_catalog, tmp_path
):
    url, state = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)
    # Vanish one link endpoint from the target catalog: the doc 1.9.6
    # (file-backed) disappears, so the 'cites' link's to_source_uri no
    # longer resolves.
    state.docs = [d for d in state.docs if d["tumbler"] != "1.9.6"]
    imported: list = []
    with _client(url) as client:
        summary = import_bundle(
            client, client, _FakeT3(), bundle,
            import_doc=_stub_import_doc(imported, state),
        )
    assert summary.docs_imported == 3
    assert len(summary.unresolvable_links) == 1
    assert summary.unresolvable_links[0]["link_type"] == "cites"
    assert summary.unresolvable_links[0]["missing"] == "to"
    # the resolvable link still imported
    assert summary.links_created + summary.links_merged == 1


def test_import_is_idempotent_on_links(fake_catalog, tmp_path):
    url, state = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)
    imported: list = []
    with _client(url) as client:
        s1 = import_bundle(client, client, _FakeT3(), bundle,
                           import_doc=_stub_import_doc(imported, state))
        s2 = import_bundle(client, client, _FakeT3(), bundle,
                           import_doc=_stub_import_doc(imported, state))
    # second pass: every link merges (server-side duplicate contract),
    # nothing new is created.
    assert s2.links_created == 0
    assert s2.links_merged == s1.links_created + s1.links_merged


def test_read_bundle_refuses_future_format(tmp_path):
    p = tmp_path / "future.jsonl"
    p.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 99}) + "\n")
    with pytest.raises(ValueError, match="format_version 99"):
        read_bundle(p)


def test_import_doc_failure_lands_in_summary_not_raise(fake_catalog, tmp_path):
    url, _ = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)

    def _boom(t3: Any, rec: dict) -> None:
        if rec["title"] == "title-b":
            raise RuntimeError("simulated put failure")

    with _client(url) as client:
        summary = import_bundle(client, client, _FakeT3(), bundle, import_doc=_boom)
    assert summary.docs_imported == 2
    assert summary.docs_failed == 1
    assert summary.doc_failures[0]["title"] == "title-b"
    assert "simulated put failure" in summary.doc_failures[0]["error"]
    assert isinstance(summary, ImportSummary)


# ── the import's call sequence into the note writer (seam-pinned) ───────────
#
# RDR-223 P2.8 (nexus-z0o2p.18): a note is written by note_write.put_note, one
# owner-carrying request. The real-engine behavior (one request, no ownerless
# chunk, re-import, per-note failure) is tests/test_z0o2p18_recovery_import.py;
# THIS pins what the importer hands put_note and how it words each outcome.


def _outcome(status: str, **kw: Any):
    from nexus.catalog.note_write import PutNoteOutcome

    base = dict(
        status=status, collection=KNOW, pieces=["body"],
        manifest_metadatas=[{"chunk_text_hash": "h" * 64, "chunk_index": 0}],
        chunk_ids=["h" * 64], catalog_doc_id="1.7.7", minted=True,
    )
    base.update(kw)
    return PutNoteOutcome(**base)


class _Hooks:
    """Records the post-store chains; stands in for the registry the importer builds."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def fire_single(self, doc_id, collection, content, **kw):
        self.events.append(("single", doc_id, collection, content))

    def fire_batch(self, doc_ids, collection, contents, embeddings=None, metadatas=None, **kw):
        self.events.append(("batch", tuple(doc_ids), collection, tuple(contents), kw))

    def fire_document(self, source_path, collection, content, **kw):
        self.events.append(("document", source_path, collection, content, kw.get("doc_id")))

    def fire_store_chains(self, *a, **kw):
        raise AssertionError("the importer must not ride fire_store_chains: it would re-write the manifest")


@pytest.fixture
def note_writer(monkeypatch):
    """Patch the resolver, put_note and the hook registry; returns (put_note calls, hooks, result)."""
    calls: list[dict] = []
    hooks = _Hooks()
    result: dict[str, Any] = {"outcome": _outcome("stored")}

    def _put_note(**kw):
        calls.append(kw)
        return result["outcome"]

    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name",
        lambda name, t3=None, for_write=False, allow_placeholder=False: KNOW,
    )
    monkeypatch.setattr("nexus.catalog.note_write.put_note", _put_note)
    monkeypatch.setattr("nexus.hook_registry.HookRegistry", lambda: hooks)
    monkeypatch.setattr("nexus.hook_registry.install_default_hooks", lambda h: None)
    return calls, hooks, result


_REC = {
    "record": "knowledge_doc", "source_uri": "", "collection": KNOW,
    "title": "seq-note", "tags": "t", "category": "ref", "content": "body",
}


def test_default_import_doc_hands_the_note_to_put_note_and_fires_the_chains(note_writer):
    import nexus.catalog.recovery_bundle as rb
    from nexus.mcp_infra import manifest_write_batch_hook

    calls, hooks, _ = note_writer
    t3 = _FakeT3()
    rb._default_import_doc(t3, dict(_REC))

    assert len(calls) == 1
    call = calls[0]
    assert (call["content"], call["collection"], call["title"]) == ("body", KNOW, "seq-note")
    assert (call["tags"], call["category"]) == ("t", "ref")
    assert call["source_agent"] == "recovery-import"
    assert t3.puts == [], "the importer wrote a chunk itself instead of through put_note"
    # the post-store chains: per piece, the batch without the manifest hook (the write request
    # already wrote and stamped it), and the aspect chain once with the catalog document id
    assert [e[0] for e in hooks.events] == ["single", "batch", "document"]
    batch = hooks.events[1]
    assert batch[4]["catalog_doc_id"] == "1.7.7"
    assert batch[4]["skip_hooks"] == {manifest_write_batch_hook}
    assert hooks.events[2][-1] == "1.7.7" and hooks.events[2][3] == "body"


def test_a_split_note_fires_every_piece_and_the_document_chain_once(note_writer):
    import nexus.catalog.recovery_bundle as rb

    _calls, hooks, result = note_writer
    result["outcome"] = _outcome(
        "stored", pieces=["ab", "cd"], chunk_ids=["1" * 64, "2" * 64],
        manifest_metadatas=[{"chunk_text_hash": "1" * 64}, {"chunk_text_hash": "2" * 64}])
    rb._default_import_doc(_FakeT3(), {**_REC, "content": "abcd"})
    assert [e[0] for e in hooks.events] == ["single", "single", "batch", "document"]
    assert hooks.events[-1][3] == "abcd", "aspect extraction must read the whole note"


@pytest.mark.parametrize("status, reason, expect", [
    ("not-landed", "engine refused", "could not store 'seq-note'"),
    ("no-catalog", "catalog registration failed: X", "could not catalog 'seq-note'"),
])
def test_a_note_that_did_not_land_raises_a_plain_failure_and_fires_no_hook(note_writer, status, reason, expect):
    import nexus.catalog.recovery_bundle as rb

    _calls, hooks, result = note_writer
    result["outcome"] = _outcome(status, reason=reason)
    with pytest.raises(RuntimeError, match=expect) as err:
        rb._default_import_doc(_FakeT3(), dict(_REC))
    assert not isinstance(err.value, rb.ImportDocUncertain)
    assert reason in str(err.value)
    assert hooks.events == []


def test_an_uncertain_note_raises_uncertain_and_fires_no_hook(note_writer):
    import nexus.catalog.recovery_bundle as rb

    _calls, hooks, result = note_writer
    result["outcome"] = _outcome("uncertain", reason="timed out in flight")
    with pytest.raises(rb.ImportDocUncertain, match="Nothing was rolled back") as err:
        rb._default_import_doc(_FakeT3(), dict(_REC))
    assert err.value.stamp_refused is False
    assert hooks.events == []


def test_a_refused_stamp_raises_uncertain_with_the_stamp_flag(note_writer):
    import nexus.catalog.recovery_bundle as rb

    _calls, hooks, result = note_writer
    result["outcome"] = _outcome(
        "uncertain", reason="stamp refused", stamp_refused=True, stamp_detail="row count mismatch")
    with pytest.raises(rb.ImportDocUncertain, match="row count mismatch") as err:
        rb._default_import_doc(_FakeT3(), dict(_REC))
    assert err.value.stamp_refused is True
    assert "'indexing'" in str(err.value)
    assert hooks.events == []


def test_a_put_note_exception_propagates_as_a_failure(note_writer, monkeypatch):
    import nexus.catalog.recovery_bundle as rb

    def _boom(**kw):
        raise RuntimeError("put exploded")

    monkeypatch.setattr("nexus.catalog.note_write.put_note", _boom)
    with pytest.raises(RuntimeError, match="put exploded"):
        rb._default_import_doc(_FakeT3(), dict(_REC))


def test_import_bundle_counts_each_outcome_and_finishes_the_rest(fake_catalog, tmp_path):
    """Three notes, three unverified outcomes (failed, uncertain, stamp refused): each lands in
    exactly one count, each is named, and a failure does not stop the notes after it."""
    import nexus.catalog.recovery_bundle as rb

    url, _ = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)

    seen: list[str] = []

    def _mixed(t3: Any, rec: dict) -> None:
        seen.append(rec["title"])
        if rec["title"] == "title-a":
            raise RuntimeError("definitive refusal")
        if rec["title"] == "title-b":
            raise rb.ImportDocUncertain("may have landed")
        if rec["title"] == "legacy-note":
            raise rb.ImportDocUncertain("not stamped", stamp_refused=True)

    with _client(url) as client:
        summary = import_bundle(client, client, _FakeT3(), bundle, import_doc=_mixed)

    assert sorted(seen) == ["legacy-note", "title-a", "title-b"], "a failure stopped the rest"
    assert summary.docs_imported == 0
    assert (summary.docs_failed, summary.docs_uncertain, summary.docs_stamp_refused) == (1, 1, 1)
    assert summary.docs_unverified == 3
    assert [f["title"] for f in summary.doc_failures] == ["title-a"]
    assert sorted((u["title"], u["stamp_refused"]) for u in summary.doc_unverified) == [
        ("legacy-note", True), ("title-b", False)]


def test_target_collection_reaches_put_note_under_a_changed_embedding_mode(note_writer, monkeypatch):
    """Critique ship-blocker: the recorded (source-install) collection name embeds the SOURCE's
    embedding model; import must reduce it to the mode-independent type__owner base and resolve THAT
    under the target, else a mode-changed reinstall raises IncompatibleCollectionError or fragments
    the corpus. put_note must be handed the TARGET's collection.

    Neutral model token on purpose (RDR-109 mode lint): the resolver below returns this literal
    directly, so it is opaque simulation data for the mode change, never a real embedder call.
    """
    import nexus.catalog.recovery_bundle as rb

    calls, _hooks, _ = note_writer
    resolved: list[str] = []
    target = "knowledge__knowledge__model-ctx__v1"

    def _resolver(name, t3=None, for_write=False, allow_placeholder=False):
        resolved.append(name)
        return target

    monkeypatch.setattr("nexus.corpus.t3_collection_name", _resolver)
    rb._default_import_doc(_FakeT3(), {**_REC, "title": "mode-note"})

    assert resolved == ["knowledge__knowledge"]
    assert calls[0]["collection"] == target


def test_target_collection_for_passes_nonconformant_names_through(monkeypatch):
    import nexus.catalog.recovery_bundle as rb

    seen: list[str] = []
    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name",
        lambda name, t3=None, for_write=False, allow_placeholder=False: seen.append(name) or name,
    )
    rb.target_collection_for("scratchpad", t3=None)
    assert seen == ["scratchpad"]  # non-conformant: untouched base


def test_link_endpoint_fallback_rederives_chroma_identity(fake_catalog, tmp_path, monkeypatch):
    """Critique trace: a link exported as chroma://SOURCE-col/title must
    resolve on a mode-changed target whose imported doc carries the
    TARGET-col identity.

    Neutral model token on purpose (RDR-109 mode lint): same reason as
    the sibling test above -- opaque simulation data, no embedder call.
    """
    import nexus.catalog.recovery_bundle as rb

    url, state = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)

    target_col = "knowledge__knowledge__model-ctx__v1"
    monkeypatch.setattr(rb, "target_collection_for", lambda col, t3: target_col)
    from nexus.aspect_readers import uri_for

    def _mode_changed_import(t3, rec):
        # The REAL chain on a mode-changed target registers under the
        # target collection, so sdp0u mints uri_for(TARGET, title) — the
        # recorded source-install identity never exists there.
        uri = uri_for(target_col, rec["title"])
        for d in state.docs:
            if d["title"] == rec["title"]:
                d["source_uri"] = uri
                d["physical_collection"] = target_col
                break

    with _client(url) as client:
        summary = import_bundle(client, client, _FakeT3(), bundle,
                                import_doc=_mode_changed_import)
    # Every chroma-endpoint link resolved via the re-derivation fallback;
    # the file-backed endpoint resolves directly. Nothing unresolvable.
    assert summary.unresolvable_links == []
    assert summary.links_created + summary.links_merged == 2


def test_vanished_span_is_stripped_and_counted(fake_catalog, tmp_path):
    """Design's locked span contract (was declared-but-dead — review-fold):
    a chash: span whose chunk is gone imports WITHOUT the span, counted."""
    url, state = fake_catalog
    gone = "9" * 64
    state.links = [
        {"from_tumbler": "1.9.1", "to_tumbler": "1.9.6", "link_type": "cites",
         "from_span": f"chash:{gone}", "to_span": f"chash:{CH_A}",
         "created_by": "u", "created_at": ""},
    ]
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)
    imported: list = []
    with _client(url) as client:
        summary = import_bundle(client, client, _FakeT3(), bundle,
                                import_doc=_stub_import_doc(imported, state))
    assert summary.links_missing_span == 1          # the vanished one, counted
    sent = state.created_links[-1]
    assert sent["from_span"] == ""                   # stripped
    assert sent["to_span"] == f"chash:{CH_A}"        # resolvable: verbatim



def test_target_collection_for_restores_a_placeholder_named_collection() -> None:
    """Critique of 86cd65ef0 (nexus-0fw11): the import reduces a recorded
    conformant name to its two-segment base before resolving for write, and
    docs__default / knowledge__knowledge are live production collections;
    the placeholder refusal must not block their restore."""
    from nexus.catalog.recovery_bundle import target_collection_for

    # The model segment is re-derived for the TARGET install (bge locally,
    # the target's active model); the placeholder subject survives either way.
    out = target_collection_for("knowledge__knowledge__bge-base-en-v15-768__v1", t3=None)
    assert out.startswith("knowledge__knowledge__") and out.endswith("__v1"), out
    out = target_collection_for("docs__default__bge-base-en-v15-768__v1", t3=None)
    assert out.startswith("docs__default__") and out.endswith("__v1"), out


# ── a note's own tags, category and ttl_days (nexus-6pfgb.14) ───────────────
#
# Two notes with identical text share one T3 chunk row, and the row carries the last writer's keys.
# The bundle takes each note's own from its catalog entry's meta (nexus-6pfgb.5), not from the row.

CH_S = "d" * 64


def _shared_pair(state: "_State", t3: "_FakeT3", *, a_meta: dict, b_meta: dict) -> None:
    """Two notes, one chunk: the row holds B's keys (B wrote last)."""
    state.docs = [
        {**_doc("1.9.1", "note-a", KNOW, source_uri="chroma://knowledge/note-a"), "metadata": a_meta},
        {**_doc("1.9.2", "note-b", KNOW, source_uri="chroma://knowledge/note-b"), "metadata": b_meta},
    ]
    state.manifests = {
        "1.9.1": [{"chash": CH_S, "position": 0}],
        "1.9.2": [{"chash": CH_S, "position": 0}],
    }
    state.links = []
    t3.rows = {CH_S: {"id": CH_S, "content": "shared text", "tags": "beta", "category": "finding",
                      "ttl_days": 30}}


def _export_docs(url: str, t3: Any, tmp_path) -> tuple["ExportSummary", dict[str, dict]]:
    with _client(url) as reader:
        summary = export_bundle(reader, t3, tmp_path / "b.jsonl")
    _h, records = read_bundle(tmp_path / "b.jsonl")
    return summary, {r["title"]: r for r in records if r["record"] == "knowledge_doc"}


def test_two_notes_sharing_a_chunk_each_export_their_own_tags_category_and_ttl(fake_catalog, tmp_path):
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(
        state, t3,
        a_meta={"tags": "alpha,one", "category": "design", "ttl_days": 7, "source_agent": "agent-a"},
        b_meta={"tags": "beta", "category": "finding", "ttl_days": 30, "source_agent": "agent-b"},
    )
    summary, docs = _export_docs(url, t3, tmp_path)

    assert summary.docs_exported == 2
    a, b = docs["note-a"], docs["note-b"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("alpha,one", "design", 7)
    assert (b["tags"], b["category"], b["ttl_days"]) == ("beta", "finding", 30)
    assert summary.note_keys_unrecovered == 0


def test_a_retracted_or_absent_ttl_exports_as_permanent(fake_catalog, tmp_path):
    """Catalog meta stores ttl_days 0 as the retraction sentinel; the row's 30 (B's) is not A's."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(
        state, t3,
        a_meta={"tags": "", "category": "design", "ttl_days": 0, "source_agent": "agent-a"},
        b_meta={"tags": "beta", "category": "finding", "source_agent": "agent-b"},
    )
    _summary, docs = _export_docs(url, t3, tmp_path)

    assert docs["note-a"]["ttl_days"] is None
    assert docs["note-b"]["ttl_days"] is None


@pytest.mark.parametrize(
    "own_meta",
    [{"source_agent": "x"}, {"tags": ""}, {"ttl_days": 0}, {"put_at": "2026-10-26T00:00:00+00:00"}],
)
@pytest.mark.parametrize("row_owner", [None, "1.9.1", "1.9.2"], ids=["row-unowned", "row-ours", "row-theirs"])
def test_an_entry_with_any_note_key_is_authoritative_over_a_single_owner_row(
    fake_catalog, tmp_path, own_meta, row_owner,
):
    """A is the chunk's only live owner and holds one note key, even an empty or retracted one: its
    other keys are unset, never refilled from a row that says 'beta', whoever wrote it. This is the
    test that fails if an empty key reads as absent, or authority is keyed on one key only.

    ``put_at`` alone is the shape every put since nexus-6pfgb.15 leaves on a note stored with every
    other key unset: such a note's empties are its own, so it is not counted as unrecovered."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta=own_meta, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}
    if row_owner:
        t3.rows[CH_S]["catalog_doc_id"] = row_owner
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("", "", None)
    assert summary.note_keys_unrecovered == 0


def test_a_legacy_note_on_a_shared_chunk_with_an_empty_row_is_not_counted(fake_catalog, tmp_path):
    """The count is of notes that lost a value: a shared legacy note whose row holds none loses none."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    t3.rows[CH_S].update({"tags": "", "category": "", "ttl_days": None})
    summary, docs = _export_docs(url, t3, tmp_path)

    assert (docs["note-a"]["tags"], docs["note-a"]["ttl_days"]) == ("", None)
    assert summary.note_keys_unrecovered == 0


def test_a_legacy_note_on_a_shared_chunk_is_not_given_the_last_writers_values(fake_catalog, tmp_path):
    """No note key in the entry's meta, and another live note holds the chunk: the row's keys may be
    the other note's, so the record carries none and the summary counts the note."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={"tags": "beta", "category": "finding", "ttl_days": 30})
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("", "", None)
    assert docs["note-b"]["tags"] == "beta"
    assert summary.note_keys_unrecovered == 1


def test_a_legacy_note_alone_on_an_unstamped_row_takes_its_tags_and_category_but_not_its_ttl(
    fake_catalog, tmp_path,
):
    """A is the chunk's only live owner and the row names no writer (Sam, nexus-6pfgb.35 option B):
    A takes the row's tags and category, since a wrong tag is cosmetic, but not its ttl_days, since a
    TTL a departed co-owner left on the row would make a permanent note expire after import. Losing
    the TTL counts the note."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}      # B's manifest is gone
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("beta", "finding", None)
    assert "note-b" not in docs, "B has no manifest left: a ghost, not an owner"
    assert summary.note_keys_unrecovered_notes == [(KNOW, "note-a")]


def test_a_legacy_note_alone_on_an_unstamped_row_with_no_ttl_is_fully_recovered(fake_catalog, tmp_path):
    """The census shape (nexus-6pfgb.35: all 8 such notes on the live tenant): sole owner, no writer
    stamp, tags and category only. Nothing is dropped, so nothing is counted."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}
    t3.rows[CH_S]["ttl_days"] = None
    summary, docs = _export_docs(url, t3, tmp_path)

    assert (docs["note-a"]["tags"], docs["note-a"]["category"]) == ("beta", "finding")
    assert summary.note_keys_unrecovered == 0


def test_a_note_with_no_keys_of_its_own_never_takes_a_departed_co_owners_keys(fake_catalog, tmp_path):
    """A written before nexus-6pfgb.5, or with every key unset before nexus-6pfgb.15 added ``put_at``
    (so its entry holds no note key), is now the chunk's only owner,
    but the row still holds B's tags, category and ttl_days, and names B as its last writer: A exports
    none of them, and is counted. A permanent note must not come back expiring."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}      # B departed
    t3.rows[CH_S].update({"catalog_doc_id": "1.9.2", "title": "note-b"})  # the row's last writer
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("", "", None)
    assert summary.note_keys_unrecovered == 1


def test_a_note_with_no_keys_that_is_the_rows_last_writer_takes_the_rows_values(fake_catalog, tmp_path):
    """The row's stamp and title are A's: its values are A's, shared chunk or not. B, which has none of
    its own either, is not the writer and gets none."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    t3.rows[CH_S].update({"catalog_doc_id": "1.9.1", "title": "note-a"})
    summary, docs = _export_docs(url, t3, tmp_path)

    a, b = docs["note-a"], docs["note-b"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("beta", "finding", 30)
    assert (b["tags"], b["category"], b["ttl_days"]) == ("", "", None)
    assert summary.note_keys_unrecovered == 1


@pytest.mark.parametrize(
    "row_writer",
    [{"catalog_doc_id": "1.9.1"}, {"catalog_doc_id": "1.9.1", "title": "note-b"},
     {"catalog_doc_id": "1.4.4", "title": "note-a"}],
    ids=["stamp-without-title", "renamed-note-own-stamp-old-title", "nxexp-import-other-boxs-stamp-own-title"],
)
def test_a_row_is_a_keyless_notes_own_only_when_both_its_stamp_and_its_title_are(
    fake_catalog, tmp_path, row_writer,
):
    """An ``.nxexp`` import keeps the SOURCE box's ``catalog_doc_id`` on the rows it writes while the
    destination mints its own tumblers, so a stale stamp can equal A's tumbler: the export reads a row
    as A's own write, and takes its ``ttl_days``, only through ``note_ttl.row_written_by`` (stamp AND
    title), never on one of them alone. A sole owner still takes such a row's tags and category, since
    neither its stamp nor its title is another note's for sure (Sam, nexus-6pfgb.35 option B, middle
    reading); the dropped TTL counts the note."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}
    t3.rows[CH_S].update(row_writer)
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert (a["tags"], a["category"], a["ttl_days"]) == ("beta", "finding", None)
    assert summary.note_keys_unrecovered == 1


@pytest.mark.parametrize(
    ("row", "allowed"),
    [
        ({}, True),
        ({"title": "note-b"}, True),
        ({"catalog_doc_id": "1.9.1", "title": "note-b"}, True),
        ({"catalog_doc_id": "1.4.4", "title": "note-a"}, True),
        ({"catalog_doc_id": "1.9.2"}, True),
        ({"catalog_doc_id": "1.9.2", "title": "note-b"}, False),
        ("not a dict", False),
    ],
    ids=["unstamped-untitled", "unstamped-other-title", "own-stamp-other-title", "other-stamp-own-title",
         "other-stamp-untitled", "other-stamp-other-title", "not-a-row"],
)
def test_row_may_give_tags_refuses_only_a_row_another_note_wrote(row, allowed):
    """nexus-6pfgb.35 option B, the middle reading: the one importable rule (note_ttl) the export uses
    and the nexus-6pfgb.10 backfill is specified to reuse (RDR-236)."""
    assert row_may_give_tags("1.9.1", row, owner_title="note-a") is allowed


def test_a_legacy_row_with_ttl_zero_exports_as_permanent(fake_catalog, tmp_path):
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}
    t3.rows[CH_S].update({"ttl_days": 0, "catalog_doc_id": "1.9.1", "title": "note-a"})
    _summary, docs = _export_docs(url, t3, tmp_path)
    assert docs["note-a"]["tags"] == "beta", "the row is A's own write"
    assert docs["note-a"]["ttl_days"] is None


def test_the_export_asks_who_owns_a_chunk_only_for_a_keyless_note_on_an_unstamped_row(fake_catalog, tmp_path):
    """Sole ownership decides tags and category only where nothing better exists: a keyed note, or a
    keyless note whose row is stamped, makes no owner lookup at all."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={"tags": "alpha"}, b_meta={"tags": "beta"})
    _export_docs(url, t3, tmp_path)
    assert state.docs_for_chashes_requests == []

    t3.rows[CH_S].update({"catalog_doc_id": "1.9.2", "title": "note-b"})
    state.docs[0]["metadata"] = {}
    _export_docs(url, t3, tmp_path)
    assert state.docs_for_chashes_requests == []

    del t3.rows[CH_S]["catalog_doc_id"]
    _export_docs(url, t3, tmp_path)
    assert state.docs_for_chashes_requests == [[CH_S]]


def test_a_split_keyless_note_with_an_own_written_piece_makes_no_owner_lookup(fake_catalog, tmp_path):
    """A piece that is the note's own write answers for all three keys, so no other piece's ownership
    is asked about, even an unstamped one."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    CH_T = "f" * 64
    state.manifests["1.9.1"] = [{"chash": CH_S, "position": 0}, {"chash": CH_T, "position": 1}]
    t3.rows[CH_S].update({"catalog_doc_id": "1.9.1", "title": "note-a"})
    t3.rows[CH_T] = {"id": CH_T, "content": " and more", "tags": "other", "category": "", "ttl_days": None}
    _summary, docs = _export_docs(url, t3, tmp_path)

    assert docs["note-a"]["tags"] == "beta"
    assert state.docs_for_chashes_requests == []


def test_a_failed_owner_lookup_withholds_tags_and_never_the_backup(fake_catalog, tmp_path, monkeypatch):
    """The lookup only adds cosmetic keys: when it fails the export goes on, and the notes whose tags it
    withheld are counted and named."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.manifests = {"1.9.1": [{"chash": CH_S, "position": 0}]}
    t3.rows[CH_S]["ttl_days"] = None
    monkeypatch.setattr(HttpCatalogClient, "docs_for_chashes",
                        lambda self, chashes: (_ for _ in ()).throw(RuntimeError("catalog 503")))
    summary, docs = _export_docs(url, t3, tmp_path)

    assert (docs["note-a"]["tags"], docs["note-a"]["category"]) == ("", "")
    assert summary.note_keys_unrecovered_notes == [(KNOW, "note-a")]


def test_an_owner_in_another_collection_does_not_count_against_sole_ownership(fake_catalog, tmp_path):
    """The owner lookup is catalog-wide; the same text in another collection is a different T3 row, so
    an owner the catalog walk places there is not a co-owner of this one."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={})
    state.docs[1]["physical_collection"] = CODE  # note-b holds the same chash, in another collection
    t3.rows[CH_S]["ttl_days"] = None
    summary, docs = _export_docs(url, t3, tmp_path)

    assert (docs["note-a"]["tags"], docs["note-a"]["category"]) == ("beta", "finding")
    assert (KNOW, "note-a") not in summary.note_keys_unrecovered_notes
    assert state.docs_for_chashes_requests == [[CH_S]]


def test_a_split_legacy_note_takes_tags_from_its_first_unstamped_sole_owned_piece(fake_catalog, tmp_path):
    """Pieces are judged in position order: a first piece another note still shares gives nothing, the
    next piece that A alone holds and no writer stamped gives tags and category."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(state, t3, a_meta={}, b_meta={"tags": "beta"})
    CH_T = "f" * 64
    state.manifests["1.9.1"] = [{"chash": CH_S, "position": 0}, {"chash": CH_T, "position": 1}]
    t3.rows[CH_S].update({"ttl_days": None})
    t3.rows[CH_T] = {"id": CH_T, "content": " and more", "tags": "own", "category": "mine", "ttl_days": None}
    summary, docs = _export_docs(url, t3, tmp_path)

    a = docs["note-a"]
    assert a["content"] == "shared text and more"
    assert (a["tags"], a["category"], a["ttl_days"]) == ("own", "mine", None)
    assert (KNOW, "note-a") not in summary.note_keys_unrecovered_notes


def test_each_notes_own_values_round_trip_through_the_import(fake_catalog, tmp_path, note_writer):
    """Export two notes that share a chunk, import the bundle: each note is written with its own keys."""
    url, state = fake_catalog
    t3 = _FakeT3()
    _shared_pair(
        state, t3,
        a_meta={"tags": "alpha,one", "category": "design", "ttl_days": 7},
        b_meta={"tags": "beta", "category": "finding", "ttl_days": 0},
    )
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, t3, bundle)
    calls, _hooks, _result = note_writer
    with _client(url) as client:
        summary = import_bundle(client, client, t3, bundle, import_doc=rb._default_import_doc)

    assert summary.docs_failed == 0 and summary.docs_imported == 2, summary.doc_failures
    by_title = {c["title"]: c for c in calls}
    assert (by_title["note-a"]["tags"], by_title["note-a"]["category"], by_title["note-a"]["ttl_days"]) == (
        "alpha,one", "design", 7)
    assert (by_title["note-b"]["tags"], by_title["note-b"]["category"], by_title["note-b"]["ttl_days"]) == (
        "beta", "finding", None)


@pytest.mark.parametrize("recorded", [0, -3, None, "", "7", 1.5, True])
def test_an_unusable_ttl_in_a_bundle_imports_as_permanent_and_never_raises(note_writer, recorded):
    calls, _hooks, _result = note_writer
    rec = dict(_REC)
    if recorded is not None:
        rec["ttl_days"] = recorded
    rb._default_import_doc(_FakeT3(), rec)
    assert calls[0]["ttl_days"] is None


def test_a_bundle_without_the_ttl_key_imports_as_permanent(note_writer):
    calls, _hooks, _result = note_writer
    rb._default_import_doc(_FakeT3(), dict(_REC))
    assert calls[0]["ttl_days"] is None


def test_a_positive_ttl_reaches_put_note(note_writer):
    calls, _hooks, _result = note_writer
    rb._default_import_doc(_FakeT3(), {**_REC, "ttl_days": 14})
    assert calls[0]["ttl_days"] == 14

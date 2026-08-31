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

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.recovery_bundle import (
    ImportSummary,
    SingleChunkInvariantError,
    classify_store_put_origin,
    export_bundle,
    import_bundle,
    link_identity_uri,
    read_bundle,
)
from nexus.catalog.types import CatalogEntry
from nexus.catalog.tumbler import Tumbler

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

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        return self.rows.get(doc_id)

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
    assert derived.startswith("chroma://")
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


def test_single_chunk_tripwire_raises_loudly(fake_catalog, tmp_path):
    url, state = fake_catalog
    state.manifests["1.9.1"] = [
        {"chash": CH_A, "position": 0}, {"chash": CH_B, "position": 1},
    ]
    with _client(url) as reader, pytest.raises(SingleChunkInvariantError):
        export_bundle(reader, _FakeT3(), tmp_path / "b.jsonl")


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
    assert derived and derived[0]["from_source_uri"].startswith("chroma://")


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


# ── the real import chain's call sequence (seam-pinned) ─────────────────────


def test_default_import_doc_drives_the_real_store_put_chain(monkeypatch, tmp_path):
    """_default_import_doc must mirror commands/store.py::put_cmd's chain
    EXACTLY: hook -> fence begin -> t3.put(catalog_doc_id=...) -> manifest
    direct. The chain's real behavior against a live engine is
    test_store_put_cli_parity.py's territory; THIS pins that the importer
    calls the same sequence with the same threading (a silently dropped
    catalog_doc_id or skipped manifest write would recreate the b6enc
    ghost class through the recovery path)."""
    import nexus.catalog.recovery_bundle as rb

    calls: list[tuple] = []

    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name",
        lambda name, t3=None, for_write=False: KNOW,
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.single_chunk_manifest_metadata",
        lambda content: ("chunk-id-1", [{"chunk_text_hash": "h" * 64, "chunk_index": 0}]),
    )

    class _Hooks:
        def fire_store_chains(self, ids, col, contents, **kw):
            calls.append(("chains", ids[0], col, kw.get("catalog_doc_id")))

    monkeypatch.setattr("nexus.hook_registry.HookRegistry", lambda: _Hooks())
    monkeypatch.setattr("nexus.hook_registry.install_default_hooks", lambda h: None)
    monkeypatch.setattr(
        "nexus.catalog.store_hook.catalog_store_hook_tracked",
        lambda title, doc_id, collection_name: (
            calls.append(("hook", title, doc_id, collection_name)) or ("1.7.7", True)
        ),
    )
    monkeypatch.setattr(
        "nexus.doc_indexer._fence_begin",
        lambda doc_id, content_hash, col: calls.append(("fence", doc_id, content_hash)),
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.store_put_manifest_direct",
        lambda doc_id, metadatas, collection: calls.append(("manifest", doc_id, collection)),
    )

    t3 = _FakeT3()
    rec = {
        "record": "knowledge_doc", "source_uri": "", "collection": KNOW,
        "title": "seq-note", "tags": "t", "category": "", "content": "body",
    }
    rb._default_import_doc(t3, rec)

    assert [c[0] for c in calls] == ["hook", "fence", "manifest", "chains"]
    assert calls[0][1:] == ("seq-note", "chunk-id-1", KNOW)
    assert calls[1][1:] == ("1.7.7", "h" * 64)
    assert calls[2][1:] == ("1.7.7", KNOW)
    # review-fold blocker pin: the post-store hook chains (chash index,
    # taxonomy, aspect enqueue) fire with the put's doc_id + catalog id.
    assert calls[3][1] == "put-seq-note"
    assert calls[3][2] == KNOW
    assert calls[3][3] == "1.7.7"
    assert len(t3.puts) == 1
    put = t3.puts[0]
    assert put["catalog_doc_id"] == "1.7.7"
    assert put["content"] == "body"
    assert put["title"] == "seq-note"


def test_default_import_doc_put_failure_fences_and_rolls_back(monkeypatch):
    """The b6enc compensation: a t3.put failure must fence-fail AND roll
    back a row minted in this call, then re-raise."""
    import nexus.catalog.recovery_bundle as rb

    events: list[str] = []
    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name", lambda name, t3=None, for_write=False: name
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.single_chunk_manifest_metadata",
        lambda content: ("cid", [{"chunk_text_hash": "h" * 64, "chunk_index": 0}]),
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.catalog_store_hook_tracked",
        lambda **kw: ("1.7.8", True),
    )
    monkeypatch.setattr(
        "nexus.doc_indexer._fence_begin", lambda *a, **k: events.append("begin")
    )
    monkeypatch.setattr(
        "nexus.doc_indexer._fence_fail", lambda *a, **k: events.append("fail")
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.rollback_minted_catalog_entry",
        lambda doc_id, original_error="": events.append(f"rollback:{doc_id}"),
    )

    class _BoomT3:
        def put(self, **kw: Any) -> str:
            raise RuntimeError("put exploded")

    rec = {"record": "knowledge_doc", "source_uri": "", "collection": KNOW,
           "title": "x", "tags": "", "category": "", "content": "b"}
    with pytest.raises(RuntimeError, match="put exploded"):
        rb._default_import_doc(_BoomT3(), rec)
    assert events == ["begin", "fail", "rollback:1.7.8"]


# ── review-fold blockers (2026-08-31 stacked review) ───────────────────────


def test_import_rederives_collection_under_changed_embedding_mode(monkeypatch):
    """Critique ship-blocker: the recorded (source-install) collection name
    embeds the SOURCE's embedding model; import must reduce it to the
    mode-independent type__owner base and resolve THAT under the target —
    else a mode-changed reinstall raises IncompatibleCollectionError or
    fragments the corpus. The resolver here models a bge->voyage target."""
    import nexus.catalog.recovery_bundle as rb

    resolved: list[str] = []
    target = "knowledge__knowledge__voyage-context-3__v1"

    def _resolver(name, t3=None, for_write=False):
        resolved.append(name)
        return target

    monkeypatch.setattr("nexus.corpus.t3_collection_name", _resolver)
    monkeypatch.setattr(
        "nexus.catalog.store_hook.single_chunk_manifest_metadata",
        lambda content: ("cid", [{"chunk_text_hash": "h" * 64, "chunk_index": 0}]),
    )
    monkeypatch.setattr(
        "nexus.catalog.store_hook.catalog_store_hook_tracked",
        lambda title, doc_id, collection_name: ("1.7.9", False),
    )
    monkeypatch.setattr("nexus.doc_indexer._fence_begin", lambda *a, **k: None)
    monkeypatch.setattr(
        "nexus.catalog.store_hook.store_put_manifest_direct", lambda *a, **k: None
    )

    class _Hooks:
        def fire_store_chains(self, *a, **k):
            pass

    monkeypatch.setattr("nexus.hook_registry.HookRegistry", lambda: _Hooks())
    monkeypatch.setattr("nexus.hook_registry.install_default_hooks", lambda h: None)

    t3 = _FakeT3()
    rec = {"record": "knowledge_doc", "source_uri": "", "collection": KNOW,
           "title": "mode-note", "tags": "", "category": "", "content": "b"}
    rb._default_import_doc(t3, rec)

    # The resolver saw the mode-independent BASE, never the recorded
    # model-bearing name; the put landed in the TARGET's collection.
    assert resolved == ["knowledge__knowledge"]
    assert t3.puts[0]["collection"] == target


def test_target_collection_for_passes_nonconformant_names_through(monkeypatch):
    import nexus.catalog.recovery_bundle as rb

    seen: list[str] = []
    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name",
        lambda name, t3=None, for_write=False: seen.append(name) or name,
    )
    rb.target_collection_for("scratchpad", t3=None)
    assert seen == ["scratchpad"]  # non-conformant: untouched base


def test_link_endpoint_fallback_rederives_chroma_identity(fake_catalog, tmp_path, monkeypatch):
    """Critique trace: a link exported as chroma://SOURCE-col/title must
    resolve on a mode-changed target whose imported doc carries the
    TARGET-col identity."""
    import nexus.catalog.recovery_bundle as rb

    url, state = fake_catalog
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)

    target_col = "knowledge__knowledge__voyage-context-3__v1"
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

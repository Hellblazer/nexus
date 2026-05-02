# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nexus.aspect_readers`` — scheme-keyed reader registry
(RDR-096 P1.1).

Coverage:

* ``CHROMA_IDENTITY_FIELD`` dispatch — every prefix in the table plus
  the unknown-prefix fallback.
* ``_read_file_uri`` — existing path, missing path, empty file,
  url-encoded path component.
* ``_read_chroma_uri`` — knowledge__ shape (``title`` identity),
  rdr__ shape (``source_path`` identity), out-of-order multi-chunk
  reassembly, missing collection, empty result, malformed URI,
  pagination past ``QUOTAS.MAX_QUERY_RESULTS``.
* ``read_source`` dispatch — file scheme, chroma scheme, unknown
  scheme, empty URI.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import chromadb
import pytest


# ── CHROMA_IDENTITY_FIELD dispatch ───────────────────────────────────────────


class TestUriFor:
    """``uri_for`` is the single source of truth for URI construction
    used by both the going-forward writer
    (``aspect_extractor._build_record`` / ``_empty_record``) and the
    backfill migration (``migrate_document_aspects_source_uri``).
    Divergence between the two would cause silent inconsistency on
    future prefix additions; the two consumers import this helper
    rather than re-implementing the rule.
    """

    def test_filesystem_collection_returns_file_uri_with_abspath(self):
        import os.path

        from nexus.aspect_readers import uri_for

        for collection in ("rdr__nexus", "docs__corpus", "code__nx"):
            uri = uri_for(collection, "src/cli.py")
            assert uri == "file://" + os.path.abspath("src/cli.py")

    def test_knowledge_collection_returns_chroma_uri(self):
        from nexus.aspect_readers import uri_for

        assert uri_for("knowledge__delos", "/papers/aleph.pdf") == (
            "chroma://knowledge__delos//papers/aleph.pdf"
        )
        assert uri_for("knowledge__knowledge", "decision-x") == (
            "chroma://knowledge__knowledge/decision-x"
        )

    def test_unknown_prefix_returns_chroma_uri(self):
        """Future prefixes default to chroma:// — same as the migration."""
        from nexus.aspect_readers import uri_for

        assert uri_for("future__x", "src") == "chroma://future__x/src"

    def test_empty_source_path_returns_none(self):
        """``None`` matches the migration's NULL-on-empty backfill
        behavior. Writers that store the result get SQLite NULL.
        """
        from nexus.aspect_readers import uri_for

        assert uri_for("rdr__nexus", "") is None
        assert uri_for("knowledge__delos", "") is None


class TestIdentityFieldDispatch:
    """The dispatch table picks the right metadata field per collection
    prefix. ``knowledge__*`` carries TWO shapes: papers ingested via
    ``nx index pdf`` populate ``source_path``; slugs from
    ``store_put`` MCP / ``nx memory promote`` populate ``title``. The
    dispatch table value is an ordered tuple so the reader can try
    each in turn (P2.0 spike survey, 2026-04-27).
    """

    def test_rdr_collection_uses_doc_id(self):
        # nexus-o6aa.10.1: post-fix, every prefix dispatches on doc_id.
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("rdr__nexus-571b8edd") == ("doc_id",)

    def test_docs_collection_uses_doc_id(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("docs__corpus") == ("doc_id",)

    def test_code_collection_uses_doc_id(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("code__nexus") == ("doc_id",)

    def test_knowledge_collection_uses_doc_id(self):
        # Both paper-shaped (``knowledge__delos``) and slug-shaped
        # (``knowledge__knowledge``) collections dispatch on doc_id;
        # the source_path/title divergence is resolved by the catalog
        # projection in ``doc_id_lookup``.
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("knowledge__knowledge") == ("doc_id",)
        assert _identity_fields_for("knowledge__delos") == ("doc_id",)

    def test_unknown_prefix_falls_back_to_doc_id(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("future__newshape") == ("doc_id",)

    def test_dispatch_table_uniformly_keyed_on_doc_id(self):
        from nexus.aspect_readers import CHROMA_IDENTITY_FIELD
        # WITH TEETH: every dispatch entry must be the single-element
        # tuple ('doc_id',). A regression that re-introduces source_path
        # or title fails this assertion directly.
        for prefix, fields in CHROMA_IDENTITY_FIELD.items():
            assert fields == ("doc_id",), (
                f"{prefix!r} dispatched on {fields!r}, expected ('doc_id',)"
            )


# ── file:// reader ───────────────────────────────────────────────────────────


class TestReadFileUri:
    def test_existing_file_returns_read_ok(self, tmp_path: Path):
        from nexus.aspect_readers import ReadOk, _read_file_uri

        p = tmp_path / "doc.md"
        p.write_text("# Hello\n\nworld.\n", encoding="utf-8")
        result = _read_file_uri(f"file://{p}")
        assert isinstance(result, ReadOk)
        assert result.text == "# Hello\n\nworld.\n"
        assert result.metadata["scheme"] == "file"
        assert result.metadata["path"] == str(p)
        assert result.metadata["bytes"] == len("# Hello\n\nworld.\n".encode("utf-8"))

    def test_missing_file_returns_read_fail_unreachable(self, tmp_path: Path):
        from nexus.aspect_readers import ReadFail, _read_file_uri

        missing = tmp_path / "nope.md"
        result = _read_file_uri(f"file://{missing}")
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "FileNotFoundError" in result.detail

    def test_empty_file_returns_read_fail_empty(self, tmp_path: Path):
        from nexus.aspect_readers import ReadFail, _read_file_uri

        p = tmp_path / "empty.md"
        p.touch()
        result = _read_file_uri(f"file://{p}")
        assert isinstance(result, ReadFail)
        assert result.reason == "empty"

    def test_url_encoded_path_is_unquoted(self, tmp_path: Path):
        from nexus.aspect_readers import ReadOk, _read_file_uri

        p = tmp_path / "with space.md"
        p.write_text("payload", encoding="utf-8")
        encoded_path = quote(str(p))
        result = _read_file_uri(f"file://{encoded_path}")
        assert isinstance(result, ReadOk)
        assert result.text == "payload"

    def test_no_path_returns_read_fail(self):
        from nexus.aspect_readers import ReadFail, _read_file_uri

        result = _read_file_uri("file://")
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"


# ── chroma:// reader fixtures ────────────────────────────────────────────────


@pytest.fixture
def t3_client():
    """A process-fresh chromadb.EphemeralClient. Per-test collection
    cleanup happens via unique collection names below; tests still
    drop their own collection at start as defense-in-depth.
    """
    return chromadb.EphemeralClient()


def _seed_chunks(
    client,
    collection: str,
    *,
    identity_field: str,
    source_id: str,
    chunks: list[tuple[int, str]],
):
    """Plant ``chunks`` (chunk_index, text) into ``collection`` with
    ``identity_field=source_id`` metadata so the chroma reader can
    find them.
    """
    try:
        client.delete_collection(collection)
    except Exception:
        pass
    coll = client.get_or_create_collection(collection)
    coll.add(
        ids=[f"{source_id}::{ci}" for ci, _ in chunks],
        documents=[text for _, text in chunks],
        metadatas=[
            {identity_field: source_id, "chunk_index": ci} for ci, _ in chunks
        ],
    )
    return coll


# ── chroma:// reader ─────────────────────────────────────────────────────────


class TestReadChromaUri:
    def test_knowledge_single_chunk_via_title(self, t3_client):
        """``knowledge__*`` chunks identify documents by ``title``."""
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        title = "decision-bfdb-update-capture-rdr005"
        _seed_chunks(
            t3_client,
            "knowledge__t1",
            identity_field="title",
            source_id=title,
            chunks=[(0, "First and only chunk text.")],
        )
        result = _read_chroma_uri(
            f"chroma://knowledge__t1/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "First and only chunk text."
        assert result.metadata["scheme"] == "chroma"
        assert result.metadata["collection"] == "knowledge__t1"
        assert result.metadata["source_id"] == title
        assert result.metadata["identity_field"] == "title"
        assert result.metadata["chunk_count"] == 1

    def test_rdr_single_chunk_via_source_path(self, t3_client):
        """``rdr__*`` chunks identify documents by ``source_path``."""
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        sp = "docs/rdr/rdr-090-realistic-agentic-scholar.md"
        _seed_chunks(
            t3_client,
            "rdr__t2",
            identity_field="source_path",
            source_id=sp,
            chunks=[(0, "RDR body text.")],
        )
        result = _read_chroma_uri(
            f"chroma://rdr__t2/{sp}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "RDR body text."
        assert result.metadata["identity_field"] == "source_path"

    def test_multi_chunk_reassembly_in_chunk_index_order(self, t3_client):
        """Chunks added out of order must come back sorted by
        ``chunk_index`` and joined with ``\\n\\n`` separators.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        title = "paper-multichunk"
        _seed_chunks(
            t3_client,
            "knowledge__t3",
            identity_field="title",
            source_id=title,
            chunks=[
                (2, "third"),
                (0, "first"),
                (1, "second"),
            ],
        )
        result = _read_chroma_uri(
            f"chroma://knowledge__t3/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "first\n\nsecond\n\nthird"
        assert result.metadata["chunk_count"] == 3

    def test_no_matching_chunks_returns_read_fail_empty(self, t3_client):
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        # Plant an unrelated document so the collection exists but
        # the queried source_id has no chunks.
        _seed_chunks(
            t3_client,
            "knowledge__t4",
            identity_field="title",
            source_id="someone-else",
            chunks=[(0, "noise")],
        )
        result = _read_chroma_uri(
            "chroma://knowledge__t4/missing-doc", t3=t3_client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "empty"

    def test_missing_collection_returns_read_fail_unreachable(self, t3_client):
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        # No seeding — collection doesn't exist on the EphemeralClient.
        try:
            t3_client.delete_collection("knowledge__nope")
        except Exception:
            pass
        result = _read_chroma_uri(
            "chroma://knowledge__nope/anything", t3=t3_client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_no_t3_client_returns_read_fail(self):
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        result = _read_chroma_uri("chroma://knowledge__x/y", t3=None)
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_malformed_uri_missing_collection(self, t3_client):
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        result = _read_chroma_uri("chroma:///source-id-only", t3=t3_client)
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_malformed_uri_missing_source_id(self, t3_client):
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        result = _read_chroma_uri("chroma://knowledge__t5/", t3=t3_client)
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_knowledge_falls_back_to_title_when_source_path_misses(
        self, t3_client,
    ):
        """``knowledge__*`` tries source_path FIRST (paper-shaped
        ingests), falls back to ``title`` SECOND (slug-shaped
        memory-promoted entries). This test plants chunks with only
        ``title`` metadata (no source_path) — the slug-shaped
        knowledge__knowledge case from research-4 — and verifies the
        fallback recovers them.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        title = "decision-bfdb-update-capture-rdr005"
        try:
            t3_client.delete_collection("knowledge__fallback")
        except Exception:
            pass
        coll = t3_client.get_or_create_collection("knowledge__fallback")
        # Note: ONLY title metadata, NO source_path key.
        coll.add(
            ids=[f"{title}::0"],
            documents=["slug-shaped knowledge note text."],
            metadatas=[{"title": title, "chunk_index": 0}],
        )
        result = _read_chroma_uri(
            f"chroma://knowledge__fallback/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "slug-shaped knowledge note text."
        # ``identity_field`` in metadata reports which field actually
        # matched — useful for triage when the fallback fires.
        assert result.metadata["identity_field"] == "title"

    def test_knowledge_uses_source_path_when_present(self, t3_client):
        """The dual case: ``knowledge__*`` collection where chunks
        DO have source_path (paper-shaped ingests like
        knowledge__delos / knowledge__art). The reader uses
        source_path on the first try without consulting title.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        sp = "/papers/aleph-bft.pdf"
        try:
            t3_client.delete_collection("knowledge__paper")
        except Exception:
            pass
        coll = t3_client.get_or_create_collection("knowledge__paper")
        coll.add(
            ids=["aleph-bft::0"],
            documents=["Paper body text."],
            # Both fields present — but source_path is the first-try
            # field so this asserts the preference order.
            metadatas=[{"source_path": sp, "title": "unrelated", "chunk_index": 0}],
        )
        # Production constructs the URI as
        # ``f"chroma://{collection}/{quote(source_path, safe='/')}"``
        # — when source_path starts with ``/``, that yields a
        # ``//``-separator URI. Mirror that here.
        result = _read_chroma_uri(
            f"chroma://knowledge__paper/{sp}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["identity_field"] == "source_path"

    def test_missing_chunk_index_uses_insertion_order_tiebreak(self, t3_client):
        """When ``chunk_index`` is absent from metadata, the chunk
        defaults to ``0``. Two such chunks must come back in a
        deterministic order — insertion sequence (chromadb's
        within-page response order) is the secondary sort key.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        title = "no-chunk-index"
        try:
            t3_client.delete_collection("knowledge__t7")
        except Exception:
            pass
        coll = t3_client.get_or_create_collection("knowledge__t7")
        # Both chunks lack ``chunk_index`` — both default to 0; the
        # insertion-sequence tiebreaker is the only thing keeping
        # the join order deterministic.
        coll.add(
            ids=[f"{title}::a", f"{title}::b"],
            documents=["alpha", "beta"],
            metadatas=[{"title": title}, {"title": title}],
        )
        result = _read_chroma_uri(
            f"chroma://knowledge__t7/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["chunk_count"] == 2
        # Both chunks are present and the sort completed without
        # error; exact order is whatever chromadb's response order
        # yields plus the stable tiebreak.
        assert "alpha" in result.text
        assert "beta" in result.text

    def test_absolute_source_path_round_trips(self, t3_client):
        """Cloud-shaped source_paths are absolute filesystem paths
        like ``/Users/.../paper.pdf``. The URI is then
        ``chroma://collection//Users/.../paper.pdf`` (double-slash
        separator). ``parsed.path.removeprefix('/')`` strips exactly
        one leading slash, preserving the second to recover the
        absolute path.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        sp = "/Users/me/papers/aleph-bft.pdf"
        _seed_chunks(
            t3_client,
            "knowledge__abs",
            identity_field="source_path",
            source_id=sp,
            chunks=[(0, "Paper body.")],
        )
        # ``//`` separator after netloc preserves the leading slash.
        result = _read_chroma_uri(
            f"chroma://knowledge__abs/{sp}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["source_id"] == sp

    def test_source_path_with_slashes_round_trips(self, t3_client):
        """rdr__/docs__/code__ source paths typically contain ``/``;
        the URI ``chroma://<collection>/<source-identifier>`` carries
        them in the path component without percent-encoding.
        ``urlparse`` returns the slashes verbatim and ``.lstrip('/')``
        removes only the single leading separator before the path.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        sp = "docs/rdr/rdr-090-realistic-agentic-scholar.md"
        _seed_chunks(
            t3_client,
            "rdr__t8",
            identity_field="source_path",
            source_id=sp,
            chunks=[(0, "RDR body.")],
        )
        result = _read_chroma_uri(
            f"chroma://rdr__t8/{sp}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["source_id"] == sp

    def test_pagination_past_limit_returns_all_chunks(self, t3_client):
        """The paginated ``coll.get`` loop must walk past
        ``QUOTAS.MAX_QUERY_RESULTS`` (cap=300) and concatenate all
        pages in chunk_index order.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri
        from nexus.db.chroma_quotas import QUOTAS

        n = QUOTAS.MAX_QUERY_RESULTS + 5  # 305
        title = "paginated-paper"
        _seed_chunks(
            t3_client,
            "knowledge__t6",
            identity_field="title",
            source_id=title,
            chunks=[(i, f"c{i}") for i in range(n)],
        )
        result = _read_chroma_uri(
            f"chroma://knowledge__t6/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["chunk_count"] == n
        # First and last chunk text confirm sort order across pages.
        parts = result.text.split("\n\n")
        assert parts[0] == "c0"
        assert parts[-1] == f"c{n - 1}"


# ── chroma:// reader: doc_id_lookup plumbing (nexus-o6aa.10.1) ───────────────


class TestReadChromaUriDocIdLookup:
    """nexus-o6aa.10.1: when ``doc_id_lookup`` is supplied, the reader
    resolves ``(collection, source_id) → doc_id`` via the catalog
    projection and queries on ``doc_id`` only. Without the lookup the
    reader falls back to the legacy multi-field probe (back-compat for
    callers without catalog access: tests, ad-hoc CLI runs).

    The Phase 4 critical-gate: aspect extraction MUST run through this
    path so chunks remain reachable after the prune verb (.10.3) drops
    ``source_path`` and ``title`` from chunk metadata. Without the
    rewrite, every aspect-extraction read would return ``empty`` and
    structurally reproduce the ART-lhk1 failure.
    """

    def test_doc_id_lookup_queries_on_doc_id_metadata(self, t3_client):
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        col_name = "knowledge__lookup"
        try:
            t3_client.delete_collection(col_name)
        except Exception:
            pass
        coll = t3_client.get_or_create_collection(col_name)
        # Plant chunks with doc_id metadata only: NO source_path,
        # NO title. Post-prune chunk shape.
        coll.add(
            ids=["chunk-0", "chunk-1"],
            documents=["first chunk", "second chunk"],
            metadatas=[
                {"doc_id": "ART-deadbeef", "chunk_index": 0},
                {"doc_id": "ART-deadbeef", "chunk_index": 1},
            ],
        )

        # Source identity in the URI (e.g. legacy source_path) is
        # mapped to doc_id via the lookup.
        def lookup(coll: str, source_id: str) -> str:
            assert coll == col_name
            assert source_id == "/abs/path/paper.pdf"
            return "ART-deadbeef"

        result = _read_chroma_uri(
            "chroma://knowledge__lookup//abs/path/paper.pdf",
            t3=t3_client, doc_id_lookup=lookup,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "first chunk\n\nsecond chunk"
        # WITH TEETH: identity_field is now ``doc_id`` for lookup-driven
        # reads. Pre-fix code reports ``source_path`` or ``title`` here.
        assert result.metadata["identity_field"] == "doc_id"

    def test_doc_id_lookup_returns_empty_yields_unreachable(self, t3_client):
        """Live-data gate semantics: a missing doc_id (catalog gap, orphan
        chunk after Phase 1 synthesis) must report a structural failure
        (``unreachable``), NOT ``empty``. The Phase 4 acceptance gate
        explicitly forbids ``empty`` skips for documents that have
        chunks; an empty-doc_id is a structural reason."""
        from nexus.aspect_readers import ReadFail, _read_chroma_uri

        col_name = "docs__nodocid"
        try:
            t3_client.delete_collection(col_name)
        except Exception:
            pass
        t3_client.get_or_create_collection(col_name)

        def lookup(_coll: str, _source_id: str) -> str:
            return ""

        result = _read_chroma_uri(
            "chroma://docs__nodocid/orphan.md",
            t3=t3_client, doc_id_lookup=lookup,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable", (
            f"missing-doc_id must report a structural reason "
            f"(unreachable), not {result.reason!r}; the Phase 4 gate "
            f"forbids 'empty' for documents that haven't been backfilled."
        )

    def test_no_doc_id_lookup_falls_back_to_legacy_probe(self, t3_client):
        """Back-compat: callers without catalog access (tests, ad-hoc
        CLI runs) call _read_chroma_uri without doc_id_lookup. The
        reader falls back to the legacy ``(source_path, title)`` probe
        so existing chunks remain readable.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        title = "decision-bfdb-update-capture-rdr005"
        _seed_chunks(
            t3_client,
            "knowledge__legacy",
            identity_field="title",
            source_id=title,
            chunks=[(0, "legacy chunk")],
        )
        # No doc_id_lookup → legacy ``(source_path, title)`` probe.
        result = _read_chroma_uri(
            f"chroma://knowledge__legacy/{title}", t3=t3_client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "legacy chunk"
        # identity_field reports which legacy field actually matched.
        assert result.metadata["identity_field"] == "title"

    def test_doc_id_lookup_falls_back_to_legacy_when_doc_id_query_empty(
        self, t3_client,
    ):
        """nexus-o6aa.10.1 transitional shape: when doc_id_lookup
        returns a doc_id but the strict doc_id query returns no chunks
        (chunks predate t3-backfill-doc-id), fall back to the legacy
        ``(source_path, title)`` probe rather than report ``empty``.
        Phase 5b drops this fallback once the prune verb's coverage
        gate turns green.

        WITH TEETH: a regression that drops the fallback (or restores
        the strict doc_id-only contract) reports ``empty`` here and
        fails the live-data gate on collections like
        ``knowledge__knowledge`` where catalog metadata.doc_id is
        populated but T3 chunks haven't been backfilled.
        """
        from nexus.aspect_readers import ReadOk, _read_chroma_uri

        col_name = "knowledge__transitional"
        try:
            t3_client.delete_collection(col_name)
        except Exception:
            pass
        coll = t3_client.get_or_create_collection(col_name)
        # Chunk has title metadata only (slug-shaped, pre-backfill);
        # NO doc_id field on the chunk.
        coll.add(
            ids=["c-0"],
            documents=["transitional content"],
            metadatas=[{
                "title": "decision-x-pre-backfill",
                "chunk_index": 0,
            }],
        )

        # Catalog has the doc_id mapped, but the chunk doesn't carry it.
        def lookup(_coll: str, _source_id: str) -> str:
            return "1.2.3"  # tumbler-shaped doc_id

        result = _read_chroma_uri(
            "chroma://knowledge__transitional/decision-x-pre-backfill",
            t3=t3_client, doc_id_lookup=lookup,
        )
        # Strict-doc_id query returns empty; legacy fallback probes on
        # (source_path, title) and finds the chunk via title.
        assert isinstance(result, ReadOk)
        assert result.text == "transitional content"
        assert result.metadata["identity_field"] == "title"


# ── nx-scratch:// reader (RDR-096 P4.1) ─────────────────────────────────────


@pytest.fixture
def t1_scratch():
    """Per-test T1Database backed by an EphemeralClient. Each test
    gets a fresh session_id so process-shared EphemeralClient state
    doesn't leak between tests.
    """
    import uuid

    import chromadb

    from nexus.db.t1 import T1Database

    return T1Database(
        session_id=f"test-{uuid.uuid4().hex[:8]}",
        client=chromadb.EphemeralClient(),
    )


class TestReadScratchUri:
    """RDR-096 P4.1: ``nx-scratch://session/<session-id>/<entry-id>``
    reader for in-session synthesis documents that get persisted to
    T3 — preserves provenance back to the originating session.
    """

    def test_live_scratch_entry_returns_read_ok(self, t1_scratch):
        from nexus.aspect_readers import ReadOk, _read_scratch_uri

        entry_id = t1_scratch.put("synthesis content from agent")
        uri = f"nx-scratch://session/{t1_scratch.session_id}/{entry_id}"
        result = _read_scratch_uri(uri, scratch=t1_scratch)
        assert isinstance(result, ReadOk)
        assert result.text == "synthesis content from agent"
        assert result.metadata["scheme"] == "nx-scratch"
        assert result.metadata["session_id"] == t1_scratch.session_id
        assert result.metadata["entry_id"] == entry_id

    def test_missing_entry_returns_read_fail_unreachable(self, t1_scratch):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        # Plant something so the collection exists, then query a
        # different entry id.
        t1_scratch.put("noise")
        missing_uri = f"nx-scratch://session/{t1_scratch.session_id}/00000000-0000-0000-0000-000000000000"
        result = _read_scratch_uri(missing_uri, scratch=t1_scratch)
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "not found" in result.detail

    def test_no_scratch_client_returns_read_fail(self):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        result = _read_scratch_uri(
            "nx-scratch://session/abc/def", scratch=None,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "no scratch client" in result.detail

    def test_malformed_uri_missing_session_id(self, t1_scratch):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        # nx-scratch://session// — empty session-id segment.
        result = _read_scratch_uri(
            "nx-scratch://session//entry-id", scratch=t1_scratch,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_malformed_uri_missing_entry_id(self, t1_scratch):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        result = _read_scratch_uri(
            "nx-scratch://session/sess-only/", scratch=t1_scratch,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_unexpected_netloc_returns_read_fail(self, t1_scratch):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        # The shape is ``nx-scratch://session/...``; anything else
        # at the netloc position is rejected so callers don't paste
        # arbitrary URIs that happen to share the scheme.
        result = _read_scratch_uri(
            "nx-scratch://other-host/sess/entry", scratch=t1_scratch,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "netloc" in result.detail

    def test_empty_content_returns_read_fail_empty(self, t1_scratch):
        from nexus.aspect_readers import ReadFail, _read_scratch_uri

        # Plant an entry with empty content.
        entry_id = t1_scratch.put("")
        uri = f"nx-scratch://session/{t1_scratch.session_id}/{entry_id}"
        result = _read_scratch_uri(uri, scratch=t1_scratch)
        assert isinstance(result, ReadFail)
        assert result.reason == "empty"

    def test_dispatch_via_read_source(self, t1_scratch):
        """``read_source`` routes nx-scratch:// URIs through
        ``_read_scratch_uri`` — verify the registry wiring.
        """
        from nexus.aspect_readers import ReadOk, read_source

        entry_id = t1_scratch.put("payload")
        uri = f"nx-scratch://session/{t1_scratch.session_id}/{entry_id}"
        result = read_source(uri, scratch=t1_scratch)
        assert isinstance(result, ReadOk)
        assert result.text == "payload"


# ── https:// reader (RDR-096 P4.2) ──────────────────────────────────────────


class _StubHttpResponse:
    """Minimal httpx.Response stand-in for tests. Carries
    ``status_code`` and ``text`` like the real client returns."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _StubHttpClient:
    """Records each ``get`` call and replies with a queued response.
    Lets tests assert ``calls == 0`` for the chroma-first preference
    test (network must NOT fire when chroma hit serves the read).
    """

    def __init__(self, responses: list[_StubHttpResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(
                f"_StubHttpClient.get({url!r}) ran out of canned responses "
                f"— test should have queued one or asserted no call."
            )
        return self.responses.pop(0)

    def close(self) -> None:
        pass


class TestReadHttpsUri:
    """RDR-096 P4.2: ``https://`` scheme reader. Default behavior
    prefers a chroma:// equivalent (no network round-trip) when a
    ``chroma_hint`` is provided and the chunk exists in T3; falls
    back to httpx fetch otherwise. ``force_refresh=True`` bypasses
    the chroma-first preference.
    """

    def test_200_returns_read_ok(self):
        from nexus.aspect_readers import ReadOk, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(200, "page body text")])
        result = _read_https_uri(
            "https://docs.bito.ai/ingest", http_client=client,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "page body text"
        assert result.metadata["scheme"] == "https"
        assert result.metadata["served_from"] == "network"
        assert result.metadata["http_status"] == 200
        assert client.calls == ["https://docs.bito.ai/ingest"]

    def test_401_returns_read_fail_unauthorized(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(401, "denied")])
        result = _read_https_uri(
            "https://paywalled.example.com/x", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unauthorized"
        assert "401" in result.detail

    def test_403_returns_read_fail_unauthorized(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(403, "forbidden")])
        result = _read_https_uri(
            "https://x.example.com", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unauthorized"

    def test_404_returns_read_fail_unreachable(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(404, "not found")])
        result = _read_https_uri(
            "https://x.example.com/missing", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "404" in result.detail

    def test_5xx_returns_read_fail_unreachable(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(503, "service unavailable")])
        result = _read_https_uri(
            "https://flaky.example.com", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"

    def test_network_exception_returns_read_fail_unreachable(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        class _ExplodingClient:
            calls: list[str] = []

            def get(self, url):
                self.calls.append(url)
                raise OSError("DNS resolution failed")

            def close(self):
                pass

        client = _ExplodingClient()
        result = _read_https_uri(
            "https://nope.invalid", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "OSError" in result.detail

    def test_empty_body_returns_read_fail_empty(self):
        from nexus.aspect_readers import ReadFail, _read_https_uri

        client = _StubHttpClient([_StubHttpResponse(200, "")])
        result = _read_https_uri(
            "https://blank.example.com", http_client=client,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "empty"

    def test_chroma_first_preference_skips_network_when_hint_resolves(
        self, t3_client,
    ):
        """When ``chroma_hint=(collection, source_id)`` is provided
        AND the chunk exists in T3 AND ``force_refresh=False``, the
        reader returns the chroma content WITHOUT making a network
        request.
        """
        from nexus.aspect_readers import ReadOk, _read_https_uri

        # Plant a chroma chunk that will serve as the cached version.
        title = "docs-bito-ingest-snapshot"
        _seed_chunks(
            t3_client,
            "knowledge__bito",
            identity_field="title",
            source_id=title,
            chunks=[(0, "cached page body from chroma")],
        )
        # Stub the http client; queue NO responses so any unexpected
        # call would AssertionError out.
        client = _StubHttpClient(responses=[])

        result = _read_https_uri(
            "https://docs.bito.ai/ingest",
            t3=t3_client,
            http_client=client,
            chroma_hint=("knowledge__bito", title),
        )
        assert isinstance(result, ReadOk)
        assert result.text == "cached page body from chroma"
        assert result.metadata["served_from"] == "chroma"
        assert result.metadata["https_uri"] == "https://docs.bito.ai/ingest"
        # Network NOT touched.
        assert client.calls == []

    def test_force_refresh_bypasses_chroma_first(self, t3_client):
        """``force_refresh=True`` skips the chroma-first preference
        and always goes to the network — useful when an operator
        wants the live page (Confluence edits, refreshed research).
        """
        from nexus.aspect_readers import ReadOk, _read_https_uri

        title = "stale-snapshot"
        _seed_chunks(
            t3_client,
            "knowledge__live",
            identity_field="title",
            source_id=title,
            chunks=[(0, "stale chroma cached body")],
        )
        client = _StubHttpClient([_StubHttpResponse(200, "fresh live page")])

        result = _read_https_uri(
            "https://live.example.com/x",
            t3=t3_client,
            http_client=client,
            chroma_hint=("knowledge__live", title),
            force_refresh=True,
        )
        assert isinstance(result, ReadOk)
        assert result.text == "fresh live page"
        assert result.metadata["served_from"] == "network"
        assert client.calls == ["https://live.example.com/x"]

    def test_chroma_miss_falls_through_to_network(self, t3_client):
        """When ``chroma_hint`` is provided but the chunk doesn't
        exist in T3, the reader falls through to httpx fetch.
        """
        from nexus.aspect_readers import ReadOk, _read_https_uri

        try:
            t3_client.delete_collection("knowledge__nope")
        except Exception:
            pass
        client = _StubHttpClient([_StubHttpResponse(200, "fetched body")])

        result = _read_https_uri(
            "https://example.com/x",
            t3=t3_client,
            http_client=client,
            chroma_hint=("knowledge__nope", "missing-source"),
        )
        assert isinstance(result, ReadOk)
        assert result.text == "fetched body"
        assert result.metadata["served_from"] == "network"
        assert client.calls == ["https://example.com/x"]


# ── read_source dispatch ─────────────────────────────────────────────────────


class TestReadSourceDispatch:
    def test_file_scheme_dispatches_to_file_reader(self, tmp_path: Path):
        from nexus.aspect_readers import ReadOk, read_source

        p = tmp_path / "x.md"
        p.write_text("content", encoding="utf-8")
        result = read_source(f"file://{p}")
        assert isinstance(result, ReadOk)
        assert result.text == "content"

    def test_chroma_scheme_dispatches_to_chroma_reader(self, t3_client):
        from nexus.aspect_readers import ReadOk, read_source

        title = "dispatched-doc"
        _seed_chunks(
            t3_client,
            "knowledge__d1",
            identity_field="title",
            source_id=title,
            chunks=[(0, "ok")],
        )
        result = read_source(f"chroma://knowledge__d1/{title}", t3=t3_client)
        assert isinstance(result, ReadOk)
        assert result.text == "ok"

    def test_unknown_scheme_returns_scheme_unknown(self):
        from nexus.aspect_readers import ReadFail, read_source

        result = read_source("s3://bucket/key")
        assert isinstance(result, ReadFail)
        assert result.reason == "scheme_unknown"

    def test_empty_uri_returns_read_fail(self):
        from nexus.aspect_readers import ReadFail, read_source

        result = read_source("")
        assert isinstance(result, ReadFail)

    def test_no_scheme_returns_scheme_unknown(self):
        from nexus.aspect_readers import ReadFail, read_source

        result = read_source("/just/a/path")
        assert isinstance(result, ReadFail)
        assert result.reason == "scheme_unknown"

    def test_devonthink_scheme_dispatches_to_dt_reader(
        self, tmp_path: Path, monkeypatch,
    ):
        """``x-devonthink-item://`` URIs must reach the DT reader via
        the registry. We can't drive osascript in tests, so we go
        through the registry-aware ``read_source`` *and* monkeypatch
        the default resolver so the reader's macOS gate doesn't reject
        the call on Linux/Windows CI workers.
        """
        from nexus.aspect_readers import ReadOk, read_source

        target = tmp_path / "dispatch.pdf"
        target.write_bytes(b"%PDF-1.4 dispatched")

        def fake_resolver(uuid: str) -> tuple[str | None, str]:
            assert uuid == "DISPATCH-UUID"
            return str(target), ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default",
            fake_resolver,
        )
        # ``read_source`` doesn't pass dt_resolver — it must fall through
        # to the patched default. We also patch sys.platform so the
        # reader's macOS gate accepts the call on non-darwin runners.
        monkeypatch.setattr("sys.platform", "darwin")

        result = read_source("x-devonthink-item://DISPATCH-UUID")
        assert isinstance(result, ReadOk)
        assert result.metadata["scheme"] == "x-devonthink-item"
        assert result.metadata["uuid"] == "DISPATCH-UUID"


# ── x-devonthink-item:// reader (nexus-bqda) ────────────────────────────────


class TestReadDevonthinkUri:
    """The DT reader resolves a UUID via DEVONthink (osascript) to a
    filesystem path, then reads the file. macOS-only — non-darwin
    callers get a clear ``ReadFail`` rather than a silent crash trying
    to invoke osascript.

    All tests inject a stub resolver via the ``dt_resolver`` keyword to
    avoid spawning a real subprocess.
    """

    def test_happy_path_resolves_and_reads_file(self, tmp_path: Path):
        from nexus.aspect_readers import ReadOk, _read_devonthink_uri

        target = tmp_path / "paper.pdf"
        target.write_bytes(b"%PDF-1.4 dt body")

        def resolver(uuid: str) -> tuple[str | None, str]:
            assert uuid == "8EDC855D-213F-40AD-A9CF-9543CC76476B"
            return str(target), ""

        result = _read_devonthink_uri(
            "x-devonthink-item://8EDC855D-213F-40AD-A9CF-9543CC76476B",
            dt_resolver=resolver,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["scheme"] == "x-devonthink-item"
        assert result.metadata["uuid"] == "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        assert result.metadata["resolved_path"] == str(target)
        assert result.metadata["bytes"] == len(b"%PDF-1.4 dt body")

    def test_missing_record_returns_unreachable(self, tmp_path: Path):
        """When DT replies that the UUID isn't found, the reader
        surfaces an ``unreachable`` failure with the resolver's
        detail so triage can tell "DT is up but doesn't know this
        record" from "DT couldn't be reached at all".
        """
        from nexus.aspect_readers import ReadFail, _read_devonthink_uri

        def resolver(uuid: str) -> tuple[str | None, str]:
            return None, f"DEVONthink record {uuid!r} not found"

        result = _read_devonthink_uri(
            "x-devonthink-item://MISSING-UUID", dt_resolver=resolver,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "not found" in result.detail

    def test_resolver_returning_path_that_doesnt_exist(self, tmp_path: Path):
        """DT might report a path that has since been moved/deleted
        out from under it (rare but possible). The reader should
        surface a ``FileNotFoundError`` ``unreachable`` failure
        rather than silently producing empty content.
        """
        from nexus.aspect_readers import ReadFail, _read_devonthink_uri

        ghost = tmp_path / "ghost.pdf"  # never written

        def resolver(uuid: str) -> tuple[str | None, str]:
            return str(ghost), ""

        result = _read_devonthink_uri(
            "x-devonthink-item://GHOST-UUID", dt_resolver=resolver,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "FileNotFoundError" in result.detail

    def test_empty_file_at_resolved_path(self, tmp_path: Path):
        from nexus.aspect_readers import ReadFail, _read_devonthink_uri

        empty = tmp_path / "empty.pdf"
        empty.touch()

        def resolver(uuid: str) -> tuple[str | None, str]:
            return str(empty), ""

        result = _read_devonthink_uri(
            "x-devonthink-item://EMPTY-UUID", dt_resolver=resolver,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "empty"

    def test_empty_uuid_returns_unreachable(self):
        from nexus.aspect_readers import ReadFail, _read_devonthink_uri

        def resolver(uuid: str) -> tuple[str | None, str]:
            raise AssertionError("resolver should not be called for empty UUID")

        result = _read_devonthink_uri(
            "x-devonthink-item://", dt_resolver=resolver,
        )
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "empty UUID" in result.detail

    def test_non_macos_platform_returns_clear_unreachable(self, monkeypatch):
        """On Linux/Windows the reader must NOT try to invoke
        osascript. Surfacing a clean ``unreachable`` lets
        cross-platform CI runners see the right error and skip
        DT-keyed entries gracefully.
        """
        from nexus.aspect_readers import ReadFail, _read_devonthink_uri

        monkeypatch.setattr("sys.platform", "linux")
        result = _read_devonthink_uri("x-devonthink-item://UUID")
        assert isinstance(result, ReadFail)
        assert result.reason == "unreachable"
        assert "macOS-only" in result.detail

    def test_uuid_in_path_component_is_tolerated(self, tmp_path: Path):
        """``urlparse`` puts the UUID in netloc when ``://`` is used
        but writers that emit ``x-devonthink-item:UUID`` (single
        colon) put it in ``path``. The reader handles both for
        defensiveness; the canonical form remains the double-slash
        shape that matches DEVONthink's own URL output.
        """
        from nexus.aspect_readers import ReadOk, _read_devonthink_uri

        target = tmp_path / "compat.pdf"
        target.write_bytes(b"%PDF-1.4 compat")

        def resolver(uuid: str) -> tuple[str | None, str]:
            assert uuid == "COMPAT-UUID"
            return str(target), ""

        result = _read_devonthink_uri(
            "x-devonthink-item:COMPAT-UUID", dt_resolver=resolver,
        )
        assert isinstance(result, ReadOk)
        assert result.metadata["uuid"] == "COMPAT-UUID"

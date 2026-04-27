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

    def test_rdr_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("rdr__nexus-571b8edd") == ("source_path",)

    def test_docs_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("docs__corpus") == ("source_path",)

    def test_code_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("code__nexus") == ("source_path",)

    def test_knowledge_collection_tries_source_path_then_title(self):
        from nexus.aspect_readers import _identity_fields_for
        # source_path first (paper-shaped collections like
        # knowledge__delos), title second (slug-shaped
        # knowledge__knowledge).
        assert _identity_fields_for("knowledge__knowledge") == ("source_path", "title")
        assert _identity_fields_for("knowledge__delos") == ("source_path", "title")

    def test_unknown_prefix_falls_back_to_source_path(self):
        from nexus.aspect_readers import _identity_fields_for
        assert _identity_fields_for("future__newshape") == ("source_path",)

    def test_dispatch_table_exposes_all_known_prefixes(self):
        from nexus.aspect_readers import CHROMA_IDENTITY_FIELD
        assert CHROMA_IDENTITY_FIELD["rdr__"] == ("source_path",)
        assert CHROMA_IDENTITY_FIELD["docs__"] == ("source_path",)
        assert CHROMA_IDENTITY_FIELD["code__"] == ("source_path",)
        assert CHROMA_IDENTITY_FIELD["knowledge__"] == ("source_path", "title")


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

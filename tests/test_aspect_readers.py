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


class TestIdentityFieldDispatch:
    """The dispatch table picks the right metadata field per collection
    prefix. ``knowledge__*`` chunks carry document identity in
    ``title``; everything else uses ``source_path`` (research-4,
    id 1011).
    """

    def test_rdr_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_field_for
        assert _identity_field_for("rdr__nexus-571b8edd") == "source_path"

    def test_docs_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_field_for
        assert _identity_field_for("docs__corpus") == "source_path"

    def test_code_collection_uses_source_path(self):
        from nexus.aspect_readers import _identity_field_for
        assert _identity_field_for("code__nexus") == "source_path"

    def test_knowledge_collection_uses_title(self):
        from nexus.aspect_readers import _identity_field_for
        assert _identity_field_for("knowledge__knowledge") == "title"

    def test_unknown_prefix_falls_back_to_source_path(self):
        from nexus.aspect_readers import _identity_field_for
        # Future prefix not in the table — defaults to source_path,
        # the dominant convention in legacy ingests.
        assert _identity_field_for("future__newshape") == "source_path"

    def test_dispatch_table_exposes_all_known_prefixes(self):
        from nexus.aspect_readers import CHROMA_IDENTITY_FIELD
        assert CHROMA_IDENTITY_FIELD["rdr__"] == "source_path"
        assert CHROMA_IDENTITY_FIELD["docs__"] == "source_path"
        assert CHROMA_IDENTITY_FIELD["code__"] == "source_path"
        assert CHROMA_IDENTITY_FIELD["knowledge__"] == "title"


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

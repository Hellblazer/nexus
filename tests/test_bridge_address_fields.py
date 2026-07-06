# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-169 G5 (bead nexus-jkv85): bridge consumer opt-in source_uri, always-on chash/span.

After the source_uri opt-in revision:
  - chash + span are always-on (returned without any flag, no I/O cost).
  - source_uri is OPT-IN: only present when include_source_uri=True is forwarded.
  - Default path wire shape is byte-identical to pre-G5 for existing callers.
  - Backward compat: consumer never errors when fields are absent (old service).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.db.http_vector_client import HttpVectorClient, _ServiceCollectionStub

# ---------------------------------------------------------------------------
# Fixtures — search rows and get envelopes
# ---------------------------------------------------------------------------

# Row from an opt-in search (include_source_uri=True), source_uri present
SEARCH_ROW_WITH_URI = {
    "id":         "abc123def456abc1230000000000000",
    "content":    "chunk text with address fields",
    "distance":   0.15,
    "collection": "knowledge__test__voyage-context-3__v1",
    "chash":      "abc123def456abc1230000000000000",
    "source_uri": "file:///vault/notes/test.md",
    "span":       "chash:aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
    "chunk_text_hash": "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
    "line_start":  10,
    "line_end":    20,
}

# Row from a default search (no flag), source_uri absent, chash/span present
SEARCH_ROW_DEFAULT = {
    "id":         "abc123def456abc1230000000000000",
    "content":    "chunk text default path",
    "distance":   0.15,
    "collection": "knowledge__test__voyage-context-3__v1",
    "chash":      "abc123def456abc1230000000000000",
    # NO source_uri
    "span":       "10-20",
    "chunk_text_hash": "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
}

# Row from a legacy service (pre-G5) — no new fields at all
SEARCH_ROW_LEGACY = {
    "id":         "deadbeef00000000000000000000000a",
    "content":    "legacy chunk without address fields",
    "distance":   0.25,
    "collection": "knowledge__test__voyage-context-3__v1",
    "chunk_text_hash": "deadbeef",
}

# Get envelope from opt-in path (include_source_uri=True)
GET_ENVELOPE_OPT_IN = {
    "ids":         ["abc123def456abc1230000000000000"],
    "documents":   ["chunk text with address fields"],
    "metadatas":   [{"chunk_text_hash": "aabbccddeeff..."}],
    "chashes":     ["abc123def456abc1230000000000000"],
    "source_uris": ["file:///vault/notes/test.md"],
    "spans":       ["chash:aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"],
}

# Get envelope from default path (chashes/spans present, source_uris absent)
GET_ENVELOPE_DEFAULT = {
    "ids":       ["abc123def456abc1230000000000000"],
    "documents": ["chunk text default path"],
    "metadatas": [{"chunk_text_hash": "aabbccddeeff..."}],
    "chashes":   ["abc123def456abc1230000000000000"],
    "spans":     ["10-20"],
    # NO source_uris
}

# Get envelope from a legacy service (pre-G5)
GET_ENVELOPE_LEGACY = {
    "ids":       ["deadbeef00000000000000000000000a"],
    "documents": ["legacy chunk"],
    "metadatas": [{"chunk_text_hash": "deadbeef"}],
    # NO chashes, source_uris, spans
}


def _make_client():
    """Return an HttpVectorClient with _tenant set."""
    client = HttpVectorClient.__new__(HttpVectorClient)
    client._tenant = "test-tenant"
    return client


# ---------------------------------------------------------------------------
# Search — default path: source_uri absent, chash/span present
# ---------------------------------------------------------------------------


class TestSearchDefaultPath:
    def test_default_path_has_no_source_uri(self):
        """Default search (no include_source_uri flag): source_uri absent from rows."""
        rows = [SEARCH_ROW_DEFAULT]
        client = _make_client()

        with patch("nexus.db.http_vector_client._post", return_value=rows) as mock_post:
            result = client.search("test query", ["knowledge__test__voyage-context-3__v1"])

        # include_source_uri must NOT be in the posted body
        posted_body = mock_post.call_args[0][1]
        assert "include_source_uri" not in posted_body

        row = result[0]
        assert row["id"] == "abc123def456abc1230000000000000"
        assert row["chash"] == "abc123def456abc1230000000000000"
        assert row["span"] == "10-20"
        # source_uri key absent — not KeyError, simply not present
        assert "source_uri" not in row

    def test_structured_default_path_still_works(self):
        """structured=True must still extract id/tumbler/distance/collection."""
        rows = [SEARCH_ROW_DEFAULT]
        client = _make_client()

        with patch("nexus.db.http_vector_client._post", return_value=rows):
            result = client.search(
                "test query",
                ["knowledge__test__voyage-context-3__v1"],
                structured=True,
            )

        assert result["ids"] == ["abc123def456abc1230000000000000"]
        assert result["distances"] == [pytest.approx(0.15)]
        assert result["collections"] == ["knowledge__test__voyage-context-3__v1"]


# ---------------------------------------------------------------------------
# Search — opt-in path: source_uri present when include_source_uri=True
# ---------------------------------------------------------------------------


class TestSearchOptIn:
    def test_opt_in_forwards_flag_and_returns_source_uri(self):
        """include_source_uri=True must be forwarded in the request body and
        the returned row must carry source_uri."""
        rows = [SEARCH_ROW_WITH_URI]
        client = _make_client()

        with patch("nexus.db.http_vector_client._post", return_value=rows) as mock_post:
            result = client.search(
                "test query",
                ["knowledge__test__voyage-context-3__v1"],
                include_source_uri=True,
            )

        # Flag forwarded
        posted_body = mock_post.call_args[0][1]
        assert posted_body.get("include_source_uri") is True

        row = result[0]
        assert row["id"] == "abc123def456abc1230000000000000"
        assert row["chash"] == "abc123def456abc1230000000000000"
        assert row["source_uri"] == "file:///vault/notes/test.md"
        assert row["span"].startswith("chash:")


# ---------------------------------------------------------------------------
# Search — backward compat: legacy service rows (no new fields)
# ---------------------------------------------------------------------------


class TestSearchBackwardCompat:
    def test_legacy_rows_do_not_raise(self):
        """Rows from a pre-G5 service have no chash/source_uri/span — must not error."""
        rows = [SEARCH_ROW_LEGACY]
        client = _make_client()

        with patch("nexus.db.http_vector_client._post", return_value=rows):
            result = client.search("test", ["knowledge__test__voyage-context-3__v1"])

        row = result[0]
        assert row["id"] == "deadbeef00000000000000000000000a"
        assert row.get("chash") is None
        assert row.get("source_uri") is None
        assert row.get("span") is None


# ---------------------------------------------------------------------------
# Get envelope — opt-in, default, and legacy paths
# ---------------------------------------------------------------------------


class TestGetEnvelope:
    def test_opt_in_surfaces_source_uris(self):
        """include_source_uri=True forwarded to store-get; response carries source_uris."""
        env = GET_ENVELOPE_OPT_IN
        stub = _ServiceCollectionStub("knowledge__test__voyage-context-3__v1", "test-tenant")

        with patch("nexus.db.http_vector_client._post", return_value=env) as mock_post:
            raw = stub.get(ids=["abc123def456abc1230000000000000"], include_source_uri=True)

        posted_body = mock_post.call_args[0][1]
        assert posted_body.get("include_source_uri") is True

        assert raw["ids"] == ["abc123def456abc1230000000000000"]
        assert raw["chashes"] == ["abc123def456abc1230000000000000"]
        assert raw["source_uris"] == ["file:///vault/notes/test.md"]
        assert raw["spans"][0].startswith("chash:")

    def test_default_path_has_no_source_uris(self):
        """Default get (no flag): chashes/spans present, source_uris absent."""
        env = GET_ENVELOPE_DEFAULT
        stub = _ServiceCollectionStub("knowledge__test__voyage-context-3__v1", "test-tenant")

        with patch("nexus.db.http_vector_client._post", return_value=env) as mock_post:
            raw = stub.get(where={})

        posted_body = mock_post.call_args[0][1]
        assert "include_source_uri" not in posted_body

        assert raw["ids"] == ["abc123def456abc1230000000000000"]
        assert raw["chashes"] == ["abc123def456abc1230000000000000"]
        assert raw["spans"] == ["10-20"]
        assert "source_uris" not in raw

    def test_legacy_envelope_does_not_raise(self):
        """Backward compat: pre-G5 envelope (no chashes/source_uris/spans) must not error."""
        env = GET_ENVELOPE_LEGACY
        stub = _ServiceCollectionStub("knowledge__test__voyage-context-3__v1", "test-tenant")

        with patch("nexus.db.http_vector_client._post", return_value=env):
            raw = stub.get(where={})

        assert raw["ids"] == ["deadbeef00000000000000000000000a"]
        assert raw.get("chashes") is None
        assert raw.get("source_uris") is None
        assert raw.get("spans") is None


class TestGetAllMetadata:
    """nexus-duoak follow-up: ids+metadata for a WHOLE collection in one
    round trip, replacing the indexer's ceil(N/300) paginated /get loop
    for the staleness-cache-build phase."""

    def test_posts_to_get_all_metadata_endpoint(self):
        stub = _ServiceCollectionStub("code__test__voyage-code-3__v1", "test-tenant")
        server_result = {
            "ids": ["c1", "c2"],
            "metadatas": [{"chunk_text_hash": "h1"}, {"chunk_text_hash": "h2"}],
        }

        with patch("nexus.db.http_vector_client._post", return_value=server_result) as mock_post:
            out = stub.get_all_metadata()

        path, body = mock_post.call_args[0]
        assert path == "/v1/vectors/get-all-metadata"
        assert body["collection"] == "code__test__voyage-code-3__v1"
        assert "where" not in body
        assert out == {"ids": ["c1", "c2"], "metadatas": [{"chunk_text_hash": "h1"}, {"chunk_text_hash": "h2"}]}

    def test_forwards_where_filter(self):
        stub = _ServiceCollectionStub("code__test__voyage-code-3__v1", "test-tenant")

        with patch("nexus.db.http_vector_client._post", return_value={"ids": [], "metadatas": []}) as mock_post:
            stub.get_all_metadata(where={"kind": "a"})

        _, body = mock_post.call_args[0]
        assert body["where"] == {"kind": "a"}

    def test_does_not_include_documents_key(self):
        """No 'documents' field in the response shape -- staleness only
        needs metadata, keeping the payload lean (the whole point of the
        endpoint versus the general-purpose /get)."""
        stub = _ServiceCollectionStub("code__test__voyage-code-3__v1", "test-tenant")

        with patch("nexus.db.http_vector_client._post", return_value={"ids": ["c1"], "metadatas": [{}]}):
            out = stub.get_all_metadata()

        assert "documents" not in out

    def test_raises_on_failure_does_not_degrade_to_empty(self):
        """Unlike get()/delete(), a failure must propagate -- a silently
        empty result here is indistinguishable from 'collection has 0
        chunks' to build_staleness_cache, which would build an empty cache
        instead of falling back to the paginated path."""
        from nexus.db.http_vector_client import VectorServiceError

        stub = _ServiceCollectionStub("code__test__voyage-code-3__v1", "test-tenant")

        with patch(
            "nexus.db.http_vector_client._post",
            side_effect=VectorServiceError("422 too many rows"),
        ):
            with pytest.raises(VectorServiceError):
                stub.get_all_metadata()

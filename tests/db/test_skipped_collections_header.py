# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5clu8, client half: the X-Nexus-Skipped-Collections header decodes to the labels the engine skipped.

The engine form-encodes each name and joins them with a comma (``VectorHandler.encodeSkippedCollections``; its test
``SkippedCollectionsHeaderTest`` encodes the exact value decoded here). A legacy name encodes to itself, so a header
from an older engine, which sent names raw, decodes to the same list.
"""
from __future__ import annotations

from nexus.db.http_vector_client import _log_skipped_collections, split_skipped_collections_header

_ENGINE_VALUE = (
    "notes+50%25%3A+caf%C3%A9,%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%AE%E3%83%8E%E3%83%BC%E3%83%88,"
    "a%2Cb,plus%2Bsign,x%E2%80%8Dy"
)


def test_the_engines_encoding_decodes_to_the_labels() -> None:
    assert split_skipped_collections_header(_ENGINE_VALUE) == [
        "notes 50%: café", "日本語のノート", "a,b", "plus+sign", "x‍y",
    ]


def test_an_older_engines_raw_legacy_names_decode_unchanged() -> None:
    raw = "docs__1-1__voyage-context-3__v1,knowledge__notes,code__a.b-c_d__v2"
    assert split_skipped_collections_header(raw) == raw.split(",")


def test_an_absent_or_empty_header_is_no_names() -> None:
    assert split_skipped_collections_header(None) == []
    assert split_skipped_collections_header("") == []
    assert split_skipped_collections_header(",,") == []


def test_the_logged_list_is_the_decoded_one() -> None:
    assert _log_skipped_collections("search", ["notes 50%: café"], "notes+50%25%3A+caf%C3%A9") == ["notes 50%: café"]

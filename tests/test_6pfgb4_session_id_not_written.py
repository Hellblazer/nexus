# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 nexus-6pfgb.4: no writer stamps ``session_id`` on a T3 chunk row.

Nothing reads it from a row (the census: zero readers); the T1 session id is a different
concept. ``ALLOWED_TOP_LEVEL`` still accepts the key so an old row echoed back by a
read-modify-write (``commands/enrich.py``) keeps validating.
"""
from __future__ import annotations

import inspect

import nexus.corpus as corpus
from nexus.catalog.note_write import _chunk_payload, put_note, write_note
from nexus.db.t3 import T3Database
from nexus.metadata_schema import ALLOWED_TOP_LEVEL, make_chunk_metadata, normalize, validate

_CHASH = "a" * 64


def test_the_factory_neither_takes_nor_emits_session_id():
    out = make_chunk_metadata(
        content_type="prose", chunk_text_hash=_CHASH, content_hash=_CHASH,
        indexed_at="2026-10-30T00:00:00+00:00", embedding_model="bge-base-en-v1.5",
    )
    assert "session_id" not in out
    assert "session_id" not in inspect.signature(make_chunk_metadata).parameters


def test_the_note_chunk_payload_carries_no_session_id(monkeypatch):
    monkeypatch.setattr(corpus, "embedding_model_row_preferred", lambda name: "bge-base-en-v1.5")
    chunks = _chunk_payload(
        "knowledge__subject", ["one piece"], [{"chash": _CHASH}], title="t", tags="", category="",
        source_agent="", ttl_days=None, catalog_doc_id="1.1.1", content_type="prose",
    )
    assert len(chunks) == 1, "the payload examined nothing"
    assert "session_id" not in chunks[0]["metadata"]


def test_no_write_entry_point_takes_a_session_id():
    for fn in (write_note, put_note, T3Database.put, _chunk_payload):
        assert "session_id" not in inspect.signature(fn).parameters, fn


def test_an_old_row_that_still_carries_session_id_keeps_validating():
    """The read-modify-write proof: the allowlist keeps the key, so enrich's echo of an
    old row neither raises in validate() nor loses the stale value in normalize()."""
    assert "session_id" in ALLOWED_TOP_LEVEL
    old = {"title": "t", "session_id": "old-session", "indexed_at": "2026-01-01T00:00:00+00:00"}
    assert normalize(old, content_type="prose")["session_id"] == "old-session"
    validate({"session_id": "old-session"})

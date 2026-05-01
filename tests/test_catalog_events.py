# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.catalog.events: typed event schemas and envelope.

Coverage maps to RDR-101 §"Event log" + §RF-101-2:
- All 12 event types have payload classes registered
- Envelope round-trips through ``to_dict()`` / ``from_dict()``
- Unknown event types preserve payload as opaque dict (projector emits
  the RF-101-2 unknown-(type, v) warning)
- Unknown payload keys are dropped silently (forward compat)
- ``new_doc_id`` and ``new_chunk_id`` produce valid UUID7 strings
- ``make_event`` looks up type from payload class
"""

from __future__ import annotations

import json
import uuid

import pytest

from nexus.catalog import events as ev


class TestUUID7Factories:
    """RF-101-1 + chunk-id rule deliverable."""

    def test_new_doc_id_is_uuid7(self):
        s = ev.new_doc_id()
        u = uuid.UUID(s)
        assert u.version == 7

    def test_new_chunk_id_is_uuid7(self):
        s = ev.new_chunk_id()
        u = uuid.UUID(s)
        assert u.version == 7

    def test_doc_ids_are_unique(self):
        ids = {ev.new_doc_id() for _ in range(100)}
        assert len(ids) == 100

    def test_chunk_ids_carry_timestamp_high_bits(self):
        # UUID7's first 48 bits are unix_ts_ms. Within-millisecond ties may
        # not be monotonic (uuid7-standard fills the tail with random bits)
        # but across-millisecond ordering must hold so the catalog can
        # use UUID7 as a rough time index.
        import time as _time

        ids: list[str] = []
        for _ in range(5):
            ids.append(ev.new_chunk_id())
            _time.sleep(0.005)  # 5 ms — well above the ms granularity
        assert ids == sorted(ids), (
            "UUID7s minted across multiple milliseconds must sort in "
            "creation order"
        )


class TestPayloadRegistry:
    """All 12 event types from RDR-101 §Event log have a registered payload."""

    def test_all_event_types_registered(self):
        for t in ev.ALL_EVENT_TYPES:
            assert ev.payload_class(t) is not None, (
                f"Event type {t!r} has no registered payload class"
            )

    def test_unknown_type_returns_none(self):
        assert ev.payload_class("FutureEventType") is None

    def test_event_type_count_matches_rdr_101(self):
        # RDR-101 §Event log enumerated 12 event types at Phase 1; Phase 3
        # follow-up nexus-o6aa.9.4 added OwnerDeleted (v: 0 dedupe path).
        # Update this assertion alongside any addition.
        assert len(ev.ALL_EVENT_TYPES) == 13


class TestEnvelopeRoundtrip:
    """Round-trip every payload type through ``to_dict()`` / ``from_dict()``."""

    @pytest.fixture
    def all_payloads(self):
        return [
            ev.OwnerRegisteredPayload(
                owner_id="1.42",
                name="nexus",
                owner_type="repo",
                repo_root="/git/nexus",
                repo_hash="abc123",
                description="test repo",
            ),
            ev.CollectionCreatedPayload(
                coll_id="code__nexus__voyage-3@2024-08",
                owner_id="1.42",
                content_type="code",
                embedding_model="voyage-3",
                model_version="2024-08",
                name="code__nexus__voyage-3@2024-08",
            ),
            ev.CollectionSupersededPayload(
                old_coll_id="code__nexus__voyage-3@2024-08",
                new_coll_id="code__nexus__voyage-3@2025-01",
                reason="rotate-model-version",
            ),
            ev.DocumentRegisteredPayload(
                doc_id=ev.new_doc_id(),
                owner_id="1.42",
                content_type="code",
                source_uri="file:///git/nexus/src/nexus/__init__.py",
                coll_id="code__nexus__voyage-3@2024-08",
                title="nexus/__init__.py",
                source_mtime=1714410000.0,
                indexed_at_doc="2026-04-30T12:00:00+00:00",
            ),
            ev.DocumentRenamedPayload(
                doc_id=ev.new_doc_id(),
                new_source_uri="file:///git/nexus/src/nexus/renamed.py",
            ),
            ev.DocumentAliasedPayload(
                alias_doc_id=ev.new_doc_id(),
                canonical_doc_id=ev.new_doc_id(),
            ),
            ev.DocumentEnrichedPayload(
                doc_id=ev.new_doc_id(),
                schema_version=ev.SCHEMA_BIB_S2_V1,
                payload={
                    "semantic_scholar_id": "abc123",
                    "doi": "10.0/xyz",
                    "year": 2024,
                    "authors": "Smith, J. and Jones, A.",
                    "venue": "ACL",
                    "citation_count": 12,
                },
                enriched_at="2026-04-30T12:00:00+00:00",
            ),
            ev.DocumentDeletedPayload(
                doc_id=ev.new_doc_id(),
                reason="user-requested",
            ),
            ev.OwnerDeletedPayload(
                owner_id="1.42",
                reason="dedupe.orphan",
            ),
            ev.ChunkIndexedPayload(
                chunk_id=ev.new_chunk_id(),
                chash="deadbeef" * 8,
                doc_id=ev.new_doc_id(),
                coll_id="code__nexus__voyage-3@2024-08",
                position=42,
                content_hash="abcd" * 16,
                embedded_at="2026-04-30T12:00:00+00:00",
            ),
            ev.ChunkOrphanedPayload(
                chunk_id=ev.new_chunk_id(),
                reason="document-deleted",
            ),
            ev.LinkCreatedPayload(
                from_doc="1.7.42",
                to_doc="1.7.43",
                link_type="cites",
                span_chash="chash:" + "ab" * 32,
                creator="bib_enricher",
            ),
            ev.LinkDeletedPayload(
                from_doc="1.7.42",
                to_doc="1.7.43",
                link_type="cites",
                reason="rebuild",
            ),
        ]

    def test_every_payload_roundtrips(self, all_payloads):
        for payload in all_payloads:
            event = ev.make_event(payload)
            d = event.to_dict()
            # JSON-serializable
            blob = json.dumps(d)
            restored_dict = json.loads(blob)
            restored = ev.Event.from_dict(restored_dict)
            assert restored.type == event.type
            assert restored.v == event.v
            assert restored.ts == event.ts
            assert restored.payload == payload, (
                f"Payload round-trip mismatch for {event.type}: "
                f"got {restored.payload!r}, expected {payload!r}"
            )

    def test_envelope_keys(self, all_payloads):
        for payload in all_payloads:
            d = ev.make_event(payload).to_dict()
            assert set(d.keys()) == {"type", "v", "payload", "ts"}


class TestVersioning:
    """Envelope ``v`` versioning per RF-101-2."""

    def test_default_version_is_0(self):
        # Default flipped from 1→0 per the round-3 review: v=1 has no
        # production projector landing site (it raises) and the previous
        # default produced a silent-drop trap when callers forgot to
        # pass ``v=0``. Default to the implemented schema; opt into v=1
        # explicitly when a v: 1 handler is wired.
        e = ev.make_event(ev.DocumentDeletedPayload(doc_id="x", reason="y"))
        assert e.v == 0

    def test_explicit_v_one(self):
        e = ev.make_event(
            ev.DocumentDeletedPayload(doc_id="x", reason="phase3_native"),
            v=1,
        )
        assert e.v == 1
        d = e.to_dict()
        assert d["v"] == 1

    def test_synthesized_v_zero(self):
        e = ev.make_event(
            ev.DocumentDeletedPayload(doc_id="x", reason="synthesized_from_tombstone"),
            v=0,
        )
        assert e.v == 0
        d = e.to_dict()
        assert d["v"] == 0


class TestForwardCompat:
    """Unknown payload keys must drop silently; unknown event types preserve dict."""

    def test_unknown_payload_key_dropped(self):
        raw = {
            "type": ev.TYPE_DOCUMENT_DELETED,
            "v": 1,
            "ts": "2026-04-30T12:00:00+00:00",
            "payload": {
                "doc_id": "abc",
                "reason": "test",
                "future_field": "ignored",  # not in DocumentDeletedPayload
            },
        }
        e = ev.Event.from_dict(raw)
        assert isinstance(e.payload, ev.DocumentDeletedPayload)
        assert e.payload.doc_id == "abc"
        assert e.payload.reason == "test"
        assert not hasattr(e.payload, "future_field")

    def test_unknown_event_type_preserves_payload_as_dict(self):
        raw = {
            "type": "FutureEventType",
            "v": 1,
            "ts": "2026-04-30T12:00:00+00:00",
            "payload": {"a": 1, "b": "two", "nested": {"c": 3}},
        }
        e = ev.Event.from_dict(raw)
        assert e.type == "FutureEventType"
        assert isinstance(e.payload, dict)
        assert e.payload == {"a": 1, "b": "two", "nested": {"c": 3}}

    def test_missing_v_defaults_to_zero(self):
        # v: 0 is legacy/synthesized; the dispatch on (type, v) must see
        # int 0 not None when callers omit the field.
        raw = {
            "type": ev.TYPE_DOCUMENT_DELETED,
            "ts": "2026-04-30T12:00:00+00:00",
            "payload": {"doc_id": "x", "reason": "y"},
        }
        e = ev.Event.from_dict(raw)
        assert e.v == 0


class TestMakeEvent:
    """``make_event`` looks up the type from the payload class."""

    def test_correct_type_for_each_payload(self):
        cases = [
            (ev.OwnerRegisteredPayload(owner_id="1.1", name="x", owner_type="repo"),
             ev.TYPE_OWNER_REGISTERED),
            (ev.DocumentDeletedPayload(doc_id="x", reason="y"),
             ev.TYPE_DOCUMENT_DELETED),
            (ev.ChunkOrphanedPayload(chunk_id="x", reason="y"),
             ev.TYPE_CHUNK_ORPHANED),
        ]
        for payload, expected_type in cases:
            e = ev.make_event(payload)
            assert e.type == expected_type

    def test_make_event_rejects_dict_payload(self):
        # Avoid silent-success on a dict that happens to look like a payload.
        with pytest.raises(ValueError, match="Unknown payload class"):
            ev.make_event({"doc_id": "x"})

    def test_make_event_populates_ts(self):
        e = ev.make_event(ev.DocumentDeletedPayload(doc_id="x", reason="y"))
        assert e.ts  # non-empty
        # ISO-8601 with timezone offset
        assert "T" in e.ts
        assert e.ts.endswith("+00:00") or e.ts.endswith("Z")

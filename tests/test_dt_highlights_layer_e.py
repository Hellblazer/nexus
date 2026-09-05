# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer E — DT highlight helpers + nx dt index --highlights wiring."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ── DT helpers: no-content filtering + key handling ─────────────────────────

def test_extract_highlights_returns_markdown() -> None:
    from nexus.mcp_client.devonthink import dt_extract_highlights
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"text": "## Highlights\n- a point"}):
        assert dt_extract_highlights("U") == "## Highlights\n- a point"


def test_extract_highlights_no_content_message_is_none() -> None:
    from nexus.mcp_client.devonthink import dt_extract_highlights
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"text": "No highlights found across 1 source record(s)"}):
        assert dt_extract_highlights("U") is None


def test_extract_highlights_unavailable_is_none() -> None:
    from nexus.mcp_client.devonthink import dt_extract_highlights
    with patch("nexus.mcp_client.devonthink.dt_call", return_value=None):
        assert dt_extract_highlights("U") is None


def test_extract_mentions_reads_markdown_key() -> None:
    # partial-success envelope carries the body under "markdown"
    from nexus.mcp_client.devonthink import dt_extract_mentions
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"markdown": "@alice mentioned", "succeeded": 1}):
        assert dt_extract_mentions("U") == "@alice mentioned"


def test_extract_highlights_empty_text_is_none() -> None:
    from nexus.mcp_client.devonthink import dt_extract_highlights
    with patch("nexus.mcp_client.devonthink.dt_call", return_value={"text": "   "}):
        assert dt_extract_highlights("U") is None


def test_short_no_content_sentinel_is_none() -> None:
    from nexus.mcp_client.devonthink import dt_extract_highlights
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"text": "No annotations found across 1 source record(s)"}):
        assert dt_extract_highlights("U") is None


def test_long_body_opening_with_sentinel_phrase_is_kept() -> None:
    """MEDIUM-2: a real highlight blob that merely opens with 'No annotations'
    prose must NOT be discarded as a no-content sentinel."""
    from nexus.mcp_client.devonthink import dt_extract_highlights
    body = (
        "No annotations were strictly required, but the author's key claim, "
        "that paged attention dominates throughput, is highlighted here, "
        "along with twelve supporting passages spanning the whole paper and "
        "several margin notes the reader added during a second close reading."
    )
    assert len(body) > 200
    with patch("nexus.mcp_client.devonthink.dt_call", return_value={"text": body}):
        assert dt_extract_highlights("U") == body


# ── _ingest_highlights_record ───────────────────────────────────────────────

# _register_real_doc DELETED (nexus-i711w Stage 2 sub-stage A2): it existed so
# writes could satisfy the ENGINE's FK to catalog_documents.tumbler
# (nexus-aqbrk) when the tests round-tripped through a real store. Both former
# callers now route through _FakeHttpHighlights (the SQLite DocumentHighlights
# they exercised is deleted; HttpDocumentHighlightsStore is the only store),
# so no real registration is needed here. The FK contract itself stays pinned
# by the engine-side store/repository tests.


def _patch_catalog(monkeypatch, tmp_path, tumbler="1.2.3", collection="c"):
    """Route the dt helpers' factory-resolved catalog to a mock.

    nexus-i711w: the ``nexus.catalog.catalog.Catalog`` patch that used to
    accompany this died with the local catalog — the helpers reach the
    catalog exclusively via the (service-only) factory seam now.
    """
    entry = MagicMock()
    entry.tumbler = tumbler
    entry.physical_collection = collection
    cat = MagicMock()
    cat.by_source_uri.return_value = entry
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    # RDR-146 P1.2: dt helpers reach the catalog via the factory; route
    # both reader and writer to the same mock.
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda **kw: cat
    )
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_writer", lambda **kw: cat
    )
    return cat


def test_ingest_highlights_record_writes_store(tmp_path, monkeypatch) -> None:
    """The ingest helper resolves the catalog entry, builds the record, and
    writes it through the highlights store — then the SAME store seam reads it
    back. Ported (nexus-i711w Stage 2 sub-stage A2) off the deleted SQLite
    DocumentHighlights/T2-facade round-trip onto the
    HttpDocumentHighlightsStore construction seam with a stateful fake; the
    real HTTP round-trip is pinned in tests/db/test_http_aspects_stores.py."""
    from nexus.commands import dt as dt_mod

    _coll = "knowledge__dt__voyage-context-3__v1"
    _patch_catalog(monkeypatch, tmp_path, tumbler="1.7", collection=_coll)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_highlights",
                        lambda u: "## Highlights\n- x")
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_mentions",
                        lambda u: None)
    fake = _FakeHttpHighlights()
    monkeypatch.setattr(
        "nexus.db.t2.http_document_highlights_store.HttpDocumentHighlightsStore",
        lambda: fake,
    )

    assert dt_mod._ingest_highlights_record("ABC") is True
    # Read back through the same store seam the show command uses.
    rec = dt_mod._open_highlights_store().get("1.7")
    assert rec is not None
    assert rec.doc_id == "1.7"
    assert rec.collection == _coll
    assert rec.highlights_md == "## Highlights\n- x"
    assert rec.source_uri == "x-devonthink-item://ABC"


def test_ingest_highlights_record_no_highlights_is_false(tmp_path, monkeypatch) -> None:
    from nexus.commands import dt as dt_mod

    _patch_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_highlights", lambda u: None)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_mentions", lambda u: None)

    assert dt_mod._ingest_highlights_record("ABC") is False


# ── nx dt index --highlights routing + nx dt highlights show ────────────────

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_gather(monkeypatch):
    records: list[tuple[str, str]] = []
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    return records


def test_index_highlights_flag_routes_and_summarizes(runner, fake_gather, monkeypatch) -> None:
    from nexus.cli import main

    fake_gather.append(("U1", "/a.pdf"))
    # nexus-fdk1x: index_cmd probes DT reachability once up front when
    # --highlights is set; force it on so this CLI-wiring test keeps
    # exercising the stubbed _ingest_highlights_record below rather than
    # tripping the new "DEVONthink MCP unreachable" exit-2 path.
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: True)
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, *, collection, corpus, dry_run, extractor="auto", force=False: (True, 1))
    calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._ingest_highlights_record",
                        lambda uuid: calls.append(uuid) or True)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--highlights"])
    assert result.exit_code == 0, result.output
    assert calls == ["U1"]
    assert "1 highlights ingested" in result.output


def test_no_highlights_flag_skips_ingest(runner, fake_gather, monkeypatch) -> None:
    from nexus.cli import main

    fake_gather.append(("U1", "/a.pdf"))
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, *, collection, corpus, dry_run, extractor="auto", force=False: (True, 1))
    calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._ingest_highlights_record",
                        lambda uuid: calls.append(uuid) or True)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "highlights ingested" not in result.output


def test_highlights_show_command(runner, tmp_path, monkeypatch) -> None:
    """`nx dt highlights <tumbler>` reads through _open_highlights_store and
    renders the record; a miss is a clean error. Ported (nexus-i711w Stage 2
    sub-stage A2) off the deleted SQLite DocumentHighlights/T2-facade seeding
    onto the HttpDocumentHighlightsStore construction seam."""
    from nexus.cli import main
    from nexus.db.t2.records import HighlightRecord

    fake = _FakeHttpHighlights()
    monkeypatch.setattr(
        "nexus.db.t2.http_document_highlights_store.HttpDocumentHighlightsStore",
        lambda: fake,
    )
    fake.upsert(HighlightRecord(
        doc_id="1.2", source_uri="x-devonthink-item://ABC",
        collection="c",
        highlights_md="## Highlights\n- the point", mentions_md="",
        ingested_at="2026-05-30T00:00:00Z",
    ))
    result = runner.invoke(main, ["dt", "highlights", "1.2"])
    assert result.exit_code == 0, result.output
    assert "the point" in result.output
    # unknown tumbler -> clean error
    miss = runner.invoke(main, ["dt", "highlights", "9.9.9"])
    assert miss.exit_code != 0
    assert "no ingested highlights" in miss.output


# ── nexus-g8r2h: highlights route via the storage facade ─────────────────────


class _FakeHttpHighlights:
    """Stateful stand-in for HttpDocumentHighlightsStore: records calls AND
    round-trips upserted records so write-then-read tests exercise the same
    store seam the production paths share (nexus-i711w Stage 2 sub-stage A2)."""

    def __init__(self) -> None:
        self.upserts: list = []
        self.gets: list = []
        self._by_doc: dict = {}
        self._by_uri: dict = {}

    def upsert(self, record) -> bool:
        self.upserts.append(record)
        self._by_doc[record.doc_id] = record
        self._by_uri[record.source_uri] = record
        return True

    def get(self, doc_id):
        self.gets.append(("get", doc_id))
        return self._by_doc.get(doc_id)

    def get_by_source_uri(self, uri):
        self.gets.append(("uri", uri))
        return self._by_uri.get(uri)


def test_ingest_routes_to_http_store_in_service_mode(
    tmp_path, monkeypatch,
) -> None:
    """nexus-g8r2h: on a migrated (service-mode) box the writer must hit the
    HTTP store — the old direct DocumentHighlights(default_db_path()) write
    landed in local SQLite where nothing service-side reads (split-brain)."""
    from nexus.commands import dt as dt_mod
    from nexus.db.storage_mode import StorageBackend

    _patch_catalog(monkeypatch, tmp_path,
                   collection="knowledge__dt__stub-cce-1024__v1")
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_highlights",
                        lambda u: "## Highlights\n- svc")
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_mentions",
                        lambda u: None)
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )
    fake = _FakeHttpHighlights()
    monkeypatch.setattr(
        "nexus.db.t2.http_document_highlights_store.HttpDocumentHighlightsStore",
        lambda: fake,
    )

    assert dt_mod._ingest_highlights_record("SVC") is True
    assert len(fake.upserts) == 1
    assert fake.upserts[0].highlights_md == "## Highlights\n- svc"


def test_show_reads_http_store_in_service_mode(monkeypatch) -> None:
    from nexus.commands import dt as dt_mod
    from nexus.db.storage_mode import StorageBackend

    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )
    fake = _FakeHttpHighlights()
    monkeypatch.setattr(
        "nexus.db.t2.http_document_highlights_store.HttpDocumentHighlightsStore",
        lambda: fake,
    )
    store = dt_mod._open_highlights_store()
    assert store is fake

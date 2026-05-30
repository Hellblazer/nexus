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

def _patch_catalog(monkeypatch, tmp_path, tumbler="1.2.3", collection="c"):
    """Patch nexus.catalog.catalog.Catalog (lazy-imported inside the helper)."""
    entry = MagicMock()
    entry.tumbler = tumbler
    entry.physical_collection = collection
    cat = MagicMock()
    cat.by_source_uri.return_value = entry
    cat._db = MagicMock()
    CatalogMock = MagicMock(return_value=cat)
    CatalogMock.is_initialized = staticmethod(lambda p: True)
    monkeypatch.setattr("nexus.catalog.catalog.Catalog", CatalogMock)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    return cat


def test_ingest_highlights_record_writes_store(tmp_path, monkeypatch) -> None:
    from nexus.commands import dt as dt_mod
    from nexus.db.t2.document_highlights import DocumentHighlights

    _patch_catalog(monkeypatch, tmp_path,
                   collection="knowledge__dt__voyage-context-3__v1")
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "memory.db")
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_highlights",
                        lambda u: "## Highlights\n- x")
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_extract_mentions",
                        lambda u: None)

    assert dt_mod._ingest_highlights_record("ABC") is True
    rec = DocumentHighlights(tmp_path / "memory.db").get("1.2.3")
    assert rec is not None
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
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, *, collection, corpus, dry_run: True)
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
                        lambda uuid, path, *, collection, corpus, dry_run: True)
    calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._ingest_highlights_record",
                        lambda uuid: calls.append(uuid) or True)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "highlights ingested" not in result.output


def test_highlights_show_command(runner, tmp_path, monkeypatch) -> None:
    from nexus.cli import main
    from nexus.db.t2.document_highlights import DocumentHighlights, HighlightRecord

    db = tmp_path / "memory.db"
    DocumentHighlights(db).upsert(HighlightRecord(
        doc_id="1.2.3", source_uri="x-devonthink-item://ABC", collection="c",
        highlights_md="## Highlights\n- the point", mentions_md="",
        ingested_at="2026-05-30T00:00:00Z",
    ))
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db)
    result = runner.invoke(main, ["dt", "highlights", "1.2.3"])
    assert result.exit_code == 0, result.output
    assert "the point" in result.output
    # unknown tumbler -> clean error
    miss = runner.invoke(main, ["dt", "highlights", "9.9.9"])
    assert miss.exit_code != 0
    assert "no ingested highlights" in miss.output

# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-njn1a (7.88.0 shakedown, finding 6): a chunk row's or a bundle record's ``ttl_days`` is
whatever a writer stored, and no reader may abort on it.

``nx store list`` and the MCP ``store_list`` computed ``timedelta(days=ttl_days)`` and caught only
ValueError, so one row with ``ttl_days`` 10**12 killed the whole listing with OverflowError, and a
string ``ttl_days`` died on the ``> 0`` comparison. Both now read the row by the rules of
``metadata_schema.is_expired``. ``nx catalog import`` wrote a record's ``ttl_days`` verbatim past
the 36500-day cap ``nx store put --ttl`` enforces.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

COLLECTION = "knowledge__x__voyage-context-3__v1"
INDEXED = "2026-01-01T00:00:00+00:00"

#: ttl_days values a row can carry that are not a TTL that lapses: each reads as permanent.
BAD_TTLS = [10**12, 10**30, float("inf"), float("nan"), "30", "x", [3], {"d": 3}, True, False, -5, 0, None]


def _entries() -> list[dict]:
    rows = [{"id": f"{i:064x}", "title": f"t{i}", "indexed_at": INDEXED, "ttl_days": ttl}
            for i, ttl in enumerate(BAD_TTLS)]
    rows.append({"id": f"{99:064x}", "title": "good", "indexed_at": INDEXED, "ttl_days": 30})
    return rows


def _mcp_list(entries: list[dict]) -> str:
    from nexus.mcp.core import store_list

    t3 = MagicMock()
    t3.collection_info.return_value = {"count": len(entries), "metadata": {}}
    t3.list_store.return_value = entries
    with patch("nexus.mcp.core._get_t3", return_value=t3):
        return store_list(collection=COLLECTION, limit=50, offset=0)


def _cli_list(entries: list[dict]) -> str:
    from nexus.cli import main

    t3 = MagicMock()
    t3.collection_info.return_value = {"count": len(entries), "metadata": {}}
    t3.list_store.return_value = entries
    t3.list_collections.return_value = [{"name": COLLECTION, "count": len(entries)}]
    with patch("nexus.commands.store._t3", return_value=t3):
        result = CliRunner().invoke(main, ["store", "list", "--collection", COLLECTION, "--limit", "50"])
    assert result.exception is None, repr(result.exception)
    return result.output


@pytest.mark.parametrize("lister", [_cli_list, _mcp_list], ids=["cli", "mcp"])
def test_a_bad_ttl_row_does_not_abort_the_listing(lister) -> None:
    out = lister(_entries())
    rows = [ln for ln in out.splitlines() if ln.startswith("  ") and INDEXED[:10] in ln]
    good = [ln for ln in rows if " good " in ln]
    bad = [ln for ln in rows if " good " not in ln]
    assert len(bad) == len(BAD_TTLS), out
    assert all("permanent" in ln for ln in bad), out
    assert len(good) == 1 and "expires 2026-01-31" in good[0], out


#: a ttl_days a timedelta can hold whose expiry DATE does not fit in a datetime (about 2.9M days from 2026
#: on): chunk_ttl_days keeps it, and the listing must still read it as permanent, as the CHANGELOG says.
OVERFLOWING_DATE_TTLS = [3_000_000, 10**8, 999_999_999]


@pytest.mark.parametrize("ttl", OVERFLOWING_DATE_TTLS)
def test_ttl_listing_label_reads_an_expiry_past_the_calendar_as_permanent(ttl) -> None:
    from nexus.metadata_schema import ttl_listing_label

    assert ttl_listing_label(ttl, INDEXED) == "permanent"
    assert ttl_listing_label(float(ttl), INDEXED) == "permanent"


@pytest.mark.parametrize("lister", [_cli_list, _mcp_list], ids=["cli", "mcp"])
def test_a_calendar_overflowing_ttl_lists_as_permanent(lister) -> None:
    rows = [{"id": f"{i:064x}", "title": f"t{i}", "indexed_at": INDEXED, "ttl_days": ttl}
            for i, ttl in enumerate(OVERFLOWING_DATE_TTLS)]
    out = lister(rows)
    listed = [ln for ln in out.splitlines() if ln.startswith("  ") and INDEXED[:10] in ln]
    assert len(listed) == len(rows) and all("permanent" in ln and "ttl " not in ln for ln in listed), out


def test_ttl_listing_label_keeps_ttl_nd_for_an_unparseable_stamp() -> None:
    from nexus.metadata_schema import ttl_listing_label

    assert ttl_listing_label(30, "not-a-date") == "ttl 30d"
    assert ttl_listing_label(30, "") == "permanent"


def test_chunk_ttl_days_follows_the_is_expired_rules() -> None:
    from nexus.metadata_schema import chunk_ttl_days

    assert chunk_ttl_days(30) == 30
    assert chunk_ttl_days(1.5) == 1.5
    for bad in BAD_TTLS:
        assert chunk_ttl_days(bad) is None, bad


def test_positive_ttl_caps_at_the_store_put_maximum() -> None:
    from nexus.catalog.recovery_bundle import _positive_ttl
    from nexus.ttl import MAX_TTL_DAYS, parse_ttl

    assert MAX_TTL_DAYS == 36500
    assert parse_ttl(f"{MAX_TTL_DAYS}d") == MAX_TTL_DAYS
    assert _positive_ttl(MAX_TTL_DAYS) == MAX_TTL_DAYS
    assert _positive_ttl(MAX_TTL_DAYS + 1) == MAX_TTL_DAYS
    assert _positive_ttl(timedelta.max.days) == MAX_TTL_DAYS
    assert _positive_ttl(30) == 30
    for permanent in (0, -1, None, "30", 3.0, True):
        assert _positive_ttl(permanent) is None, permanent


def test_positive_ttl_reads_a_value_no_date_can_hold_as_permanent() -> None:
    """A TTL above timedelta's range is the same permanent the readers (chunk_ttl_days, note_ttl_days)
    make of it; clamping it to 36500 would turn a never-lapses row into one that lapses in a century."""
    from nexus.catalog.recovery_bundle import _positive_ttl

    for permanent in (timedelta.max.days + 1, 10**12, 10**30, float("inf"), float("nan"), -float("inf")):
        assert _positive_ttl(permanent) is None, permanent


def test_positive_ttl_logs_the_original_value_when_it_clamps() -> None:
    from structlog.testing import capture_logs

    from nexus.catalog.recovery_bundle import _positive_ttl

    with capture_logs() as logs:
        assert _positive_ttl(40_000) == 36500
        assert _positive_ttl(30) == 30
        assert _positive_ttl(10**12) is None
    clamped = [e for e in logs if e["event"] == "recovery_bundle_ttl_clamped"]
    assert len(clamped) == 1 and clamped[0]["log_level"] == "warning", logs
    assert clamped[0]["ttl_days"] == 40_000 and clamped[0]["clamped_to"] == 36500


def test_the_note_writers_refuse_a_ttl_past_the_store_put_maximum() -> None:
    """put_note, write_note and T3Database.put refuse above MAX_TTL_DAYS as they refuse <= 0, before any
    side effect; nx store put --ttl and the MCP store_put already cap through parse_ttl."""
    from nexus.catalog.note_write import put_note, write_note
    from nexus.db.t3 import T3Database
    from nexus.ttl import MAX_TTL_DAYS

    over = MAX_TTL_DAYS + 1
    with pytest.raises(ValueError, match=f"ttl_days={over}"):
        put_note(content="njn1a", collection=COLLECTION, ttl_days=over)
    with pytest.raises(ValueError, match=f"ttl_days={over}"):
        write_note(catalog_doc_id="1.1.1", collection=COLLECTION, pieces=["x"], ttl_days=over)
    with pytest.raises(ValueError, match=f"ttl_days={over}"):
        object.__new__(T3Database).put(collection=COLLECTION, content="njn1a", ttl_days=over)


def test_bundle_import_hands_the_note_writer_a_capped_ttl(monkeypatch) -> None:
    from nexus.catalog import note_write
    from nexus.catalog import recovery_bundle as rb

    class _Stop(Exception):
        pass

    seen: dict = {}

    def _put_note(**kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(rb, "target_collection_for", lambda recorded, t3: "knowledge__njn1a__bge-base-en-v15-768__v1")
    monkeypatch.setattr(note_write, "put_note", _put_note)
    rec = {"record": "knowledge_doc", "collection": "knowledge__njn1a", "title": "njn1a-ttl",
           "content": "body", "tags": "", "category": "", "ttl_days": 40_000}
    with pytest.raises(_Stop):
        rb._default_import_doc(object(), rec)
    assert seen["ttl_days"] == 36500


def test_bundle_import_reads_a_ttl_past_timedelta_as_permanent(monkeypatch) -> None:
    from nexus.catalog import note_write
    from nexus.catalog import recovery_bundle as rb

    class _Stop(Exception):
        pass

    seen: dict = {}

    def _put_note(**kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(rb, "target_collection_for", lambda recorded, t3: "knowledge__njn1a__bge-base-en-v15-768__v1")
    monkeypatch.setattr(note_write, "put_note", _put_note)
    rec = {"record": "knowledge_doc", "collection": "knowledge__njn1a", "title": "njn1a-ttl",
           "content": "body", "tags": "", "category": "", "ttl_days": 10**12}
    with pytest.raises(_Stop):
        rb._default_import_doc(object(), rec)
    assert seen["ttl_days"] is None


def test_context_annotations_read_a_bad_ttl_as_permanent() -> None:
    """The annotation line is the sibling reader: a bool ttl_days used to render as 'expires in 1d'."""
    from datetime import UTC, datetime

    from nexus.context_annotations import annotation_line, entry_facts

    now = datetime(2026, 1, 10, tzinfo=UTC)
    for bad in (True, "x", float("nan"), 10**30):
        line = annotation_line({"indexed_at": INDEXED, "ttl_days": bad}, now=now)
        assert "expire" not in line, (bad, line)
    assert "expires in 20d" in annotation_line({"indexed_at": INDEXED, "ttl_days": 29}, now=now)

    class _Entry:
        title = "t"
        file_path = ""
        indexed_at = INDEXED
        meta = {"ttl_days": True}

    assert "ttl_days" not in entry_facts(_Entry())


@pytest.fixture(autouse=True)
def _no_catalog_tags(monkeypatch):
    """nexus-6pfgb.38: store list/get look a note's tags up in the catalog; these tests stub the T3 and
    have no catalog, so the lookup is stubbed (tests/test_6pfgb38_tags_display.py covers it)."""
    monkeypatch.setattr("nexus.catalog.store_hook.catalog_tags_for_chunks", lambda *a, **k: {})

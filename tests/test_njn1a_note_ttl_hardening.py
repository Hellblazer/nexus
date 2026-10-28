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
    assert _positive_ttl(10**12) == MAX_TTL_DAYS
    assert _positive_ttl(30) == 30
    for permanent in (0, -1, None, "30", 3.0, True):
        assert _positive_ttl(permanent) is None, permanent


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
           "content": "body", "tags": "", "category": "", "ttl_days": 10**12}
    with pytest.raises(_Stop):
        rb._default_import_doc(object(), rec)
    assert seen["ttl_days"] == 36500


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

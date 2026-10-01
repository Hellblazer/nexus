# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.35 (M4): the MCP ``store_list`` names its total as STORED.

``collection_info()["count"]`` is every physical row (RDR-192 Step 5
amendment); ``list_store`` returns live rows. ``nx store list`` already says
"N stored" (nexus-sis0m.3); the MCP tool still printed "of N" beside a live
page, overstating the paging, and answered an all-unowned collection with
"No entries at offset 0 (total N)" as if the offset were the problem.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

COLLECTION = "knowledge__x__voyage-context-3__v1"


def _list(entries: list[dict], *, stored: int, limit: int = 20, offset: int = 0) -> str:
    from nexus.mcp.core import store_list

    t3 = MagicMock()
    t3.collection_info.return_value = {"count": stored, "metadata": {}}
    t3.list_store.return_value = entries
    with patch("nexus.mcp.core._get_t3", return_value=t3):
        return store_list(collection=COLLECTION, limit=limit, offset=offset)


def _entry(i: int) -> dict:
    return {"id": f"{i:064x}", "title": f"t{i}"}


def test_header_names_the_total_as_stored() -> None:
    out = _list([_entry(1), _entry(2)], stored=62)
    assert "(showing 1-2; 62 stored)" in out, out
    assert " of 62" not in out, out


def test_no_next_hint_after_a_short_page() -> None:
    """The stored total exceeds the live rows, so a short page is the end."""
    out = _list([_entry(1), _entry(2)], stored=62, limit=5)
    assert "next: offset" not in out, out
    assert "(end)" in out, out


def test_next_hint_after_a_full_page() -> None:
    out = _list([_entry(1), _entry(2)], stored=62, limit=2)
    assert "next: offset=2" in out, out


def test_an_empty_page_beside_a_stored_total_says_what_it_means() -> None:
    out = _list([], stored=7)
    assert "7 stored" in out, out
    assert "no live" in out.lower() or "none are live" in out.lower(), out


def test_a_limit_above_the_engine_page_cap_is_not_read_as_the_end() -> None:
    """The engine clamps one ``/v1/vectors/get`` page to 300 rows. A caller
    asking for 500 on a collection holding 450 live rows gets 300 back; that is
    a full page, not the end, so the next-offset hint must still appear
    (fix round 2: the first cut compared the page against the requested limit,
    read 300 < 500 as the end and printed "(end)" with 150 rows unlisted)."""
    from nexus.db.limits import MAX_QUERY_RESULTS

    page = [_entry(i) for i in range(MAX_QUERY_RESULTS)]
    out = _list(page, stored=450, limit=500)
    assert f"next: offset={MAX_QUERY_RESULTS}" in out, out
    assert "(end)" not in out, out


def test_a_short_page_under_an_oversized_limit_is_still_the_end() -> None:
    """The clamp must not turn every short page into a hint: 120 rows under a
    limit of 500 is the end of the listing."""
    out = _list([_entry(i) for i in range(120)], stored=120, limit=500)
    assert "(end)" in out, out
    assert "next: offset" not in out, out

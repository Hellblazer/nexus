# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qe78r: a real 0 in a chunk's span reaches the manifest as 0, not NULL.

``make_chunk_metadata`` defaults all four span fields to 0, so 0 is the producers' "not recorded"
sentinel too. The two readings are told apart by what 0 can mean in each field: lines are 1-indexed
(0 is never a line), ``chunk_end_char`` is exclusive and a chunk is never empty (0 is never an end),
and ``chunk_start_char`` is 0 for a chunk at the head of its text, which is real exactly when the
chunk's end is known. The blanket ``m.get(field) or None`` turned that last case into NULL, so the
first chunk of every document lost its character span.

Pure unit tests (no engine): the two row producers, the backfill readers that copy a T3 chunk's
span into the manifest, and the shared rule.
"""
from __future__ import annotations

import pytest

from nexus.catalog.manifest_backfill import _fetch_chunk_by_id, _fetch_chunks_by_key
from nexus.catalog.note_write import note_manifest_rows
from nexus.mcp_infra import _manifest_chunk_rows
from nexus.metadata_schema import make_chunk_metadata

X = "a" * 64
Y = "b" * 64
_SPAN = ("line_start", "line_end", "char_start", "char_end")


def _span(row: dict) -> tuple:
    return tuple(row[k] for k in _SPAN)


def _factory_meta(**kw) -> dict:
    """A chunk's metadata exactly as an indexer writes it (the factory's defaults and all)."""
    return make_chunk_metadata(
        content_type="markdown", chunk_text_hash=X, content_hash=X,
        indexed_at="2026-11-02T00:00:00+00:00", embedding_model="m", **kw,
    )


# ── the two row producers ────────────────────────────────────────────────────

def test_zero_start_chunk_round_trips_as_zero_through_the_mcp_row_producer() -> None:
    meta = {"chunk_text_hash": X, "chunk_index": 0, "chunk_start_char": 0, "chunk_end_char": 120}
    (row,) = _manifest_chunk_rows([(0, meta)])
    assert row["char_start"] == 0 and row["char_start"] is not None
    assert row["char_end"] == 120


def test_zero_start_chunk_round_trips_as_zero_through_the_note_row_producer() -> None:
    (row,) = note_manifest_rows([
        {"chunk_text_hash": X, "chunk_index": 0, "chunk_start_char": 0, "chunk_end_char": 7},
    ])
    assert row["char_start"] == 0 and row["char_start"] is not None
    assert row["char_end"] == 7


def test_markdown_chunk_keeps_its_zero_start_and_has_no_lines() -> None:
    """The real indexer shape: char span known, line fields left at the factory's 0."""
    meta = _factory_meta(chunk_start_char=0, chunk_end_char=300)
    (row,) = _manifest_chunk_rows([(0, meta)])
    assert _span(row) == (None, None, 0, 300)


def test_code_chunk_keeps_its_first_line_and_zero_start() -> None:
    meta = _factory_meta(chunk_start_char=0, chunk_end_char=90, line_start=1, line_end=4)
    (row,) = _manifest_chunk_rows([(0, meta)])
    assert _span(row) == (1, 4, 0, 90)


def test_a_chunk_with_no_recorded_span_stays_all_null() -> None:
    """Every field at the factory's 0: nothing was recorded, nothing is claimed."""
    (row,) = _manifest_chunk_rows([(0, _factory_meta())])
    assert _span(row) == (None, None, None, None)
    (bare,) = _manifest_chunk_rows([(0, {"chunk_text_hash": X})])
    assert _span(bare) == (None, None, None, None)


def test_zero_start_with_an_unknown_end_is_not_a_known_start() -> None:
    (row,) = _manifest_chunk_rows([(0, {"chunk_text_hash": X, "chunk_start_char": 0, "chunk_end_char": 0})])
    assert _span(row) == (None, None, None, None)


def test_nonzero_values_pass_through_unchanged() -> None:
    meta = {"chunk_text_hash": X, "line_start": 5, "line_end": 9, "chunk_start_char": 40, "chunk_end_char": 99}
    (row,) = _manifest_chunk_rows([(0, meta)])
    assert _span(row) == (5, 9, 40, 99)
    (note,) = note_manifest_rows([meta])
    assert _span(note) == (5, 9, 40, 99)


# ── the backfill readers: a T3 chunk's span copied into the manifest ─────────

class _Col:
    def __init__(self, metas: dict[str, dict]) -> None:
        self._metas = metas

    def get(self, **kw):
        ids = kw.get("ids") or list(self._metas)
        return {"ids": ids, "metadatas": [self._metas[i] for i in ids]}


def test_backfill_by_key_keeps_a_real_zero_start_and_drops_the_unrecorded_zeros() -> None:
    col = _Col({X: {"chunk_text_hash": X, "chunk_index": 0, "chunk_start_char": 0,
                    "chunk_end_char": 50, "line_start": 0, "line_end": 0}})
    (row,) = _fetch_chunks_by_key(col, "1.1.1", "docs__c", "catalog_doc_id")
    assert _span(row) == (None, None, 0, 50)


def test_backfill_by_key_with_nothing_recorded_writes_nulls_not_zeros() -> None:
    col = _Col({X: {"chunk_text_hash": X, "chunk_index": 0, "chunk_start_char": 0,
                    "chunk_end_char": 0, "line_start": 0, "line_end": 0}})
    (row,) = _fetch_chunks_by_key(col, "1.1.1", "docs__c", "catalog_doc_id")
    assert _span(row) == (None, None, None, None)


def test_backfill_by_id_follows_the_same_rule() -> None:
    col = _Col({X: {"chunk_text_hash": X, "chunk_start_char": 0, "chunk_end_char": 12,
                    "line_start": 0, "line_end": 0}})
    row = _fetch_chunk_by_id(col, X, "1.1.1", "knowledge__c")
    assert _span(row) == (None, None, 0, 12)


# ── the shared rule ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ((None, None, None, None), (None, None, None, None)),
        ((0, 0, 0, 0), (None, None, None, None)),
        ((1, 1, 0, 1), (1, 1, 0, 1)),
        ((0, 0, 0, 5), (None, None, 0, 5)),
        ((0, 0, 7, 0), (None, None, 7, None)),
        ((3, 4, None, None), (3, 4, None, None)),
        ((0, 0, None, 5), (None, None, None, 5)),
    ],
)
def test_manifest_span_rule(given, expected) -> None:
    from nexus.catalog.types import manifest_span

    assert tuple(manifest_span(*given)[k] for k in _SPAN) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (("12", "13", "5", "9"), (None, None, None, None)),
        (("abc", None, "0", 7), (None, None, None, 7)),
        ((True, True, True, True), (None, None, None, None)),
        ((False, False, False, 5), (None, None, None, 5)),
        ((3, 4, True, 9), (3, 4, None, 9)),
        ((1.5, 2.0, 3.5, 4.5), (None, None, None, None)),
    ],
)
def test_manifest_span_reads_a_non_int_or_a_bool_as_not_recorded(given, expected) -> None:
    """A T3 row backfilled from elsewhere can carry a string span (``"12"``) or a bool; neither is a
    number the engine could store, and neither may raise out of the comparison."""
    from nexus.catalog.types import manifest_span

    assert tuple(manifest_span(*given)[k] for k in _SPAN) == expected

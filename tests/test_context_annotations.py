# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-onn7s: per-result context annotations, fixed clock."""
from __future__ import annotations

from datetime import UTC, datetime

from nexus.context_annotations import READER_INSTRUCTION, annotate, annotation_line

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def test_indexed_age_and_published_year():
    tokens = annotate({"indexed_at": "2026-08-27T17:49:07+00:00", "bib_year": 2026}, now=NOW)
    assert tokens == ["indexed 2026-08-27 (11d ago)", "published 2026"]


def test_naive_and_zulu_stamps_are_read_as_utc():
    assert annotate({"indexed_at": "2026-09-01T00:00:00"}, now=NOW) == ["indexed 2026-09-01 (7d ago)"]
    assert annotate({"indexed_at": "2026-09-01T00:00:00Z"}, now=NOW) == ["indexed 2026-09-01 (7d ago)"]


def test_ttl_reads_as_remaining_or_expired_and_permanent_says_nothing():
    base = {"indexed_at": "2026-09-01T00:00:00+00:00"}
    assert "expires in 23d" in annotate({**base, "ttl_days": 30}, now=NOW)
    assert "expired" in annotate({**base, "ttl_days": 3}, now=NOW)
    assert not any(t.startswith("expire") for t in annotate({**base, "ttl_days": 0}, now=NOW))
    assert not any(t.startswith("expire") for t in annotate(base, now=NOW))


def test_index_state_is_named_only_when_not_complete():
    meta = {"indexed_at": "2026-09-01T00:00:00+00:00"}
    assert "index_state: failed" in annotate(meta, now=NOW, index_state="failed")
    assert not any("index_state" in t for t in annotate(meta, now=NOW, index_state="complete"))
    assert not any("index_state" in t for t in annotate(meta, now=NOW, index_state=None))


def test_catalog_year_fills_in_when_the_chunk_has_no_bib_year():
    assert annotate({}, now=NOW, year=2019) == ["published 2019"]


def test_garbage_is_silent_not_fatal():
    assert annotate({"indexed_at": "not a date", "bib_year": "n/a", "ttl_days": "x"}, now=NOW) == []
    assert annotation_line({}) == ""


def test_future_stamp_clamps_to_zero_days():
    assert annotate({"indexed_at": "2026-09-09T00:00:00+00:00"}, now=NOW) == ["indexed 2026-09-09 (0d ago)"]


def test_contradiction_flag_is_not_repeated_here():
    assert annotate({"_contradiction_flag": True}, now=NOW) == []


def test_reader_instruction_names_the_rule():
    assert "newer" in READER_INSTRUCTION and "cite" in READER_INSTRUCTION
    # it must not claim a ranking signal the default configuration does not apply
    assert "boost" not in READER_INSTRUCTION.lower() and "ranked" not in READER_INSTRUCTION.lower()
    assert "not when it was written" in READER_INSTRUCTION


# ── nexus-onn7s, nx_answer half: source notes on hydrated chunks ────────────


def test_source_note_names_document_collection_and_dates():
    """Neutral model token on purpose (RDR-109 mode lint): a
    collection-NAME string passed straight through to source_note's
    string-formatting logic; no embedder."""
    from nexus.context_annotations import source_note

    collection = "knowledge__vector-search__model-ctx__v1"
    note = source_note(
        {"title": "ctxnote", "indexed_at": "2026-08-27T17:49:07+00:00", "bib_year": 2026},
        collection=collection, now=NOW,
    )
    assert note == (
        f"ctxnote · {collection} · "
        "indexed 2026-08-27 (11d ago) · published 2026"
    )


def test_source_note_falls_back_to_path_and_is_empty_when_nothing_is_known():
    from nexus.context_annotations import source_note

    assert source_note({"_display_path": "docs/a.md"}, now=NOW) == "docs/a.md"
    assert source_note({"source_path": "src/x.py"}, collection="code__x", now=NOW) == "src/x.py · code__x"
    assert source_note({}, now=NOW) == ""


def test_with_source_note_prefixes_only_when_both_are_present():
    from nexus.context_annotations import with_source_note

    assert with_source_note("body", "n") == "[source: n]\nbody"
    assert with_source_note("body", "") == "body"
    assert with_source_note("", "n") == ""


def test_source_instruction_names_the_marker_and_the_rule():
    from nexus.context_annotations import SOURCE_INSTRUCTION

    assert "[source: ...]" in SOURCE_INSTRUCTION
    assert "never quote it as support" in SOURCE_INSTRUCTION
    assert "not when it was written" in SOURCE_INSTRUCTION
    assert "more recently published" in SOURCE_INSTRUCTION and "cite" in SOURCE_INSTRUCTION
    # no authority claim the line cannot back, no ranking claim
    assert "record of record" not in SOURCE_INSTRUCTION and "boost" not in SOURCE_INSTRUCTION.lower()


def test_source_clause_is_empty_without_the_marker():
    from nexus.context_annotations import SOURCE_INSTRUCTION, source_clause

    assert source_clause("plain body") == ""
    assert source_clause("") == ""
    assert source_clause("[source: Doc A]\nbody") == f"\n\n{SOURCE_INSTRUCTION}"

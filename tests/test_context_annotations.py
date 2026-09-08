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

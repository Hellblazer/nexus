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


def test_source_note_falls_back_to_the_owners_path_and_is_empty_when_nothing_is_known():
    from nexus.context_annotations import source_note

    assert source_note({"file_path": "docs/a.md"}, now=NOW) == "docs/a.md"
    assert source_note({"file_path": "src/x.py"}, collection="code__x", now=NOW) == "src/x.py · code__x"
    assert source_note({}, now=NOW) == ""


def test_source_note_does_not_read_chunk_row_path_keys():
    """nexus-6pfgb.11: the facts are the owners', so a chunk row's own path keys name nothing."""
    from nexus.context_annotations import source_note

    assert source_note({"_display_path": "docs/a.md", "source_path": "src/x.py"}, now=NOW) == ""


def _entry(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_entry_facts_reads_only_what_the_entry_states():
    from nexus.context_annotations import entry_facts

    full = _entry(title="T", file_path="a.md", indexed_at="2026-08-27T17:49:07+00:00",
                  bib_year=2020, year=1999, meta={"ttl_days": "30"})
    assert entry_facts(full) == {"title": "T", "file_path": "a.md", "indexed_at": "2026-08-27T17:49:07+00:00",
                                 "bib_year": 2020, "ttl_days": 30}
    # The catalog year fills in for an unenriched document; no meta ttl_days is no expiry.
    assert entry_facts(_entry(title="", year=1999, bib_year=0, meta={})) == {"bib_year": 1999}
    assert entry_facts(_entry()) == {}
    assert entry_facts(_entry(meta={"ttl_days": "n/a"})) == {}


def test_agreed_facts_keeps_a_field_only_when_every_owner_states_it_alike():
    from nexus.context_annotations import agreed_facts

    a = {"title": "Same", "indexed_at": "2026-08-27T17:49:07+00:00", "bib_year": 2020, "ttl_days": 30}
    b = {"title": "Same", "indexed_at": "2026-08-27T17:49:07Z", "bib_year": 2021, "ttl_days": 30}
    # One spelling of one instant agrees; a different year disagrees.
    assert agreed_facts([a, b]) == {
        "title": "Same", "indexed_at": a["indexed_at"], "ttl_days": 30,
    }
    assert agreed_facts([a, {"title": "Same"}]) == {"title": "Same"}  # a lacking owner drops the field
    assert agreed_facts([a]) == a
    assert agreed_facts([]) == {}


def test_agreed_facts_drops_an_expiry_that_has_no_agreed_stamp_to_count_from():
    from nexus.context_annotations import agreed_facts

    a = {"indexed_at": "2026-08-27T00:00:00+00:00", "ttl_days": 30}
    b = {"indexed_at": "2026-09-01T00:00:00+00:00", "ttl_days": 30}
    assert agreed_facts([a, b]) == {}


def test_note_facts_names_the_owners_titles_where_they_differ_and_drops_their_dates():
    from nexus.context_annotations import note_facts

    a = {"title": "Doc A", "indexed_at": "2026-08-01T00:00:00+00:00", "bib_year": 2020}
    b = {"title": "Doc B", "indexed_at": "2026-09-01T00:00:00+00:00", "bib_year": 2020}
    assert note_facts([a, b]) == {"title": '"Doc A", "Doc B"', "bib_year": 2020}
    assert note_facts([{"bib_year": 2020}, {"bib_year": 2020}]) == {"bib_year": 2020}
    assert note_facts([a]) == a
    assert note_facts([]) == {}


def test_note_facts_counts_an_untitled_owner_instead_of_dropping_it():
    """An owner with no title is a document the line must not hide: ``"Doc A"`` alone would
    read as one document."""
    from nexus.context_annotations import note_facts

    a = {"title": "Doc A", "bib_year": 2020}
    assert note_facts([a, {"bib_year": 2020}])["title"] == '"Doc A" (1 untitled)'
    assert note_facts([a, {}, {}])["title"] == '"Doc A" (2 untitled)'
    many = [{"title": f"D{i}"} for i in range(1, 5)] + [{}]
    assert note_facts(many)["title"] == '"D1", "D2", "D3" (+1 more, 1 untitled)'


def test_note_facts_titles_stay_apart_when_a_title_holds_the_note_separator():
    """A title that contains " · " or a quote cannot read as two titles."""
    from nexus.context_annotations import note_facts

    out = note_facts([{"title": "Part A · Part B"}, {"title": 'The "Odd" One'}])["title"]
    assert out == '"Part A · Part B", "The \\"Odd\\" One"'
    assert out.count('", "') == 1


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
    # a line does not promise a document and dates every time: it names titles when known and
    # only the dates the owning documents agree on
    assert "naming the document" not in SOURCE_INSTRUCTION
    assert "agree on" in SOURCE_INSTRUCTION and "left out" in SOURCE_INSTRUCTION
    # no authority claim the line cannot back, no ranking claim
    assert "record of record" not in SOURCE_INSTRUCTION and "boost" not in SOURCE_INSTRUCTION.lower()


def test_source_clause_is_empty_without_the_marker():
    from nexus.context_annotations import SOURCE_INSTRUCTION, source_clause

    assert source_clause("plain body") == ""
    assert source_clause("") == ""
    assert source_clause("[source: Doc A]\nbody") == f"\n\n{SOURCE_INSTRUCTION}"

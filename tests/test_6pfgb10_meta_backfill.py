# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.10 (RDR-236 Phase 1): the per-key rules of the fill-only catalog-meta backfill, on fakes.

The real-engine journey is ``tests/integration/test_6pfgb10_backfill_meta.py``. These pin the rule table:
which row may give a note its TTL (the writer's, stamp AND title), which may give tags (a sole owner's),
that an entry's presence of a key (retraction values included) is final, that an array is what a legacy
string becomes only in the .38 rung (never here), and the guards an indexed document's copy needs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.catalog import meta_backfill as mb
from nexus.catalog.meta_backfill import PRESENT, SHARED, STAMPED
from nexus.cli import main
from nexus.commands.enrich import _chunk_bib, _chunk_idless_bib, _idless_fill

PUT = "2026-10-01T12:00:00+00:00"          # a note's put, and the row stamped with it
LATER = "2026-10-01T12:03:00+00:00"        # 3 minutes on: inside the five-minute skew
MUCH_LATER = "2026-10-01T13:00:00+00:00"   # an hour on: someone else's write


def _entry(meta=None, *, title="note", tumbler="1.2.3", indexed_at=PUT, content_type="knowledge",
           file_path="", author="", collection="knowledge__x__minilm__v1"):
    return SimpleNamespace(
        tumbler=tumbler, title=title, indexed_at=indexed_at, content_type=content_type, file_path=file_path,
        author=author, meta=meta, physical_collection=collection, alias_of="",
    )


def _row(**kw):
    return {"indexed_at": PUT, **kw}


MINE = {"catalog_doc_id": "1.2.3", "title": "note"}
OTHERS = {"catalog_doc_id": "1.2.9", "title": "other"}


# ── notes ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_keyless_note_takes_ttl_put_at_and_tags_as_an_array_from_the_row_it_wrote():
    row = _row(**MINE, tags="alpha, beta", category="design", ttl_days=7, source_agent="agent-a")
    plan = mb.plan_note(_entry({}), [("c1", row)], sole=[])
    assert plan.meta == {"put_at": PUT, "ttl_days": 7, "tags": ["alpha", "beta"], "category": "design",
                         "source_agent": "agent-a"}
    assert set(plan.outcome.values()) == {STAMPED}


def test_a_permanent_note_it_wrote_leaves_the_keyless_count_with_ttl_zero_and_sends_no_empty_key():
    plan = mb.plan_note(_entry(None), [("c1", _row(**MINE, tags="", category="", ttl_days=None))], sole=[])
    assert plan.meta == {"put_at": PUT, "ttl_days": 0}
    assert plan.outcome["tags"] == "residue:empty" and plan.outcome["category"] == "residue:empty"


def test_a_row_another_note_wrote_gives_nothing_and_a_departed_co_owners_ttl_never_lands():
    row = _row(**OTHERS, tags="theirs", category="theirs", ttl_days=30)
    plan = mb.plan_note(_entry({}), [("c1", row)], sole=["c1"])
    assert plan.meta == {} and plan.outcome["put_at"] == SHARED


def test_a_sole_owner_recovers_tags_category_and_a_put_at_but_never_the_rows_ttl():
    """An unstamped legacy row, or a renamed note's row, names nobody: row_written_by refuses it for the
    TTL, row_may_give_tags allows it for tags on sole ownership (Sam's option B)."""
    row = _row(tags="legacy", category="finding", ttl_days=30, source_agent="old")
    plan = mb.plan_note(_entry({}), [("c1", row)], sole=["c1"])
    assert plan.meta == {"put_at": PUT, "ttl_days": 0, "tags": ["legacy"], "category": "finding", "source_agent": "old"}


def test_a_shared_row_without_sole_ownership_is_skipped_and_counted_shared_not_residue():
    plan = mb.plan_note(_entry({}), [("c1", _row(tags="legacy"))], sole=[])
    assert plan.meta == {} and set(plan.outcome.values()) == {SHARED}


def test_a_note_with_no_rows_at_all_is_residue_per_key():
    plan = mb.plan_note(_entry({}), [("c1", None)], sole=[])
    assert set(plan.outcome.values()) == {"residue:no_row"}


def test_a_row_written_after_the_note_is_not_copied():
    plan = mb.plan_note(_entry({}), [("c1", _row(**MINE, ttl_days=7, indexed_at=MUCH_LATER))], sole=[])
    assert plan.meta == {} and plan.outcome["ttl_days"] == "residue:row_newer"
    inside = mb.plan_note(_entry({}), [("c1", _row(**MINE, ttl_days=7, indexed_at=LATER))], sole=[])
    assert inside.meta["ttl_days"] == 7, "three minutes after the entry is inside the five-minute skew"


def test_a_row_with_no_clock_cannot_be_attributed():
    plan = mb.plan_note(_entry({}), [("c1", {**MINE, "ttl_days": 7})], sole=[])
    assert plan.meta == {} and plan.outcome["put_at"] == "residue:no_indexed_at"


@pytest.mark.parametrize("held", [{"put_at": PUT}, {"ttl_days": 0, "put_at": ""}, {"tags": [], "put_at": PUT}])
def test_a_retracted_key_is_a_present_key_and_is_never_refilled_from_a_stale_row(held):
    row = _row(**MINE, tags="stale", category="stale", ttl_days=30)
    plan = mb.plan_note(_entry(dict(held)), [("c1", row)], sole=["c1"])
    if "put_at" in held:
        assert plan.meta == {} and set(plan.outcome.values()) == {PRESENT}


def test_an_entry_holding_ttl_but_no_put_at_gets_only_put_at_and_only_from_a_row_with_the_same_ttl():
    entry = _entry({"ttl_days": 7, "tags": ["kept"]})
    same = mb.plan_note(entry, [("c1", _row(**MINE, ttl_days=7, tags="other"))], sole=[])
    assert same.meta == {"put_at": PUT}, "tags and ttl_days are the entry's; an absent key there is unset"
    differs = mb.plan_note(entry, [("c1", _row(**MINE, ttl_days=30))], sole=[])
    assert differs.meta == {} and differs.outcome["put_at"] == "residue:row_ttl_differs"
    not_written = mb.plan_note(entry, [("c1", _row(ttl_days=7))], sole=["c1"])
    assert not_written.outcome["put_at"] == "residue:no_written_row"


def test_a_legacy_tags_string_is_left_alone_and_counted_for_the_array_rung():
    plan = mb.plan_note(_entry({"tags": "a, b", "put_at": PUT}), [], sole=[])
    assert plan.meta == {} and plan.outcome["tags_legacy_string"] == PRESENT
    assert "tags" not in plan.meta, "this verb never converts an existing string (nexus-6pfgb.38 owns that)"


def test_ownership_is_asked_only_for_a_note_no_written_row_decides():
    keyless = _entry({})
    assert mb.note_needs_ownership(keyless, [("c1", _row(**MINE))]) == []
    assert mb.note_needs_ownership(keyless, [("c1", _row(tags="x")), ("c2", _row(**OTHERS)), ("c3", None)]) == ["c1"]
    assert mb.note_needs_ownership(_entry({"put_at": PUT}), [("c1", _row(tags="x"))]) == []


# ── indexed documents ──────────────────────────────────────────────────────────────────────────────

def _doc(meta=None, **kw):
    return _entry(meta if meta is not None else {}, content_type="paper", indexed_at=PUT, **kw)


def test_extraction_keys_are_copied_from_agreeing_single_owner_rows_and_the_override_only_where_true():
    rows = [_row(extraction_method="mineru", extraction_source="file", quality_gate_overridden=True)] * 3
    plan = mb.plan_indexed_rows(_doc(), rows, n_chunks=3)
    assert plan.meta["extraction_method"] == "mineru" and plan.meta["extraction_source"] == "file"
    assert plan.meta["quality_gate_overridden"] is True


@pytest.mark.parametrize("row_value", [False, None, "missing"])
def test_the_override_is_never_written_false_from_a_false_or_missing_key(row_value):
    row = _row(extraction_method="mineru")
    if row_value != "missing":
        row["quality_gate_overridden"] = row_value
    plan = mb.plan_indexed_rows(_doc(), [row], n_chunks=1)
    assert "quality_gate_overridden" not in plan.meta
    assert plan.outcome["quality_gate_overridden"] == "residue:empty"


def test_rows_that_disagree_make_the_key_residue_not_a_guess():
    rows = [_row(extraction_method="mineru"), _row(extraction_method="pymupdf")]
    plan = mb.plan_indexed_rows(_doc(), rows, n_chunks=2)
    assert "extraction_method" not in plan.meta and plan.outcome["extraction_method"] == "residue:disagree"


def test_a_document_whose_chunks_are_all_shared_is_skipped_and_counted_shared():
    plan = mb.plan_indexed_rows(_doc(), [], n_chunks=4)
    assert plan.meta == {} and plan.author == ""
    assert {plan.outcome[k] for k in ("extraction_method", "frecency_score", "author")} == {SHARED}
    empty = mb.plan_indexed_rows(_doc(), [], n_chunks=0)
    assert empty.outcome["extraction_method"] == "residue:no_chunks"


def test_a_key_the_entry_holds_is_present_and_a_non_empty_author_is_never_replaced():
    rows = [_row(extraction_method="new", source_author="Someone")]
    held = {"extraction_method": "", "extraction_source": "file", "quality_gate_overridden": False, "frecency_score": 0.0}
    plan = mb.plan_indexed_rows(_doc(held, author="Curated"), rows, n_chunks=1)
    assert plan.meta == {} and plan.author == ""
    assert set(plan.outcome.values()) == {PRESENT}


@pytest.mark.parametrize("raw", ["None", "['Ann Lee', 'Bo Chen']", "[]x", None])
def test_a_source_author_that_is_not_a_plain_string_is_skipped(raw):
    """The row writer ``str()``-ed the frontmatter value: a list repr or "None" is not an author."""
    plan = mb.plan_indexed_rows(_doc(), [_row(source_author=raw)], n_chunks=1)
    assert plan.author == "" and plan.outcome["author"] == ("residue:empty" if raw is None else "residue:author_not_plain")


def test_extraction_source_is_stamped_only_from_a_value_never_from_absence():
    absent = mb.plan_indexed_rows(_doc(), [_row(extraction_method="mineru")], n_chunks=1)
    assert "extraction_source" not in absent.meta and absent.outcome["extraction_source"] == "residue:empty"
    present = mb.plan_indexed_rows(_doc(), [_row(extraction_source="dt_content")], n_chunks=1)
    assert present.meta["extraction_source"] == "dt_content"


def test_an_empty_author_column_is_filled_from_source_author():
    plan = mb.plan_indexed_rows(_doc(), [_row(source_author=" Ann Lee ")], n_chunks=1)
    assert plan.author == "Ann Lee"


def test_frecency_is_stamped_with_a_computed_at_of_the_rows_and_zero_is_not_a_score():
    plan = mb.plan_indexed_rows(_doc(), [_row(frecency_score=0.5), _row(frecency_score=0.5, indexed_at=LATER)], n_chunks=2)
    assert plan.meta["frecency_score"] == 0.5 and plan.meta["frecency_computed_at"] == LATER
    zero = mb.plan_indexed_rows(_doc(), [_row(frecency_score=0.0)], n_chunks=1)
    assert "frecency_score" not in zero.meta and zero.outcome["frecency_score"] == "residue:empty"


def test_a_row_written_after_the_document_is_dropped_and_no_clock_means_no_attribution():
    late = mb.plan_indexed_rows(_doc(), [_row(extraction_method="x", indexed_at=MUCH_LATER)], n_chunks=1)
    assert late.meta == {} and late.outcome["extraction_method"] == "residue:row_newer"
    mixed = mb.plan_indexed_rows(
        _doc(), [_row(extraction_method="x"), _row(extraction_method="y", indexed_at=MUCH_LATER)], n_chunks=2)
    assert mixed.meta["extraction_method"] == "x", "the later row is another writer's and is not evidence"
    clockless = mb.plan_indexed_rows(_doc(), [{"extraction_method": "x"}], n_chunks=1)
    assert clockless.outcome["extraction_method"] == "residue:no_indexed_at"


# ── the run: counts, writes, non-vacuity, failure isolation ────────────────────────────────────────

@dataclass
class _Manifest:
    chash: str
    collection: str | None = None


@dataclass
class _World:
    entries: list = field(default_factory=list)
    manifests: dict = field(default_factory=dict)
    owners: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)       # (collection, chash) -> row
    updates: list = field(default_factory=list)
    fail_owners: bool = False
    fail_rows_for: set = field(default_factory=set)

    def all_documents(self):
        return list(self.entries)

    def get_manifests(self, ids):
        return {i: self.manifests[i] for i in ids if i in self.manifests}

    def chash_positions(self, chashes):
        if self.fail_owners:
            raise RuntimeError("owner lookup down")
        return [{"chash": c, "doc_id": d, "position": 0, "chunk_count": 1} for c in chashes for d in self.owners.get(c, [])]

    def chunk_rows(self, collection, chashes):
        if collection in self.fail_rows_for:
            raise RuntimeError("store-get down")
        return {c: self.rows[(collection, c)] for c in chashes if (collection, c) in self.rows}

    def update_many(self, payloads):
        self.updates.extend(payloads)
        return [1] * len(payloads)


def _world_with_one_of_each():
    w = _World()
    note = _entry({}, title="a-note", tumbler="1.1.1")
    solo = _entry({}, title="solo", tumbler="1.1.2", content_type="paper", file_path="p.pdf", collection="docs__x__m__v1")
    shared = _entry({}, title="shared", tumbler="1.1.3", content_type="paper", file_path="q.pdf", collection="docs__x__m__v1")
    other = _entry({}, title="other", tumbler="1.1.4", content_type="paper", file_path="r.pdf", collection="docs__x__m__v1")
    w.entries = [note, solo, shared, other]
    w.manifests = {
        "1.1.1": [_Manifest("n1")], "1.1.2": [_Manifest("s1")], "1.1.3": [_Manifest("x1")], "1.1.4": [_Manifest("x1")],
    }
    w.owners = {"n1": ["1.1.1"], "s1": ["1.1.2"], "x1": ["1.1.3", "1.1.4"]}
    c = note.physical_collection
    w.rows[(c, "n1")] = _row(catalog_doc_id="1.1.1", title="a-note", tags="t", ttl_days=3)
    w.rows[("docs__x__m__v1", "s1")] = _row(extraction_method="mineru", source_author="Ann")
    w.rows[("docs__x__m__v1", "x1")] = _row(extraction_method="pymupdf")
    return w


def test_a_dry_run_counts_per_key_and_writes_nothing_then_apply_writes_and_a_second_run_stamps_nothing():
    w = _world_with_one_of_each()
    dry = mb.backfill_meta(w, w, None, apply=False)
    assert w.updates == [] and dry.documents_examined == 4 and dry.documents_to_stamp == 4, \
        "the note, the solo paper, and the two sharers (nexus-6pfgb.23: source_format needs no row)"
    ext = dry.keys["doc.extraction_method"]
    assert (ext.examined, ext.stamped, ext.shared) == (3, 1, 2), "the solo doc stamps, the two sharers are skipped"
    assert dry.documents_all_shared == 2
    assert dry.keys["note.ttl_days"].stamped == 1 and dry.keys["note.tags"].stamped == 1

    first = mb.backfill_meta(w, w, w, apply=True)
    by_tumbler = {u["tumbler"]: u for u in w.updates}
    assert by_tumbler["1.1.1"]["meta"] == {"put_at": PUT, "ttl_days": 3, "tags": ["t"]}
    assert by_tumbler["1.1.2"]["meta"]["extraction_method"] == "mineru" and by_tumbler["1.1.2"]["author"] == "Ann"
    assert "tags" not in by_tumbler["1.1.2"]["meta"] and "category" not in by_tumbler["1.1.2"]["meta"], \
        "tags and category of an indexed document are not backfilled: the register-time stamp and the next index run fill them"
    assert by_tumbler["1.1.2"]["meta"]["source_format"] == "pdf"
    assert by_tumbler["1.1.3"]["meta"] == {"source_format": "pdf"} and by_tumbler["1.1.4"]["meta"] == {"source_format": "pdf"}, \
        "a chunk two documents hold decides no row-sourced key, but source_format is derived from the entry, not the chunk"
    assert first.documents_written == 4 and first.writes_failed == 0

    # the catalog now holds what was written; a second run finds nothing to stamp
    for e in w.entries:
        written = by_tumbler.get(str(e.tumbler), {})
        e.meta = dict(written.get("meta", {}))
        e.author = written.get("author", e.author)
    w.updates.clear()
    second = mb.backfill_meta(w, w, w, apply=True)
    assert w.updates == [] and sum(v.stamped for v in second.keys.values()) == 0


def test_an_empty_catalog_fails_the_run_naming_why():
    w = _World()
    with pytest.raises(mb.VacuousRunError, match="examined 0 documents"):
        mb.backfill_meta(w, w, None, apply=False)


def test_a_failed_owner_lookup_withholds_the_keys_counts_the_documents_and_never_fails_the_run():
    w = _world_with_one_of_each()
    w.fail_owners = True
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert rep.lookups_failed == 3, "the note's own written row needs no ownership; the three documents do"
    ext_written = [u for u in w.updates if "extraction_method" in u["meta"]]
    assert ext_written == []
    assert [u["meta"] for u in w.updates if u["tumbler"] == "1.1.2"] == [{"source_format": "pdf"}], \
        "an indexed document's row-sourced keys are withheld; its derived source_format needs no lookup"


def test_a_failed_row_read_for_one_collection_isolates_its_documents():
    w = _world_with_one_of_each()
    w.fail_rows_for = {"docs__x__m__v1"}
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert rep.lookups_failed == 1, "only the solo document had a sole-owner row to read; the sharers need no read"
    assert any(u["tumbler"] == "1.1.1" and u["meta"]["ttl_days"] == 3 for u in w.updates), "the note's collection read fine"


def test_a_write_the_engine_did_not_count_is_reported_failed():
    w = _world_with_one_of_each()
    w.update_many = lambda payloads: [0] * len(payloads)  # type: ignore[method-assign]
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert rep.writes_failed == rep.documents_to_stamp and rep.documents_written == 0


def test_a_chunk_two_collections_hold_is_a_different_row_so_a_same_text_owner_elsewhere_does_not_share_it():
    w = _world_with_one_of_each()
    elsewhere = _entry({}, title="elsewhere", tumbler="1.1.5", content_type="paper", collection="docs__y__m__v1")
    w.entries.append(elsewhere)
    w.owners["s1"] = ["1.1.2", "1.1.5"]   # the same text, in another collection
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert any(u["tumbler"] == "1.1.2" and u["meta"].get("extraction_method") == "mineru" for u in w.updates)
    assert rep.documents_examined == 5


# ── the command ────────────────────────────────────────────────────────────────────────────────────

def _invoke(monkeypatch, world, *args):
    closed = []
    world.close = lambda: closed.append(True)  # type: ignore[attr-defined]
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: world)
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: world)
    monkeypatch.setattr("nexus.db.make_t3", lambda **_k: world)
    result = CliRunner().invoke(main, ["catalog", "backfill-meta", *args])
    return result, closed


def test_the_command_dry_run_prints_per_key_counts_and_writes_nothing(monkeypatch):
    w = _world_with_one_of_each()
    result, _ = _invoke(monkeypatch, w)
    assert result.exit_code == 0, result.output
    assert "Examined 4 documents (1 notes)" in result.output
    assert "doc.extraction_method: examined 3, held 0, skipped (shared) 2, would stamp 1, residue 0" in result.output
    assert "Dry run: nothing written" in result.output
    assert w.updates == []


def test_the_command_apply_writes_and_json_carries_the_reasons(monkeypatch):
    w = _world_with_one_of_each()
    result, closed = _invoke(monkeypatch, w, "--apply", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["applied"] is True and report["documents_written"] == 4 and closed == [True]
    assert report["keys"]["note.tags"]["stamped"] == 1
    assert "residue_reasons" in report["keys"]["doc.quality_gate_overridden"]
    assert len(w.updates) == 4


def test_the_json_report_names_the_source_format_residue_by_reason(monkeypatch):
    """nexus-6pfgb.23: a document the rule derives nothing for is residue, ``no_rule`` where it has a path and
    ``no_file_path`` where it has none; the counts sit under ``keys["doc.source_format"]``."""
    w = _World()
    entries = [
        _entry({}, title="pdf", tumbler="1.1.1", content_type="paper", file_path="p.pdf", collection="docs__x__m__v1"),
        _entry({}, title="txt", tumbler="1.1.2", content_type="docs", file_path="a.txt", collection="docs__x__m__v1"),
        _entry({}, title="log", tumbler="1.1.3", content_type="docs", file_path="b.log", collection="docs__x__m__v1"),
        _entry({}, title="nopath", tumbler="1.1.4", content_type="docs", file_path="", collection="docs__x__m__v1"),
        _entry({"source_format": "pdf"}, title="held", tumbler="1.1.5", content_type="paper", file_path="h.pdf",
               collection="docs__x__m__v1"),
    ]
    w.entries = entries
    w.manifests = {str(e.tumbler): [_Manifest(f"c{i}")] for i, e in enumerate(entries)}
    w.owners = {f"c{i}": [str(e.tumbler)] for i, e in enumerate(entries)}

    result, _ = _invoke(monkeypatch, w, "--json")

    assert result.exit_code == 0, result.output
    key = json.loads(result.stdout)["keys"]["doc.source_format"]
    assert key == {
        "examined": 5, "present": 1, "skipped_shared": 0, "stamped": 1, "residue": 3,
        "residue_reasons": {"no_rule": 2, "no_file_path": 1},
    }


def test_the_command_exits_non_zero_naming_the_vacuous_run(monkeypatch):
    result, _ = _invoke(monkeypatch, _World())
    assert result.exit_code != 0 and "examined 0 documents" in result.output


def test_the_command_exits_non_zero_when_a_write_is_not_counted(monkeypatch):
    w = _world_with_one_of_each()
    w.update_many = lambda payloads: [0] * len(payloads)  # type: ignore[method-assign]
    result, _ = _invoke(monkeypatch, w, "--apply")
    assert result.exit_code == 1


# ── nexus-6pfgb.28: the id-less bib of `nx enrich bib --backfill-catalog` ──────────────────────────

def test_an_idless_bib_is_read_from_year_authors_venue_and_citations_only():
    row = {"bib_year": 2019, "bib_authors": "Dana", "bib_venue": "OSDI", "bib_citation_count": 31}
    assert _chunk_idless_bib(row) == {"year": 2019, "authors": "Dana", "venue": "OSDI", "citation_count": 31}
    assert _chunk_bib(row)["year"] == 2019 and "semantic_scholar_id" in _chunk_bib(row), "the id-bearing reader is unchanged"
    assert _chunk_idless_bib({"bib_year": 0, "bib_authors": "", "bib_venue": "", "bib_citation_count": 0}) is None
    assert _chunk_idless_bib({"title": "x"}) is None and _chunk_idless_bib(None) is None


def test_an_idless_bib_fills_only_the_columns_the_document_leaves_empty():
    bib = {"year": 2019, "authors": "Dana", "venue": "OSDI", "citation_count": 31}
    entry = SimpleNamespace(bib_year=0, bib_authors="", bib_venue="Curated", bib_citation_count=0)
    assert _idless_fill(entry, bib) == {"bib_year": 2019, "bib_authors": "Dana", "bib_citation_count": 31}
    full = SimpleNamespace(bib_year=2000, bib_authors="A", bib_venue="V", bib_citation_count=1)
    assert _idless_fill(full, bib) == {}


@pytest.mark.parametrize("ctype", ["prose", "rdr", "markdown", "pdf", "paper", "code"])
def test_no_content_type_is_left_out_of_the_row_sourced_keys(ctype):
    """`nx index repo` registers markdown and prose through the repo hook, which writes neither the author nor
    extraction_source: the selection must not be limited to the markdown or PDF hooks' content types."""
    w = _World()
    doc = _entry({}, title="d", tumbler="1.9.1", content_type=ctype, file_path="docs/a.md", collection="docs__x__m__v1")
    w.entries = [doc]
    w.manifests = {"1.9.1": [_Manifest("h1")]}
    w.owners = {"h1": ["1.9.1"]}
    w.rows[("docs__x__m__v1", "h1")] = _row(source_author="Ann Lee", extraction_source="dt_content")
    mb.backfill_meta(w, w, w, apply=True)
    fmt = "pdf" if ctype in ("pdf", "paper") else "markdown"   # nexus-6pfgb.23: the extension is read first
    assert w.updates == [{"tumbler": "1.9.1", "meta": {"extraction_source": "dt_content", "source_format": fmt}, "author": "Ann Lee"}]

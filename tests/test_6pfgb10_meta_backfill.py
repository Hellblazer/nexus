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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.catalog import meta_backfill as mb
from nexus.catalog import note_ttl
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


@pytest.mark.parametrize("stamp", ["", None, 5, True, {}, []])
def test_a_ttl_entry_whose_put_at_holds_no_stamp_is_filled_like_one_with_no_put_at(stamp):
    """The table CHECK on catalog_documents (nexus-6pfgb.44) reads a put_at of "" as absent, and so is any
    value that is not a non-empty string. An entry with a TTL and such a put_at is a violator; key presence
    would leave it for ever and the census would never read zero."""
    entry = _entry({"ttl_days": 7, "put_at": stamp})
    same = mb.plan_note(entry, [("c1", _row(**MINE, ttl_days=7))], sole=[])
    assert same.meta == {"put_at": PUT} and same.outcome["put_at"] == STAMPED
    differs = mb.plan_note(entry, [("c1", _row(**MINE, ttl_days=30))], sole=[])
    assert differs.meta == {} and differs.outcome["put_at"] == "residue:row_ttl_differs", "the same ttl rule as no put_at"


def test_a_digit_string_ttl_with_an_empty_put_at_is_filled_because_the_reader_and_the_check_count_it():
    plan = mb.plan_note(_entry({"ttl_days": "7", "put_at": ""}), [("c1", _row(**MINE, ttl_days=7))], sole=[])
    assert plan.meta == {"put_at": PUT}


def test_the_retraction_pair_keeps_its_empty_put_at_and_a_stamped_entry_is_untouched():
    """ttl_days 0 with put_at "" is what a re-put without a TTL writes (_NOTE_META_UNSET): it is not a
    violator, and it is final. A real stamp is final whatever the TTL."""
    row = _row(**MINE, tags="stale", category="stale", ttl_days=30)
    for held in ({"ttl_days": 0, "put_at": ""}, {"ttl_days": 7, "put_at": PUT}, {"put_at": ""}):
        plan = mb.plan_note(_entry(dict(held)), [("c1", row)], sole=["c1"])
        assert plan.meta == {} and set(plan.outcome.values()) == {PRESENT}, held


def test_a_run_stamps_a_ttl_entry_with_an_empty_put_at_and_a_second_run_finds_it_done():
    w = _World()
    violator = _entry({"ttl_days": 7, "put_at": ""}, title="v", tumbler="1.1.1")
    retraction = _entry({"ttl_days": 0, "put_at": ""}, title="r", tumbler="1.1.2")
    w.entries = [violator, retraction]
    w.manifests = {"1.1.1": [_Manifest("n1")], "1.1.2": [_Manifest("n2")]}
    w.owners = {"n1": ["1.1.1"], "n2": ["1.1.2"]}
    c = violator.physical_collection
    w.rows[(c, "n1")] = _row(catalog_doc_id="1.1.1", title="v", ttl_days=7)
    w.rows[(c, "n2")] = _row(catalog_doc_id="1.1.2", title="r", ttl_days=7)

    first = mb.backfill_meta(w, w, w, apply=True)

    assert [(u["tumbler"], u["meta"]) for u in w.updates] == [("1.1.1", {"put_at": PUT})], \
        "the retraction pair is final: it is not a violator and is not sent"
    assert first.keys["note.put_at"].stamped == 1
    violator.meta = {"ttl_days": 7, "put_at": PUT}
    w.updates.clear()
    second = mb.backfill_meta(w, w, w, apply=True)
    assert w.updates == [] and second.keys["note.put_at"].stamped == 0


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

    def chunk_rows(self, collection, chashes, *, sensitivity="include"):
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
    assert by_tumbler["1.1.2"]["meta"]["tags"] == ["pdf"] and by_tumbler["1.1.2"]["meta"]["category"] == "paper", \
        "nexus-9cstf: tags and category of an indexed document are derived from the entry, as source_format is"
    assert by_tumbler["1.1.2"]["meta"]["source_format"] == "pdf"
    derived = {"source_format": "pdf", "tags": ["pdf"], "category": "paper"}
    assert by_tumbler["1.1.3"]["meta"] == derived and by_tumbler["1.1.4"]["meta"] == derived, \
        "a chunk two documents hold decides no row-sourced key, but the derived keys come from the entry, not the chunk"
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
    assert [u["meta"] for u in w.updates if u["tumbler"] == "1.1.2"] == [
        {"source_format": "pdf", "tags": ["pdf"], "category": "paper"}], \
        "an indexed document's row-sourced keys are withheld; its derived keys need no lookup"


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


def test_the_text_report_says_which_documents_each_key_family_covers_and_why_residue_is_residue(monkeypatch):
    """7.94.0 shakedown (class H): a catalog of one note and one indexed document printed `Examined 2 documents`
    and then `examined 1` on every key line, which read as 'examined 1 of 2'. Each family counts only its own
    kind of document (note.* the notes, doc.* the indexed documents); the text now says so, and gives a key's
    residue reasons beside its count (they were --json only)."""
    w = _world_with_one_of_each()
    result, _ = _invoke(monkeypatch, w)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Examined 4 documents (1 notes)" in out
    assert "note.* keys cover the 1 note(s); doc.* keys cover the 3 non-note document(s)" in out
    json_result, _ = _invoke(monkeypatch, _world_with_one_of_each(), "--json")
    keys = json.loads(json_result.stdout)["keys"]
    with_residue = {k: v["residue_reasons"] for k, v in keys.items() if v["residue"]}
    assert with_residue, "control: the fixture has residue to explain"
    for key, reasons in with_residue.items():
        rendered = ", ".join(f"{r} {n}" for r, n in sorted(reasons.items()))
        line = next(line for line in out.splitlines() if line.strip().startswith(f"{key}:"))
        assert line.endswith(f"residue {sum(reasons.values())} ({rendered})"), line


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
    derived = {
        "prose": {"tags": ["markdown"], "category": "prose"}, "rdr": {"tags": ["markdown"], "category": "prose"},
        "pdf": {"tags": ["pdf"], "category": "prose"}, "paper": {},   # a paper at a .md path was not made by nx index pdf
        "code": {"tags": ["md"], "category": "code"}, "markdown": {},   # index_markdown's fallback can register it: no rule
    }[ctype]
    assert w.updates == [{
        "tumbler": "1.9.1", "meta": {"extraction_source": "dt_content", "source_format": fmt, **derived}, "author": "Ann Lee",
    }]


# ── nexus-9cstf: tags and category of an indexed document are derived from the entry ────────────

def _indexed(meta, content_type, file_path, tumbler="1.4.1", collection="docs__x__m__v1"):
    """An indexed document whose row-sourced keys are all held, so only the derived keys are open."""
    held = {"extraction_method": "m", "quality_gate_overridden": False, "extraction_source": "file", "frecency_score": 0.0}
    return _entry({**held, **meta}, title="d", tumbler=tumbler, content_type=content_type, file_path=file_path,
                  author="Ann", collection=collection)


@pytest.mark.parametrize("content_type,path,tags,category", [
    ("code", "src/a.py", ["py"], "code"),                  # the repo code indexer: the extension, lower-cased
    ("code", "src/A.PY", ["py"], "code"),
    ("prose", "docs/a.md", ["markdown"], "prose"),         # the repo path and the md hook agree on a .md file
    ("prose", "docs/a.markdown", ["markdown"], "prose"),
    ("rdr", "docs/rdr/rdr-1.md", ["markdown"], "prose"),
    ("pdf", "papers/p.pdf", ["pdf"], "prose"),             # registered by the repo PDF path: prose
    ("paper", "/abs/p.pdf", ["pdf"], "paper"),             # registered by nx index pdf: paper
    ("paper", "/abs/P.PDF", ["pdf"], "paper"),             # the extension is read lower-cased
])
def test_a_document_lacking_tags_and_category_gets_what_its_own_indexer_wrote(content_type, path, tags, category):
    plan = mb.plan_tags_category(_indexed({}, content_type, path))
    assert plan.meta == {"tags": tags, "category": category}
    assert plan.outcome == {"tags": STAMPED, "category": STAMPED}


@pytest.mark.parametrize("content_type,path,category,tags_reason", [
    ("prose", "notes/a.txt", "prose", "ambiguous_indexer"),   # repo says ["txt"], nx index md says ["markdown"]
    ("rdr", "r.rst", "prose", "ambiguous_indexer"),
    ("prose", "Makefile", "prose", "ambiguous_indexer"),
    ("prose", "", "prose", "no_file_path"),
    ("code", "Makefile", "code", "no_extension"),            # the row writer wrote "" for a file with no extension
    ("code", "", "code", "no_file_path"),
])
def test_a_pair_with_only_one_of_the_two_keys_derivable_stamps_it_and_names_why_the_other_is_residue(
    content_type, path, category, tags_reason,
):
    plan = mb.plan_tags_category(_indexed({}, content_type, path))
    assert plan.meta == {"category": category}, "no tags key is sent without a real value"
    assert plan.outcome == {"tags": f"residue:{tags_reason}", "category": STAMPED}


@pytest.mark.parametrize("content_type,path,reason", [
    # a prose or rdr entry at a .pdf path: the repo pass registers it as type pdf, so this entry was
    # registered by nx index md / rdr or a DEVONthink import, and nx index pdf of the same file stamps
    # paper/["pdf"]; the entry cannot say which ran, so neither key is guessed
    ("prose", "papers/p.pdf", "ambiguous_indexer"),
    ("rdr", "docs/rdr/p.pdf", "ambiguous_indexer"),
    ("prose", "papers/P.PDF", "ambiguous_indexer"),
    # a paper entry with no path is an `nx catalog backfill` placeholder (one per docs__ collection)
    ("paper", "", "no_file_path"),
    # a paper entry whose path is not a .pdf (a placeholder is often markdown) was not made by nx index pdf
    ("paper", "a.md", "not_pdf_path"),
    ("paper", "notes/a.txt", "not_pdf_path"),
    ("paper", "Makefile", "not_pdf_path"),
])
def test_a_pair_whose_indexer_the_entry_cannot_name_leaves_both_keys_as_residue(content_type, path, reason):
    plan = mb.plan_tags_category(_indexed({}, content_type, path))
    assert plan.meta == {}, "a fill-only write is permanent: nothing is sent for a pair that was guessed"
    assert plan.outcome == {"tags": f"residue:{reason}", "category": f"residue:{reason}"}


@pytest.mark.parametrize("content_type,path,reason", [
    ("docs", "a.txt", "no_rule"), ("", "LICENSE", "no_rule"), ("markdown", "a.md", "no_rule"),
    ("docs", "", "no_file_path"), ("", "", "no_file_path"),
])
def test_a_pair_the_rule_derives_nothing_for_is_residue_for_both_keys(content_type, path, reason):
    plan = mb.plan_tags_category(_indexed({}, content_type, path))
    assert plan.meta == {} and plan.outcome == {"tags": f"residue:{reason}", "category": f"residue:{reason}"}


@pytest.mark.parametrize("held", [
    {"tags": ["curated"]}, {"tags": "a, b"},                 # a non-empty tags value, a legacy string included
    {"category": "curated"},
])
def test_a_key_the_entry_holds_non_empty_is_never_touched(held):
    plan = mb.plan_tags_category(_indexed(held, "paper", "p.pdf"))
    key = next(iter(held))
    other = "category" if key == "tags" else "tags"
    assert key not in plan.meta and plan.outcome[key] == PRESENT
    assert plan.outcome[other] == STAMPED and other in plan.meta, "the other key is still open and still filled"


@pytest.mark.parametrize("held", [
    {"tags": []}, {"tags": None}, {"tags": ""},
    {"category": ""}, {"category": None}, {"category": []},
])
def test_a_key_the_entry_holds_empty_is_filled_as_a_re_index_would_fill_it(held):
    """nexus-3ax49 (Sam's ruling, 2026-11-01): the backfill and a re-index share one definition of empty for an
    indexed document's tags and category: absent, None, '' and [] are open, anything else is held."""
    plan = mb.plan_tags_category(_indexed(held, "paper", "p.pdf"))
    assert plan.meta == {"tags": ["pdf"], "category": "paper"}
    assert plan.outcome == {"tags": STAMPED, "category": STAMPED}


def test_an_empty_key_the_rule_cannot_settle_is_residue_not_a_held_key():
    plan = mb.plan_tags_category(_indexed({"tags": []}, "prose", "notes/a.txt"))
    assert plan.meta == {"category": "prose"}
    assert plan.outcome == {"tags": "residue:ambiguous_indexer", "category": STAMPED}


def test_a_document_holding_both_keys_non_empty_sends_nothing():
    plan = mb.plan_tags_category(_indexed({"tags": ["x"], "category": "y"}, "code", "a.py"))
    assert plan.meta == {} and not plan.has_writes() and plan.outcome == {"tags": PRESENT, "category": PRESENT}


def test_the_backfill_and_the_re_index_share_one_definition_of_empty():
    """A value the re-index helper fills is a key the backfill fills, and the reverse, for every shape."""
    from nexus.catalog.indexed_meta import fill_only_meta

    stamp = {"tags": ["py"], "category": "code"}
    for value in (None, "", [], (), {}, ["t"], "a, b", "curated", 0, False):
        held = {"tags": value, "category": value}
        plan = mb.plan_tags_category(_indexed(held, "code", "a.py"))
        re_index_fills = fill_only_meta(stamp, held) == stamp
        assert (plan.outcome["tags"] == STAMPED) is re_index_fills, repr(value)
        assert (plan.outcome["category"] == STAMPED) is re_index_fills, repr(value)


def test_the_derived_keys_read_no_chunk_row_and_no_manifest():
    """A document whose row-sourced keys are all held and whose chunks are all shared still gets tags and
    category, and the run neither reads a manifest, asks for owners, nor reads a chunk row for it."""
    class _Trip(_World):
        def get_manifests(self, ids):
            raise AssertionError("no manifest read for a document with nothing row-sourced open")

        def chash_positions(self, chashes):
            raise AssertionError("no owner lookup")

        def chunk_rows(self, collection, chashes, *, sensitivity="include"):
            raise AssertionError("no chunk row read")

    w = _Trip()
    w.entries = [_indexed({}, "paper", "p.pdf", "1.4.1"), _indexed({}, "code", "src/a.py", "1.4.2")]
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert {u["tumbler"]: u["meta"] for u in w.updates} == {
        "1.4.1": {"source_format": "pdf", "tags": ["pdf"], "category": "paper"},
        "1.4.2": {"source_format": "code", "tags": ["py"], "category": "code"},
    }
    assert rep.lookups_failed == 0 and rep.documents_written == 2


def test_a_note_is_never_given_derived_tags_or_category_by_its_content_type():
    """A note carries a path-shaped file_path here on purpose: were the note taken for an indexed document,
    source_format would derive ``markdown`` from it and a stamp would be sent. Its keys are counted under
    note.* only, and the note's own residue is what the note rules alone make of it. With the doc guard
    removed the plans still land under note.* (kind stays "note"), so the updates and note.* count asserts
    are what fail; the doc.* asserts only state the expected shape."""
    w = _World()
    w.entries = [
        _entry({"put_at": PUT}, title="n", tumbler="1.4.9", file_path="n.md"),   # holds its note keys
        _entry({}, title="k", tumbler="1.4.8", file_path="k.md"),                # keyless, no row to recover from
    ]
    rep = mb.backfill_meta(w, w, w, apply=True)
    assert w.updates == [], "neither a derived key nor anything else is sent for a note"
    assert not any(k.startswith("doc.") for k in rep.keys), sorted(rep.keys)
    for derived in ("doc.tags", "doc.category"):
        assert rep.keys.get(derived) is None, f"{derived} examined 0: no indexed document is in this world"
    for key in ("tags", "category", "put_at", "source_agent", "ttl_days"):
        st = rep.keys[f"note.{key}"]
        assert (st.examined, st.present, st.shared, st.stamped, dict(st.residue)) == (2, 1, 0, 0, {"no_row": 1}), key


def _tags_world():
    w = _World()
    w.entries = [
        _indexed({}, "code", "src/a.py", "1.5.1"),
        _indexed({}, "prose", "docs/a.md", "1.5.2"),
        _indexed({}, "pdf", "p.pdf", "1.5.3"),
        _indexed({}, "paper", "q.pdf", "1.5.4"),
        _indexed({}, "prose", "docs/a.txt", "1.5.5"),       # tags ambiguous, category stamped
        _indexed({}, "docs", "x.txt", "1.5.6"),             # no rule for either
        _indexed({"tags": ["curated-tag"], "category": "curated"}, "paper", "r.pdf", "1.5.7"),
        _entry({}, title="note", tumbler="1.5.8", content_type="knowledge", indexed_at=PUT),
    ]
    return w


def test_the_dry_run_report_counts_tags_and_category_per_key_with_reasons(monkeypatch):
    w = _tags_world()
    result, _ = _invoke(monkeypatch, w, "--json")
    assert result.exit_code == 0, result.output
    keys = json.loads(result.stdout)["keys"]
    assert keys["doc.tags"] == {
        "examined": 7, "present": 1, "skipped_shared": 0, "stamped": 4, "residue": 2,
        "residue_reasons": {"ambiguous_indexer": 1, "no_rule": 1},
    }
    assert keys["doc.category"] == {
        "examined": 7, "present": 1, "skipped_shared": 0, "stamped": 5, "residue": 1, "residue_reasons": {"no_rule": 1},
    }
    assert w.updates == []

    text, _ = _invoke(monkeypatch, _tags_world())
    assert "doc.tags: examined 7, held 1, skipped (shared) 0, would stamp 4, residue 2" in text.output
    assert "doc.category: examined 7, held 1, skipped (shared) 0, would stamp 5, residue 1" in text.output


def test_apply_writes_the_derived_keys_and_a_second_run_stamps_nothing():
    w = _tags_world()
    mb.backfill_meta(w, w, w, apply=True)
    by = {u["tumbler"]: u["meta"] for u in w.updates}
    assert by["1.5.1"]["tags"] == ["py"] and by["1.5.1"]["category"] == "code"
    assert by["1.5.3"]["category"] == "prose" and by["1.5.4"]["category"] == "paper"
    assert by["1.5.5"].get("category") == "prose" and "tags" not in by["1.5.5"]
    assert "tags" not in by.get("1.5.6", {}) and "category" not in by.get("1.5.6", {})
    assert "tags" not in by.get("1.5.7", {}) and "category" not in by.get("1.5.7", {}), "non-empty held keys are never resent"
    for e in w.entries:
        if str(e.tumbler) in by:
            e.meta = {**(e.meta or {}), **by[str(e.tumbler)]}
    w.updates.clear()
    again = mb.backfill_meta(w, w, w, apply=True)
    assert w.updates == []
    assert again.keys["doc.tags"].stamped == 0 and again.keys["doc.category"].stamped == 0


def test_apply_fills_a_document_whose_tags_and_category_are_empty_and_keeps_a_non_empty_one():
    w = _World()
    w.entries = [
        _indexed({"tags": [], "category": ""}, "code", "src/a.py", "1.6.1"),
        _indexed({"tags": None, "category": None}, "code", "src/b.py", "1.6.2"),
        _indexed({"tags": ["mine"], "category": "mine", "source_format": "code"}, "code", "src/c.py", "1.6.3"),
    ]
    report = mb.backfill_meta(w, w, w, apply=True)
    by = {u["tumbler"]: u["meta"] for u in w.updates}
    for t in ("1.6.1", "1.6.2"):
        assert (by[t]["tags"], by[t]["category"]) == (["py"], "code"), by[t]
    assert "1.6.3" not in by, "a non-empty value is kept: nothing is sent"
    assert report.keys["doc.tags"].stamped == 2 and report.keys["doc.tags"].present == 1


@pytest.mark.parametrize("content_type,path", [
    ("code", "src/a.py"), ("code", "src/A.PY"), ("prose", "docs/a.md"), ("rdr", "docs/rdr/r.md"), ("pdf", "p.pdf"),
])
def test_the_backfill_agrees_with_the_repo_pass_where_it_derives_a_value(content_type, path):
    """The backfill is the register-time statement read back from the entry: wherever it derives a key, the
    repo pass (document_register_meta) wrote the same value."""
    from nexus.catalog.indexed_meta import backfill_tags_category, document_register_meta

    values, residue = backfill_tags_category(content_type, path)
    registered = document_register_meta(content_type, path)
    assert values == {k: registered[k] for k in ("tags", "category")} and residue == {}


def test_the_backfill_agrees_with_the_single_file_verbs_where_it_derives_a_value():
    from nexus.catalog.indexed_meta import backfill_tags_category, markdown_register_meta, pdf_register_meta

    for ct in ("prose", "rdr"):
        values, _ = backfill_tags_category(ct, "a.md")
        assert values == {k: markdown_register_meta(ct, "a.md")[k] for k in ("tags", "category")}
    values, _ = backfill_tags_category("paper", "p.pdf")
    assert values == {k: pdf_register_meta("p.pdf", "paper")[k] for k in ("tags", "category")}
    values, _ = backfill_tags_category("pdf", "p.pdf")
    assert values == {k: pdf_register_meta("p.pdf", "pdf")[k] for k in ("tags", "category")}


# ── nexus-6pfgb.43: a prose document in its own repo's docs/rdr collection takes its extension as tags ───────

REPO = frozenset({"1.87"})
OWN_DOCS = "docs__1-87__voyage-context-3__v1"
OWN_RDR = "rdr__1-87__voyage-context-3__v1"
#: The catalog's docs/rdr collection rows as repo_prose_collections gives them: name -> owner_id (the row's column).
COLLS = {OWN_DOCS: "1-87", OWN_RDR: "1-87", "docs__1-88__m__v1": "1-88", "docs__1-870__m__v1": "1-870",
         "docs__1-4__m__v1": "1-4", "docs__renamed__m__v1": "1-87"}


def _repo_doc(meta, content_type, path, tumbler="1.87.12", collection=OWN_DOCS):
    return _indexed(meta, content_type, path, tumbler=tumbler, collection=collection)


@pytest.mark.parametrize("content_type,path,tags,collection", [
    ("prose", "Ghidra/Processors/x86/data/ia.sinc", ["sinc"], OWN_DOCS),
    ("prose", "ddl_utils/src/main/resources/a.SQL", ["sql"], OWN_DOCS),   # lower-cased, as the repo pass writes it
    ("rdr", "docs/notes.rst", ["rst"], OWN_RDR),
    ("prose", "a/b.sql", ["sql"], "docs__renamed__m__v1"),   # the row owner_id decides, not the name segment
])
def test_a_prose_document_in_its_own_repo_collection_takes_the_repo_pass_extension(content_type, path, tags, collection):
    plan = mb.plan_tags_category(_repo_doc({}, content_type, path, collection=collection), REPO, COLLS)
    assert plan.meta == {"tags": tags, "category": "prose"}
    assert plan.outcome == {"tags": STAMPED, "category": STAMPED}


@pytest.mark.parametrize("tumbler,path,collection,reason", [
    ("1.4.1", "notes/a.txt", "docs__1-4__m__v1", "ambiguous_indexer"),       # not under a repo owner
    ("1.870.1", "notes/a.txt", "docs__1-870__m__v1", "ambiguous_indexer"),   # a string prefix is not a segment match
    ("1.87.12", "notes/a.txt", "knowledge__1-87__m__v1", "ambiguous_indexer"),  # no docs/rdr row for it
    ("1.87.12", "notes/a.txt", "docs__1-88__m__v1", "ambiguous_indexer"),    # another repo's collection
    ("1.87.12", "notes/a.txt", "", "ambiguous_indexer"),                     # no physical collection
    ("1.87.12", "/abs/notes/a.txt", OWN_DOCS, "ambiguous_indexer"),
    ("1.87.12", "C:\\repo\\a.txt", OWN_DOCS, "ambiguous_indexer"),
    ("1.87.12", "file:///repo/a.txt", OWN_DOCS, "ambiguous_indexer"),
    ("1.87.12", "Makefile", OWN_DOCS, "no_extension"),                       # the repo pass writes no tag without one
    ("1.87.12", "", OWN_DOCS, "no_file_path"),
])
def test_a_prose_document_the_rule_cannot_place_stays_residue(tumbler, path, collection, reason):
    plan = mb.plan_tags_category(_repo_doc({}, "prose", path, tumbler=tumbler, collection=collection), REPO, COLLS)
    assert plan.meta == {"category": "prose"}, "no tags key without a real value"
    assert plan.outcome["tags"] == f"residue:{reason}"


def test_the_repo_rule_is_fill_only_and_leaves_a_pdf_path_ambiguous():
    held = mb.plan_tags_category(_repo_doc({"tags": ["markdown"]}, "prose", "a/b.sinc"), REPO, COLLS)
    assert held.meta == {"category": "prose"} and held.outcome["tags"] == PRESENT
    pdf = mb.plan_tags_category(_repo_doc({}, "prose", "a/b.pdf"), REPO, COLLS)
    assert pdf.meta == {} and pdf.outcome == {"tags": "residue:ambiguous_indexer", "category": "residue:ambiguous_indexer"}


def test_repo_owner_prefixes_takes_repo_owners_including_deactivated_ones():
    calls = []

    class _R:
        def list_owners(self, *, include_deactivated=False):
            calls.append(include_deactivated)
            return [{"tumbler_prefix": "1.87", "owner_type": "repo"}, {"tumbler_prefix": "1.2", "owner_type": "curator"},
                    {"tumbler_prefix": "", "owner_type": "repo"}]

    assert mb.repo_owner_prefixes(_R()) == frozenset({"1.87"}) and calls == [True]
    assert mb.repo_owner_prefixes(object()) == frozenset()


def test_a_reader_whose_owner_listing_refuses_the_flag_fails_loudly():
    class _Old:
        def list_owners(self):
            return [{"tumbler_prefix": "1.87", "owner_type": "repo"}]

    with pytest.raises(TypeError):
        mb.repo_owner_prefixes(_Old())


def test_the_run_stamps_from_the_owner_listing_and_reports_how_many_repo_owners_it_saw():
    w = _World()
    w.entries = [_repo_doc({}, "prose", "Ghidra/a.sinc", tumbler="1.87.5"),
                 _indexed({}, "prose", "notes/b.txt", tumbler="1.4.5")]
    w.list_owners = lambda include_deactivated=False: [{"tumbler_prefix": "1.87", "owner_type": "repo"}]
    w.list_collections = lambda lifecycle_state=None, content_type=None: (
        [{"name": OWN_DOCS, "content_type": "docs", "owner_id": "1-87"}] if content_type in (None, "docs") else [])
    rep = mb.backfill_meta(w, w, w, apply=True)
    stamped = {u["tumbler"]: u["meta"].get("tags") for u in w.updates}
    assert stamped.get("1.87.5") == ["sinc"] and stamped.get("1.4.5") is None
    assert rep.keys["doc.tags"].stamped == 1 and rep.keys["doc.tags"].residue == {"ambiguous_indexer": 1}
    assert rep.repo_owners == 1 and rep.as_dict()["repo_owners"] == 1


def test_repo_prose_collections_keeps_docs_and_rdr_rows_with_their_owner_column():
    rows = [{"name": OWN_DOCS, "content_type": "docs", "owner_id": "1-87"},
            {"name": "knowledge__1-87__m__v1", "content_type": "knowledge", "owner_id": "1-87"},
            {"name": "code__1-87__m__v1", "content_type": "code", "owner_id": "1-87"},
            {"name": OWN_RDR, "content_type": "rdr", "owner_id": "1-87"}]

    class _R:  # the route filters by content_type, as GET /v1/catalog/collections/list does
        def list_collections(self, lifecycle_state=None, content_type=None):
            return [r for r in rows if content_type is None or r["content_type"] == content_type]

    assert mb.repo_prose_collections(_R()) == {OWN_DOCS: "1-87", OWN_RDR: "1-87"}
    assert mb.repo_prose_collections(object()) == {}

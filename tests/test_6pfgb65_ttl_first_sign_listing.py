# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.65 (RDR-236 Phase 4 Step 1b): ``nx catalog backfill-meta --list-ttl-first-sign``, on fakes.

The notes whose expiry RDR-236 Step 1 changes: the TTL sweep's guard held them by the first sign of
``catalog_ttl_stale`` alone. Split from ``tests/test_6pfgb10_meta_backfill.py`` (file-size ratchet), whose
fakes and helpers these tests reuse.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from nexus.catalog import meta_backfill as mb
from nexus.catalog import note_ttl
from nexus.cli import main
from tests.test_6pfgb10_meta_backfill import LATER, MUCH_LATER, PUT, _entry, _invoke, _Manifest, _World

# ── nexus-6pfgb.65 (RDR-236 Phase 4 Step 1b): notes held by catalog_ttl_stale's first sign alone ────
#
# RDR-236 Step 1 stops ``catalog_ttl_stale`` holding a note whose row TTL differs from the catalog's while the
# row's ``indexed_at`` is not later than ``put_at`` plus the skew (a pre-.18 import's permanent row, or a note
# whose owner changed its TTL with a new-client re-put: two shapes the rows cannot tell apart). The dry run
# counts and lists them so Sam reads the list before Step 1 lands. Read-only: it stamps nothing and changes no
# expiry decision.

EARLIER = "2026-09-01T12:00:00+00:00"      # a legacy row's clock, a month before the catalog's put_at


def _ttl_world():
    """Three keyed notes, each with one legacy-keyed row (the shape an older client left):
    A  row permanent, row indexed_at before put_at   -> held by the first sign alone
    B  row permanent, row indexed_at an hour after   -> caught by the indexed_at sign
    C  row TTL equal to the catalog's, in step       -> not held"""
    w = _World()
    keyed = {"ttl_days": 1, "put_at": PUT, "tags": [], "category": "", "source_agent": ""}
    notes = [
        _entry(dict(keyed), title="a-note", tumbler="1.1.1"),
        _entry(dict(keyed), title="b-note", tumbler="1.1.2"),
        _entry(dict(keyed), title="c-note", tumbler="1.1.3"),
    ]
    w.entries = notes
    w.manifests = {str(e.tumbler): [_Manifest(f"n{i}")] for i, e in enumerate(notes, 1)}
    w.owners = {f"n{i}": [str(e.tumbler)] for i, e in enumerate(notes, 1)}
    coll = notes[0].physical_collection
    w.rows[(coll, "n1")] = {"catalog_doc_id": "1.1.1", "title": "a-note", "indexed_at": EARLIER}
    w.rows[(coll, "n2")] = {"catalog_doc_id": "1.1.2", "title": "b-note", "indexed_at": MUCH_LATER}
    w.rows[(coll, "n3")] = {"catalog_doc_id": "1.1.3", "title": "c-note", "indexed_at": LATER, "ttl_days": 1}
    return w


EXPIRES = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)     # PUT plus the notes' 1 day TTL


def _listed(w, *, now=EXPIRES, **kw):
    """A dry run that lists (the listing reads rows only when asked), at a fixed clock."""
    return mb.backfill_meta(w, w, None, apply=False, list_first_sign=True, now=now, **kw)


def test_the_dry_run_lists_the_note_held_by_the_first_sign_alone_and_no_other():
    w = _ttl_world()

    report = _listed(w)

    assert [n["tumbler"] for n in report.ttl_first_sign_notes] == ["1.1.1"], (
        "A (row TTL differs, row older than put_at) is listed; B (caught by the indexed_at sign) and C (TTLs match) are not")
    only = report.ttl_first_sign_notes[0]
    assert only == {
        "tumbler": "1.1.1", "title": "a-note", "collection": w.entries[0].physical_collection,
        "catalog_ttl_days": 1, "put_at": PUT, "row_ttl_days": None, "row_indexed_at": EARLIER, "rows": 1,
        "reason": "first sign only", "expires_at": EXPIRES.isoformat(), "lapsed_now": True,
    }
    assert w.updates == [], "read-only"
    assert report.documents_to_stamp == 0 and report.documents_complete == 3, "the listing changes no count of the run"


def test_a_listed_note_says_whether_it_has_already_lapsed_on_both_sides_of_its_expiry():
    """What the first sweep after Step 1 ships would retire: a note past its expiry. The boundary is the
    expiry itself, as note_ttl.lapsed reads it."""
    before = _listed(_ttl_world(), now=EXPIRES - timedelta(seconds=1)).ttl_first_sign_notes[0]
    at = _listed(_ttl_world(), now=EXPIRES).ttl_first_sign_notes[0]
    assert (before["lapsed_now"], at["lapsed_now"]) == (False, True)
    assert before["expires_at"] == at["expires_at"] == EXPIRES.isoformat()


def test_a_catalog_permanent_note_with_an_old_ttld_row_is_not_listed():
    """Step 1 cannot change a note that never lapses (the shape of every post-.18 import)."""
    w = _ttl_world()
    perm = _entry({"ttl_days": 0, "put_at": PUT}, title="p-note", tumbler="1.1.4")
    absent = _entry({"put_at": PUT}, title="q-note", tumbler="1.1.5")
    w.entries += [perm, absent]
    coll = perm.physical_collection
    for i, e in enumerate([perm, absent], 4):
        w.manifests[str(e.tumbler)] = [_Manifest(f"n{i}")]
        w.owners[f"n{i}"] = [str(e.tumbler)]
        w.rows[(coll, f"n{i}")] = {"catalog_doc_id": str(e.tumbler), "title": e.title, "indexed_at": EARLIER, "ttl_days": 30}

    report = _listed(w)

    assert [n["tumbler"] for n in report.ttl_first_sign_notes] == ["1.1.1"]


def test_a_row_with_a_differing_ttl_and_no_indexed_at_is_listed_under_its_own_reason():
    """The pre-Step-1 rule held it by the TTL alone; Step 1 ignores a row with no clock."""
    w = _ttl_world()
    coll = w.entries[0].physical_collection
    w.rows[(coll, "n1")] = {"catalog_doc_id": "1.1.1", "title": "a-note"}   # what a current client leaves
    w.rows[(coll, "n3")] = {"catalog_doc_id": "1.1.3", "title": "c-note", "ttl_days": 1}   # control: the TTLs match

    report = _listed(w)

    by = {n["tumbler"]: n for n in report.ttl_first_sign_notes}
    assert set(by) == {"1.1.1"}
    assert by["1.1.1"]["reason"] == "first sign, no indexed_at" and by["1.1.1"]["row_indexed_at"] is None


def test_the_note_held_by_the_first_sign_alone_is_not_held_by_the_expiry_guard_since_step_1():
    """The listing names what Step 1 releases: the guard that sweeps expiry ignores it, and still holds B."""
    w = _ttl_world()
    by ={str(e.tumbler): e for e in w.entries}

    def stale(tumbler, chash):
        e = by[tumbler]
        return note_ttl.catalog_ttl_stale(
            e.meta, e.indexed_at, [w.rows[(e.physical_collection, chash)]], owner_tumbler=e.tumbler, owner_title=e.title)

    assert (stale("1.1.1", "n1"), stale("1.1.2", "n2"), stale("1.1.3", "n3")) == (False, True, False)


def test_a_note_with_one_row_held_by_the_first_sign_and_another_by_the_indexed_at_sign_is_not_listed():
    w = _ttl_world()
    w.manifests["1.1.1"] = [_Manifest("n1"), _Manifest("n1b")]
    w.rows[(w.entries[0].physical_collection, "n1b")] = {
        "catalog_doc_id": "1.1.1", "title": "a-note", "indexed_at": MUCH_LATER}

    report = _listed(w)

    assert report.ttl_first_sign_notes == []


def test_an_entry_without_put_at_is_not_listed():
    w = _ttl_world()
    coll = w.entries[0].physical_collection
    nokey = _entry({"ttl_days": 1}, title="d-note", tumbler="1.1.4")        # no put_at: nothing there changed
    w.entries.append(nokey)
    w.manifests["1.1.4"] = [_Manifest("n4")]
    w.owners["n4"] = ["1.1.4"]
    w.rows[(coll, "n4")] = {"catalog_doc_id": "1.1.4", "title": "d-note", "indexed_at": EARLIER}

    report = _listed(w)

    assert [n["tumbler"] for n in report.ttl_first_sign_notes if n["tumbler"] == "1.1.4"] == []


def _counting_reads(w):
    reads: list[str] = []
    real = w.chunk_rows
    w.chunk_rows = lambda collection, chashes, **kw: reads.extend(chashes) or real(collection, chashes, **kw)  # type: ignore[method-assign]
    return reads


def test_a_plain_dry_run_reads_no_row_for_the_listing_and_reports_no_section():
    """The listing reads every keyed note's manifest and rows: it runs only when asked, so a plain dry run reads
    what it read before Step 1b."""
    w = _ttl_world()
    reads = _counting_reads(w)
    manifest_asks: list[list[str]] = []
    real = w.get_manifests
    w.get_manifests = lambda ids: manifest_asks.append(list(ids)) or real(ids)  # type: ignore[method-assign]

    report = mb.backfill_meta(w, w, None, apply=False, now=EXPIRES)

    assert reads == [] and manifest_asks == [], "every note is keyed and complete: the plain run needs no row"
    assert report.ttl_first_sign_checked is False and report.ttl_first_sign_notes == []
    assert "ttl_first_sign_only" not in report.as_dict()


def test_an_apply_run_lists_nothing_and_reads_no_row_for_it():
    w = _ttl_world()
    reads = _counting_reads(w)

    report = mb.backfill_meta(w, w, w, apply=True)

    assert report.ttl_first_sign_notes == [] and reads == [] and w.updates == []
    assert "ttl_first_sign_only" not in report.as_dict()


def test_an_apply_run_refuses_the_listing():
    w = _ttl_world()
    for kw in ({"list_first_sign": True}, {"list_only": True}):
        with pytest.raises(ValueError, match="dry run"):
            mb.backfill_meta(w, w, w, apply=True, **kw)


def test_the_list_only_run_reads_the_keyed_notes_and_skips_the_stamping_plan():
    """No owner lookup, no document's rows, no plan: the counts of the stamping plan stay empty, and a keyless
    note and an indexed document cost nothing."""
    w = _ttl_world()
    keyless = _entry({}, title="k-note", tumbler="1.1.4")
    doc = _entry({}, title="solo", tumbler="1.1.5", content_type="paper", file_path="p.pdf", collection="docs__x__m__v1")
    w.entries += [keyless, doc]
    w.manifests.update({"1.1.4": [_Manifest("n4")], "1.1.5": [_Manifest("s1")]})
    w.owners.update({"n4": ["1.1.4"], "s1": ["1.1.5"]})
    w.rows[(keyless.physical_collection, "n4")] = {"catalog_doc_id": "1.1.4", "title": "k-note", "ttl_days": 3}
    w.rows[("docs__x__m__v1", "s1")] = {"extraction_method": "mineru"}
    reads = _counting_reads(w)
    owner_asks: list[list[str]] = []
    real = w.chash_positions
    w.chash_positions = lambda chashes: owner_asks.append(list(chashes)) or real(chashes)  # type: ignore[method-assign]

    report = mb.backfill_meta(w, w, None, apply=False, list_only=True, now=EXPIRES)

    assert sorted(reads) == ["n1", "n2", "n3"], "only the keyed notes' rows"
    assert owner_asks == [] and w.updates == []
    assert [n["tumbler"] for n in report.ttl_first_sign_notes] == ["1.1.1"]
    assert report.documents_examined == 5 and report.documents_to_stamp == 0 and report.keys == {}
    assert report.as_dict()["list_only"] is True


def test_a_failed_row_read_for_the_listing_is_counted_and_never_fails_the_run():
    w = _ttl_world()
    w.fail_rows_for = {w.entries[0].physical_collection}

    report = _listed(w)

    assert report.ttl_first_sign_notes == [] and report.ttl_first_sign_unread == 3
    assert report.lookups_failed == 0 and report.documents_complete == 3


def test_a_listed_note_in_a_later_batch_of_a_run_with_progress_is_listed():
    """Review r2 of b1d604af4: the progress block read the clock into a name the listing also used, so from the
    second batch on the listing compared a float with a datetime and the run died. The held note sits in the LAST
    of three one-document batches, with a progress callback, the shape every real CLI run has."""
    w = _ttl_world()
    w.entries.reverse()     # A, the held note, is now batch 3 of 3
    seen = []

    report = mb.backfill_meta(w, w, None, apply=False, list_first_sign=True, batch=1, progress=seen.append, now=EXPIRES)

    assert len(seen) >= 2, "non-vacuous: progress fired before the listed note's batch"
    assert [n["tumbler"] for n in report.ttl_first_sign_notes] == ["1.1.1"]
    assert report.ttl_first_sign_notes[0]["lapsed_now"] is True


def test_the_command_lists_a_note_in_a_later_batch(monkeypatch):
    w = _ttl_world()
    w.entries.reverse()

    listed, _ = _invoke(monkeypatch, w, "--list-ttl-first-sign", "--batch-size", "1")

    assert listed.exit_code == 0, listed.output
    assert "1.1.1" in listed.output and "a-note" in listed.output, listed.output


def test_failed_listing_reads_stop_the_run_after_the_failed_round_limit():
    """A failed listing read is trouble for --stop-after-failed-rounds, so a listing run against a failing service
    stops instead of walking the whole catalog one failing request per batch."""
    w = _ttl_world()
    w.fail_rows_for = {w.entries[0].physical_collection}

    report = mb.backfill_meta(w, w, None, apply=False, list_first_sign=True, batch=1, stop_after_failed_rounds=2,
                              now=EXPIRES)

    assert report.interrupted and report.stop_reason == mb.STOP_FAILED_ROUNDS, report
    assert report.batches_done == 2 and report.ttl_first_sign_unread == 2


def test_the_command_counts_the_list_in_the_summary_and_prints_it_only_under_the_flag(monkeypatch):
    listed, _ = _invoke(monkeypatch, _ttl_world(), "--list-ttl-first-sign")
    assert listed.exit_code == 0, listed.output
    assert "1 note(s) held today by catalog_ttl_stale's first sign alone" in listed.output
    assert "1.1.1" in listed.output and "a-note" in listed.output and "first sign only" in listed.output
    assert "lapsed already" in listed.output and "not lapsed yet" not in listed.output
    assert "b-note" not in listed.output and "c-note" not in listed.output


def test_a_plain_dry_run_command_neither_reads_nor_mentions_the_list(monkeypatch):
    w = _ttl_world()
    reads = _counting_reads(w)
    plain, _ = _invoke(monkeypatch, w)
    assert plain.exit_code == 0, plain.output
    assert reads == [] and "first sign" not in plain.output and "a-note" not in plain.output


def test_the_json_report_carries_the_count_and_the_rows_under_the_flag_only(monkeypatch):
    result, _ = _invoke(monkeypatch, _ttl_world(), "--json", "--list-ttl-first-sign")
    assert result.exit_code == 0, result.output
    section = json.loads(result.stdout)["ttl_first_sign_only"]
    assert section["count"] == 1 and section["unread"] == 0
    assert [n["tumbler"] for n in section["notes"]] == ["1.1.1"]
    assert section["notes"][0]["reason"] == "first sign only"
    assert isinstance(section["notes"][0]["lapsed_now"], bool) and section["notes"][0]["expires_at"]

    plain, _ = _invoke(monkeypatch, _ttl_world(), "--json")
    assert "ttl_first_sign_only" not in json.loads(plain.stdout)


def test_an_unread_keyed_note_exits_non_zero_and_the_summary_says_the_list_is_incomplete(monkeypatch):
    w = _ttl_world()
    w.fail_rows_for = {w.entries[0].physical_collection}
    result, _ = _invoke(monkeypatch, w, "--list-ttl-first-sign")
    assert result.exit_code == 1, result.output
    assert "3 keyed note(s) could not be read" in result.output and "incomplete" in result.output


def test_the_list_only_command_prints_the_list_without_the_stamping_summary(monkeypatch):
    result, _ = _invoke(monkeypatch, _ttl_world(), "--list-ttl-first-sign-only")
    assert result.exit_code == 0, result.output
    assert "1.1.1" in result.output and "a-note" in result.output
    assert "note.ttl_days" not in result.output and "would stamp" not in result.output


@pytest.mark.parametrize("flag", ["--list-ttl-first-sign", "--list-ttl-first-sign-only"])
def test_the_listing_is_refused_with_apply(monkeypatch, flag):
    w = _ttl_world()
    result, _ = _invoke(monkeypatch, w, "--apply", flag)
    assert result.exit_code == 2 and "--apply" in result.output and w.updates == []


def test_the_help_states_the_read_load_of_the_listing_in_requests():
    result = CliRunner().invoke(main, ["catalog", "backfill-meta", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "ceil(chunks/300)" in text and "chunk text included" in text
    assert "about seven requests" not in text


def test_an_apply_command_prints_no_list_and_the_json_has_no_section(monkeypatch):
    result, _ = _invoke(monkeypatch, _ttl_world(), "--apply", "--json")
    assert result.exit_code == 0, result.output
    assert "ttl_first_sign_only" not in json.loads(result.stdout)

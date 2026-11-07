# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 7.94.0 shakedown (classes C/G): ``nx upgrade`` reported the ``note-tags-array`` and
``manifest-section-labels`` rungs "converged and verified" on a fresh box, where there was nothing to
convert, so that line proved nothing about the conversion.

These pins seed the legacy rows on the self-provisioned engine and drive the production walk
(``nexus.commands.upgrade._run_ladder`` over ``default_registry``): the dry run must report the rung
pending, the real run must convert exactly the legacy rows and record the rung, and a second dry run must
find it converged. Each fails if the rung's ``converge`` is replaced with a no-op (checked by hand when
this file was written; the first assertion that trips is the post-walk state, not a log line).

Not integration-marked: the substrate provisions itself. The rung-level twins are
``test_note_tags_array_rung.py`` (storage-free) and ``test_rdr192_manifest_backfill_substrate.py``.
"""
from __future__ import annotations

import pytest

from nexus.commands.upgrade import _run_ladder
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL, RUNG_NOTE_TAGS_ARRAY
from tests.upgrade._substrate_sql import MODEL, seed_chunk

_LABELLED = {
    "section_title": "Refs", "section_type": "references", "page_number": 3,
    "indexed_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
def catalog(t2_service_env):
    from tests._catalog_fixture_ops import ActiveCatalog  # noqa: PLC0415

    cat = ActiveCatalog()
    return cat, cat.register_owner("shk794", "curator")


def _pending_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "pending" in line]


def test_the_walk_converts_a_comma_string_note_and_a_second_dry_run_is_converged(
    t2_service_env, catalog, capsys,
) -> None:
    cat, owner = catalog
    coll = f"knowledge__shk794tags__{MODEL}__v1"
    legacy = str(cat.register(
        owner, "legacy tags note", content_type="knowledge", physical_collection=coll,
        meta={"tags": "arch, decision,arch", "category": "design"}))
    current = str(cat.register(
        owner, "current tags note", content_type="knowledge", physical_collection=coll,
        meta={"tags": ["fresh"]}))
    # Non-vacuity: the legacy spelling is stored as a string, the control as an array.
    assert cat.resolve(legacy).meta["tags"] == "arch, decision,arch"
    assert cat.resolve(current).meta["tags"] == ["fresh"]

    _run_ladder(dry_run=True, auto_mode=False)
    pending = [line for line in _pending_lines(capsys.readouterr().out) if RUNG_NOTE_TAGS_ARRAY in line]
    assert len(pending) == 1 and "1 note(s) of 2 hold their tags as a comma string" in pending[0], pending
    assert cat.resolve(legacy).meta["tags"] == "arch, decision,arch", "the dry run writes nothing"

    _run_ladder(dry_run=False, auto_mode=False)
    assert f"rung '{RUNG_NOTE_TAGS_ARRAY}' converged and verified" in capsys.readouterr().out
    after = cat.resolve(legacy).meta
    assert after["tags"] == ["arch", "decision"] and after["category"] == "design"
    assert cat.resolve(current).meta["tags"] == ["fresh"], "an array is left alone"

    _run_ladder(dry_run=True, auto_mode=False)
    assert not [line for line in _pending_lines(capsys.readouterr().out) if RUNG_NOTE_TAGS_ARRAY in line]


def test_the_walk_fills_exactly_the_legacy_row_a_chunk_label_is_exact_for(
    t2_service_env, catalog, capsys,
) -> None:
    tenant = t2_service_env
    cat, owner = catalog
    coll = f"knowledge__shk794labels__{MODEL}__v1"

    def doc(title: str, rows: list[tuple[str, int]]) -> str:
        t = str(cat.register(owner, title, content_type="paper", physical_collection=coll, chunk_count=len(rows)))
        cat.write_manifest(t, [{"chash": c, "position": p} for c, p in rows], collection=coll)
        return t

    c_single = seed_chunk(tenant, coll, "single shk794", _LABELLED)
    c_shared = seed_chunk(tenant, coll, "shared shk794", _LABELLED)
    c_bare = seed_chunk(tenant, coll, "bare shk794", {"indexed_at": "2026-01-01T00:00:00+00:00"})
    single = doc("single", [(c_single, 0)])
    shared_a, shared_b = doc("shared-a", [(c_shared, 0)]), doc("shared-b", [(c_shared, 0)])
    bare = doc("bare", [(c_bare, 0)])

    # Non-vacuity: every row is legacy, the chunk row of `single` carries a label, and only one row holds it.
    for t in (single, shared_a, shared_b, bare):
        assert [r.labels_written for r in cat.get_manifest(t)] == [False], t

    _run_ladder(dry_run=True, auto_mode=False)
    pending = [line for line in _pending_lines(capsys.readouterr().out) if RUNG_MANIFEST_SECTION_LABELS in line]
    assert len(pending) == 1 and "1 legacy manifest row(s) can take" in pending[0], pending  # the engine's count (nexus-l4hds)
    assert [r.labels_written for r in cat.get_manifest(single)] == [False], "the dry run writes nothing"

    _run_ladder(dry_run=False, auto_mode=False)
    assert f"rung '{RUNG_MANIFEST_SECTION_LABELS}' converged and verified" in capsys.readouterr().out
    (filled,) = cat.get_manifest(single)
    assert (filled.section_title, filled.section_type, filled.page_number, filled.labels_written) == (
        "Refs", "references", 3, True)
    for t in (shared_a, shared_b, bare):
        assert [r.labels_written for r in cat.get_manifest(t)] == [False], f"{t} stays legacy by design"

    _run_ladder(dry_run=True, auto_mode=False)
    assert not [line for line in _pending_lines(capsys.readouterr().out) if RUNG_MANIFEST_SECTION_LABELS in line]


_SPAN = {"line_start": 3, "line_end": 9, "chunk_start_char": 40, "chunk_end_char": 190}
_SPAN_COLUMNS = ("line_start", "line_end", "char_start", "char_end")


def _engine_null_span_rows() -> int:
    """The engine's own count (the retire-keys dry run, ``ChunkKeySweep.preconditions``): live manifest rows with a NULL
    span column whose chunk row holds the matching key. It writes nothing."""
    from nexus.db import make_t3  # noqa: PLC0415

    return int(make_t3().chunk_keys_sweep(dry_run=True)["preconditions"]["null_span_rows"])


def _span_fill_counts():
    """The rung's own read-only scan against the engine under test: what its summary says, to compare with the engine."""
    from nexus.upgrade_ladder.rungs.manifest_span_fill import ManifestSpanFillRung  # noqa: PLC0415

    counts = ManifestSpanFillRung(record_fn=lambda: None, managed_fn=lambda: False)._dry_run()
    assert counts is not None
    return counts


def test_the_walk_fills_exactly_the_manifest_spans_a_chunk_row_is_exact_for(
    t2_service_env, catalog, capsys,
) -> None:
    """nexus-l8o33. Against the engine's own ``null_span_rows``: the fill lowers it by exactly the rows it is exact for,
    leaves a shared chunk, a chunk a deleted document also holds, a chunk that records no span and a row whose stored
    column the chunk row contradicts NULL, fills the NULL columns of a partial row that agrees, keeps a column the manifest
    already holds, and sends a row's section labels back unchanged (the append rewrites them from the request). The
    rung's summary accounts for the engine's count, before and after, bucket by bucket."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = f"knowledge__shk794spans__{MODEL}__v1"
    stamp = {"indexed_at": "2026-01-01T00:00:00+00:00"}

    def doc(title: str, rows: list[dict]) -> str:
        t = str(cat.register(owner, title, content_type="paper", physical_collection=coll, chunk_count=len(rows)))
        cat.write_manifest(t, rows, collection=coll)
        return t

    c_exact = seed_chunk(tenant, coll, "exact span shk794", {**_SPAN, **stamp})
    c_agree = seed_chunk(tenant, coll, "agreeing partial span shk794", {**_SPAN, **stamp})
    c_clash = seed_chunk(tenant, coll, "contradicted span shk794", {**_SPAN, **stamp})
    c_labelled = seed_chunk(tenant, coll, "labelled span shk794", {**_SPAN, **stamp})
    c_shared = seed_chunk(tenant, coll, "shared span shk794", {**_SPAN, **stamp})
    c_ghost = seed_chunk(tenant, coll, "deleted holder span shk794", {**_SPAN, **stamp})
    c_zero = seed_chunk(tenant, coll, "unrecorded span shk794", {
        "line_start": 0, "line_end": 0, "chunk_start_char": 0, "chunk_end_char": 0, **stamp})
    c_lines = seed_chunk(tenant, coll, "lines only span shk794", {
        "line_start": 3, "line_end": 9, "chunk_start_char": 0, "chunk_end_char": 0, **stamp})

    exact = doc("exact", [{"chash": c_exact, "position": 0}])
    agree = doc("agree", [{"chash": c_agree, "position": 0, "line_start": 3, "line_end": 9}])
    clash = doc("clash", [{"chash": c_clash, "position": 0, "line_start": 77, "line_end": 80}])
    labelled = doc("labelled", [{
        "chash": c_labelled, "position": 0, "section_title": "Intro", "section_type": "body", "page_number": 4}])
    shared_a = doc("shared-a", [{"chash": c_shared, "position": 0}])
    shared_b = doc("shared-b", [{"chash": c_shared, "position": 0}])
    held = doc("held", [{"chash": c_ghost, "position": 0}])
    gone = doc("gone", [{"chash": c_ghost, "position": 0}])
    zero = doc("zero", [{"chash": c_zero, "position": 0}])
    lines = doc("lines", [{"chash": c_lines, "position": 0}])
    assert cat.delete_document(gone)

    def spans(t: str) -> tuple:
        (r,) = cat.get_manifest(t)
        return tuple(getattr(r, c) for c in _SPAN_COLUMNS)

    # Non-vacuity: every column of the fillable rows is NULL, or is the partial rows' two stored ones, and the chunk rows hold spans.
    assert spans(exact) == spans(labelled) == spans(lines) == (None,) * 4
    assert spans(agree) == (3, 9, None, None) and spans(clash) == (77, 80, None, None)
    (before_labels,) = cat.get_manifest(labelled)
    assert (before_labels.section_title, before_labels.labels_written) == ("Intro", True)
    before = _engine_null_span_rows()
    assert before == 9, "exact, agree, clash, labelled, shared x2, held (the deleted holder's row is not live), zero, lines"

    # The rung's summary accounts for the engine's count, by name (the dry run is the same scan the walk makes).
    scan = _span_fill_counts()
    assert scan.counted == before and scan.reconciles(), scan.summary()
    assert (scan.fillable, scan.multi_row_cross_document, scan.tombstoned_co_owner, scan.chunk_span_unrecorded,
            scan.span_disagrees) == (4, 2, 1, 1, 1), scan.summary()
    assert scan.abandoned_total() == 4, "the shared pair (2), the held row and the contradicted row record a span that goes unused"
    assert scan.engine_count_after() == before - 3, "the lines-only row is filled and stays counted for its zero chars"

    _run_ladder(dry_run=True, auto_mode=False)
    pending = [line for line in _pending_lines(capsys.readouterr().out) if RUNG_MANIFEST_SPAN_FILL in line]
    assert len(pending) == 1 and "4 manifest row(s) of 9 with a NULL span column can take" in pending[0], pending
    assert spans(exact) == (None,) * 4, "the dry run writes nothing"
    assert _engine_null_span_rows() == before

    _run_ladder(dry_run=False, auto_mode=False)
    assert f"rung '{RUNG_MANIFEST_SPAN_FILL}' converged and verified" in capsys.readouterr().out
    assert spans(exact) == (3, 9, 40, 190)
    assert spans(agree) == (3, 9, 40, 190), "the columns the manifest held are kept; the NULL ones are filled"
    assert spans(labelled) == (3, 9, 40, 190)
    assert spans(lines) == (3, 9, None, None), "the chunk records no chars: nothing to copy there"
    assert spans(clash) == (77, 80, None, None), "the chunk row contradicts the stored lines: nothing is copied"
    (after_labels,) = cat.get_manifest(labelled)
    assert (after_labels.section_title, after_labels.section_type, after_labels.page_number,
            after_labels.labels_written) == ("Intro", "body", 4, True), "labels are sent back as they were"
    for t in (shared_a, shared_b, held, zero):
        assert spans(t) == (None,) * 4, f"{t} stays NULL by design"
    after = _engine_null_span_rows()
    assert after == before - 3 == scan.engine_count_after(), (before, after, scan.summary())

    # After the walk the same scan names the residue: nothing fillable, every counted row in a named bucket.
    rest = _span_fill_counts()
    assert rest.counted == after and rest.fillable == 0 and rest.reconciles(), rest.summary()
    assert (rest.multi_row_cross_document, rest.tombstoned_co_owner, rest.chunk_span_unrecorded, rest.span_disagrees) == (
        2, 1, 2, 1), rest.summary()

    _run_ladder(dry_run=True, auto_mode=False)
    assert not [line for line in _pending_lines(capsys.readouterr().out) if RUNG_MANIFEST_SPAN_FILL in line]
    _run_ladder(dry_run=False, auto_mode=False)
    assert _engine_null_span_rows() == after, "a rerun changes nothing"

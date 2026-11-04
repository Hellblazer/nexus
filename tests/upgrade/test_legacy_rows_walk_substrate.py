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
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, RUNG_NOTE_TAGS_ARRAY
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

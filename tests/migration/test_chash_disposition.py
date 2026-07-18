# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-180 Item8 (nexus-jxizy.4): null/orphaned chunk_text disposition.

The three-way policy on a synthetic fixture tenant (live residue is 0 —
A-1 — so these shapes exist only here, by design): every chunk gets
exactly one disposition, in priority order, keyed off the union old→new
content map. The pointer-cascade and metadata-stamp EXECUTION halves of
dispositions (c)/(d) ride the Item6 ETL writer (nexus-jxizy.6); this
module owns the classification contract those writers consume.
"""
from __future__ import annotations

import hashlib

import pytest

from nexus.migration.chash_disposition import (
    ChunkRecord,
    Disposition,
    OrphanPolicy,
    build_content_map,
    classify,
    full_digest_hex,
    synthetic_chash_hex,
)


def _rec(old: str, text: str | None, tenant: str = "t1", coll: str = "knowledge__x") -> ChunkRecord:
    return ChunkRecord(old_chash=old, chunk_text=text, tenant_id=tenant, collection=coll)


def _old(text: str) -> str:
    """Today's legacy 32-hex id for *text* (the truncation being retired)."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class TestContentMap:
    def test_union_map_covers_every_content_bearing_row(self):
        rows = [_rec(_old("alpha"), "alpha"), _rec(_old("beta"), "beta")]
        cmap = build_content_map(rows)
        assert cmap[_old("alpha")] == full_digest_hex("alpha")
        assert cmap[_old("beta")] == full_digest_hex("beta")

    def test_null_text_rows_contribute_nothing(self):
        rows = [_rec(_old("alpha"), None), _rec(_old("alpha"), "")]
        assert build_content_map(rows) == {}

    def test_old_hex_is_strict_prefix_of_new_hex(self):
        """A-3 finding 2: same text ⇒ same digest, so the legacy 32-hex is
        the prefix of the new 64-hex for every rehashable row."""
        cmap = build_content_map([_rec(_old("gamma"), "gamma")])
        (old, new), = cmap.items()
        assert new.startswith(old)
        assert len(old) == 32 and len(new) == 64


class TestClassify:
    def test_a_rehashable_row_gets_full_digest(self):
        rows = [_rec(_old("alpha"), "alpha")]
        results, counts = classify(rows, build_content_map(rows))
        assert results[0].disposition is Disposition.REHASHED
        assert results[0].new_chash_hex == full_digest_hex("alpha")
        assert counts.rehashed == 1 and counts.remapped == counts.dropped == counts.synthesized == 0

    def test_b_reference_only_with_content_sibling_is_remapped_not_dropped(self):
        content = _rec(_old("alpha"), "alpha", coll="knowledge__a")
        reference = _rec(_old("alpha"), None, coll="docs__b")
        rows = [content, reference]
        results, counts = classify(rows, build_content_map(rows))
        by = {(r.collection, r.disposition) for r in results}
        assert ("knowledge__a", Disposition.REHASHED) in by
        assert ("docs__b", Disposition.REMAPPED) in by
        remapped = next(r for r in results if r.disposition is Disposition.REMAPPED)
        assert remapped.new_chash_hex == full_digest_hex("alpha")
        assert counts.remapped == 1 and counts.dropped == 0 and counts.synthesized == 0

    def test_c_orphan_under_drop_is_dropped_with_no_new_key(self):
        rows = [_rec("f" * 32, None)]
        results, counts = classify(rows, build_content_map(rows), orphan_policy=OrphanPolicy.DROP)
        assert results[0].disposition is Disposition.DROPPED
        assert results[0].new_chash_hex is None
        assert counts.dropped == 1

    def test_d_orphan_under_synthesize_gets_deterministic_flagged_surrogate(self):
        rows = [_rec("f" * 32, None, tenant="t9", coll="docs__z")]
        results, counts = classify(
            rows, build_content_map(rows), orphan_policy=OrphanPolicy.SYNTHESIZE
        )
        r = results[0]
        assert r.disposition is Disposition.SYNTHESIZED
        assert r.new_chash_hex == synthetic_chash_hex("t9", "docs__z", "f" * 32)
        assert r.synthetic is True
        assert counts.synthesized == 1

    def test_priority_order_content_beats_reference_beats_orphan(self):
        """One row of each class in a single run; counts partition exactly."""
        rows = [
            _rec(_old("alpha"), "alpha"),
            _rec(_old("alpha"), None, coll="docs__ref"),
            _rec("0" * 32, None, coll="docs__orphan"),
        ]
        results, counts = classify(rows, build_content_map(rows))
        assert (counts.rehashed, counts.remapped, counts.dropped, counts.synthesized) == (1, 1, 1, 0)
        assert len(results) == len(rows)

    def test_empty_text_is_reference_shaped_not_rehashable(self):
        rows = [_rec(_old("alpha"), "alpha"), _rec(_old("alpha"), "", coll="docs__ref")]
        results, _ = classify(rows, build_content_map(rows))
        ref = next(r for r in results if r.collection == "docs__ref")
        assert ref.disposition is Disposition.REMAPPED


class TestSyntheticSurrogate:
    def test_surrogate_is_deterministic_and_input_scoped(self):
        a = synthetic_chash_hex("t1", "c1", "a" * 32)
        assert a == synthetic_chash_hex("t1", "c1", "a" * 32)
        assert a != synthetic_chash_hex("t2", "c1", "a" * 32)
        assert a != synthetic_chash_hex("t1", "c2", "a" * 32)
        assert a != synthetic_chash_hex("t1", "c1", "b" * 32)

    def test_surrogate_is_full_width_and_versioned(self):
        """32 bytes (64 hex) — byte-indistinguishable from a content digest
        BY DESIGN; the metadata flag is the honest signal, not the bytes."""
        s = synthetic_chash_hex("t1", "c1", "a" * 32)
        assert len(s) == 64
        expected = hashlib.sha256(
            b"nexus:synthetic-chash:v1|t1|c1|" + b"a" * 32
        ).hexdigest()
        assert s == expected


class TestRefusals:
    def test_conflicting_content_for_same_old_chash_fails_loud(self):
        """Two different texts claiming one old_chash is corpus corruption
        (or a 128-bit collision) — never pick one silently (GH #1390)."""
        rows = [
            _rec("c" * 32, "text-one"),
            _rec("c" * 32, "text-two", coll="docs__other"),
        ]
        with pytest.raises(ValueError, match="conflicting content"):
            build_content_map(rows)

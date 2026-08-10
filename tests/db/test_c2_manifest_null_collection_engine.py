# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 nexus/chroma-residue-plan-2026-08-10 §C2: real-engine coverage proving
``nx doctor``'s manifest-orphan check can read FALSE-CLEAN on pre-backfill
(``collection IS NULL``) manifest rows, and that the fix surfaces the
excluded population explicitly instead of folding it into a clean verdict.

Ghost/sourceless documents (registered with no ``physical_collection``) are
the reproducible case: ``CatalogRepository``'s write-time collection stamp
(nexus-x6kdz) returns NULL for a doc with no ``physical_collection``, and
``manifest_backfill()``'s own WHERE clause (``physical_collection IS NOT
NULL AND != ''``) never touches those rows either — so their manifest rows
stay ``collection=NULL`` PERMANENTLY, backfill or no backfill
(catalog-014-manifest-collection-stamp.xml's own changeset comment: "ghost
docs (physical_collection empty) ... are skipped, matching
manifest_backfill()'s semantics"). This is exactly the population
``manifest_orphans()``/``manifest_verify_all()`` filter out before doing
anything else, and it is what ``health._check_manifest_null_collection``
(the C2 fix) reports.

Every test is routed to a real per-test-tenant engine catalog by the autouse
``_pin_t2_substrate`` fixture (tests/conftest.py) — no explicit substrate
fixture request needed (same precedent as
``tests/db/test_du2dw_chash_conformance_report_engine.py``).
"""
from __future__ import annotations

from tests._catalog_fixture_ops import ActiveCatalog, active_reader

_SEQ = [0]


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _chunk(chash: str, position: int) -> dict:
    return {
        "chash": chash, "position": position, "chunk_index": None,
        "line_start": None, "line_end": None, "char_start": None, "char_end": None,
    }


def _never_embedded_chash(seed: int) -> str:
    """A syntactically-valid 64-hex chash with no backing chunks_<dim> row."""
    return f"{seed:064x}"


def _register_ghost(seq: int, label: str):
    """A ghost/sourceless document: no ``physical_collection`` at all."""
    cat = ActiveCatalog()
    owner = cat.register_owner(f"c2-{label}-{seq}", "curator")
    tumbler = cat.register(owner, f"c2 {label} doc {seq}", content_type="knowledge")
    return cat, tumbler


class TestManifestNullCollectionFalseClean:
    def test_ghost_document_manifest_row_reads_collection_null(self) -> None:
        """Baseline: a ghost doc's manifest row genuinely lands with
        ``collection IS NULL`` — the write-time stamp (nexus-x6kdz) has no
        ``physical_collection`` to derive from."""
        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "baseline")

        before = active_reader().manifest_null_collection_report()
        chash = _never_embedded_chash(seq)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)])
        after = active_reader().manifest_null_collection_report()

        assert after["total"] == before["total"] + 1, (
            f"expected the ghost doc's manifest row to land with "
            f"collection IS NULL: before={before} after={after}"
        )

    def test_manifest_backfill_never_covers_ghost_rows(self) -> None:
        """THE GHOST-DOC REFINEMENT, proven empirically: running
        ``manifest_backfill()`` first is NECESSARY BUT NOT SUFFICIENT. A
        ghost document's manifest row survives backfill unchanged — its
        ``physical_collection`` is NULL/empty, so ``manifest_backfill()``'s
        own WHERE clause never selects it."""
        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "ghost-bf")
        chash = _never_embedded_chash(seq + 1_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)])

        before = active_reader().manifest_null_collection_report()
        active_reader().manifest_backfill()  # the documented call-protocol remedy
        after = active_reader().manifest_null_collection_report()

        assert after["total"] == before["total"], (
            f"manifest_backfill() must NOT reduce the ghost-doc population "
            f"— it has no physical_collection to stamp: before={before} "
            f"after={after}"
        )

    def test_dangling_manifest_check_reads_clean_on_ghost_row_with_no_backing_chunk(
        self,
    ) -> None:
        """THE FALSE-CLEAN PROOF: a ghost document's manifest row references
        a chash with NO corresponding chunk row anywhere — genuinely
        missing data — yet ``_check_dangling_manifests`` (backed by
        ``manifest_verify_all``) reports it clean, because the row is
        invisible to that check's SQL before any comparison happens. This
        behavior is UNCHANGED by the C2 fix (the fix adds REPORTING, it does
        not touch ``manifest_verify_all``'s own filter) — pinned so a future
        change to that filter doesn't silently invalidate the premise of
        the null-collection check below.
        """
        import nexus.health as h

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "falseclean")
        chash = _never_embedded_chash(seq + 2_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)])

        results = h._check_dangling_manifests()
        assert results, "expected at least one HealthResult"
        assert all(r.ok for r in results), (
            f"expected _check_dangling_manifests to read clean on an "
            f"invisible (collection-NULL) ghost row: {results}"
        )

    def test_c2_fix_surfaces_the_excluded_population_instead_of_a_silent_clean(
        self,
    ) -> None:
        """THE FIX. With the same setup as the false-clean proof above,
        ``_check_manifest_null_collection`` (new, T2 nexus/chroma-residue-
        plan-2026-08-10 §C2) reports the excluded population explicitly —
        so the COMBINED doctor output for this data is no longer a clean
        sweep. FAILS against pre-fix code (the function does not exist);
        PASSES against the fix.
        """
        import nexus.health as h

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "fix")
        chash = _never_embedded_chash(seq + 3_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)])

        dangling = h._check_dangling_manifests()
        null_collection = h._check_manifest_null_collection()

        combined = dangling + null_collection
        assert not all(r.ok for r in combined), (
            f"expected the combined doctor output to be NON-clean once the "
            f"null-collection check is included — a ghost-doc row exists "
            f"but every check reads clean: {combined}"
        )
        assert len(null_collection) == 1
        nc = null_collection[0]
        assert nc.ok is False
        assert nc.warn is True
        assert "collection IS NULL" in nc.detail
        assert "permanently" in nc.detail.lower() or "ghost" in nc.detail.lower()

    def test_c2_fix_is_read_only_never_calls_backfill(self, monkeypatch) -> None:
        """The check must remain READ-ONLY — it must never invoke the
        WRITE-side ``manifest_backfill()`` as a side effect of a health
        check."""
        import nexus.health as h

        real_cat = active_reader()
        called: list[bool] = []

        class _SpyCat:
            def manifest_null_collection_report(self):
                return real_cat.manifest_null_collection_report()

            def manifest_backfill(self):  # pragma: no cover - must never run
                called.append(True)
                raise AssertionError(
                    "manifest_backfill must not be called by a read-only doctor check"
                )

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: _SpyCat(), raising=False,
        )
        h._check_manifest_null_collection()
        assert not called, "the read-only check invoked the WRITE-side manifest_backfill()"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 nexus/chroma-residue-plan-2026-08-10 §C2 + RDR-191 nexus-71gw2 REBASE.

ORIGINAL PREMISE (pre-71gw2, preserved here for context): a ghost/sourceless
document (registered with no ``physical_collection``) made
``CatalogRepository``'s write-time collection stamp (nexus-x6kdz) return NULL,
and ``manifest_backfill()``'s own WHERE clause (``physical_collection IS NOT
NULL AND != ''``) never touched those rows either — so a ghost doc's manifest
row stayed ``collection=NULL`` PERMANENTLY, invisible to
``manifest_orphans()``/``manifest_verify_all()`` (both filter to
``collection IS NOT NULL`` before doing anything else) and hence invisible to
``nx doctor``'s "dangling manifest chashes" check — the FALSE-CLEAN this
module originally proved and ``health._check_manifest_null_collection`` (the
C2 fix) reported explicitly.

REBASED (RDR-191 nexus-71gw2, ``catalog-025-collection-not-null.xml``):
``catalog_document_chunks.collection`` is now NOT NULL, so the false-clean's
ROOT CAUSE — a manifest row that can carry NULL at all — is structurally
gone.

SECOND REBASE (Hal's FINAL RDR-191 ruling, ``catalog-025-collection-not-
null.xml``'s header, nexus-j862l reconciliation, 2026-08-12): the first
rebase (above) assumed the INSERT-SITE would SKIP a ghost document's
manifest write entirely, resolving/verifying the collection from the
document's own state. Hal's final ruling REJECTED that resolution/skip
design outright (same ruling that rewrote the Java-side
``ManifestCollectionStampTest`` — see its class javadoc for the full
history): ``write_manifest``/``append_manifest_chunks`` now take a
REQUIRED, caller-supplied ``collection`` keyword, validated non-blank at
the CLIENT boundary (``ValueError`` if blank/omitted) and stamped VERBATIM
on every row by the engine (``CatalogRepository.insertManifestChunkRows``)
— with ZERO relationship to the target document's own
``physical_collection`` field. A "ghost" document (registered with no
``physical_collection``) is therefore no longer special to
``write_manifest`` at all: the caller still must supply *some* real
collection string, and once it does, the row LANDS exactly like it would
for any other document — no more per-row SKIP, no more resolution, no more
ambiguity. This module now proves THAT contract for the ghost-document case
specifically (see individual test docstrings for what each one
demonstrates); ``_check_manifest_null_collection``'s 0 stays honest for a
different, purely structural reason now (the column cannot carry NULL at
all, full stop) rather than because a write was skipped.

``_check_manifest_null_collection`` and ``manifest_backfill()`` themselves
are OUT OF SCOPE for this bead (health.py's false-clean replacement is
DEFERRED to RDR-191 Phase 6 — the wire contract two files this bead may not
touch hard-code the field set); this rebase only updates what the ghost-doc
write path now actually does.

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


def _ghost_write_collection(label: str, seq: int) -> str:
    """A real, routable collection name — RDR-191's shipped design has no
    relationship between this value and the target document's own
    ``physical_collection``; any caller-supplied non-blank string works, so
    tests use one shaped like a real ``knowledge__`` collection (routable to
    the 1024-dim table, matching ``test_du2dw_chash_conformance_report_engine.py``'s
    and ``test_heizf_manifest_verify_list_engine.py``'s convention) so the
    dangling-manifest checks exercised below can actually route it.

    nexus-mode-lint (nexus-f1f2x): the ``voyage-context-3`` token below is a
    hoisted string literal, invisible to
    ``tests/test_mode_declarations_are_explicit.py``'s per-function
    ``inspect.getsource`` census because it lives in this helper's body, not
    inline in any calling test. Reason class "string-literal-as-name": the
    token is one segment of a conformant RDR-103 collection-name string
    passed straight to ``write_manifest``'s ``collection`` kwarg; no
    embedder is constructed anywhere in this module (every test here goes
    through the real engine catalog client, never a Voyage call). Registered
    in ``_HOISTED_TOKEN_SITES`` in the lint file.
    """
    return f"knowledge__c2-{label}-{seq}__voyage-context-3__v1"


class TestManifestNullCollectionFalseClean:
    def test_ghost_document_write_manifest_lands_with_caller_supplied_collection(
        self,
    ) -> None:
        """RDR-191 final-ruling rebase of the original baseline (pre-j862l:
        "a ghost doc's write_manifest is a SKIPPED no-op"). That premise is
        gone: the client requires a non-blank ``collection`` kwarg (proven
        separately by ``test_write_manifest_requires_non_blank_collection``
        below); once supplied, the row is written and stamped with EXACTLY
        that value, unconditionally — the document's own (empty)
        ``physical_collection`` plays no part in whether or how the row
        lands. ``manifest_null_collection_report`` stays at 0/0 regardless,
        but now for the STRUCTURAL reason (the column is NOT NULL, so a
        NULL-collection row can never exist to begin with), not because the
        write was skipped."""
        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "baseline")
        collection = _ghost_write_collection("baseline", seq)

        before = active_reader().manifest_null_collection_report()
        chash = _never_embedded_chash(seq)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)
        after = active_reader().manifest_null_collection_report()

        assert after["total"] == before["total"], (
            f"a NULL-collection manifest row is structurally impossible now "
            f"(NOT NULL constraint) — must stay unchanged either way: "
            f"before={before} after={after}"
        )
        rows = active_reader().get_manifest(str(tumbler))
        assert len(rows) == 1, (
            "RDR-191: the caller supplied a real collection, so the row "
            "LANDS — a ghost document is no longer special-cased at the "
            "insert site"
        )
        assert rows[0].chash == chash

    def test_write_manifest_requires_non_blank_collection(self) -> None:
        """RDR-191: the fail-loud half of the contract. This is the direct
        replacement for the retired insert-site SKIP — a ghost document (or
        any document) cannot slip a blank collection past the client
        boundary; the call never reaches the wire at all."""
        import pytest

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "blank-collection")
        chash = _never_embedded_chash(seq + 500_000)
        with pytest.raises(ValueError, match="collection"):
            cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection="")

    def test_manifest_backfill_has_nothing_left_to_cover(self) -> None:
        """RDR-191 rebase: ``manifest_backfill()`` remains a permanent 0-row
        no-op for a ghost document — not because its row is missing (it now
        exists, stamped with the caller-supplied collection at write time),
        but because there is no longer any NULL-collection row anywhere for
        backfill to stamp (RDR-191 plan §7.2 item 3 — manifest_backfill is
        VESTIGIAL under this design, not deleted)."""
        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "ghost-bf")
        collection = _ghost_write_collection("ghost-bf", seq)
        chash = _never_embedded_chash(seq + 1_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)
        assert len(active_reader().get_manifest(str(tumbler))) == 1, (
            "the caller-supplied collection means the row is written, not "
            "skipped"
        )

        before = active_reader().manifest_null_collection_report()
        active_reader().manifest_backfill()  # the documented call-protocol remedy
        after = active_reader().manifest_null_collection_report()

        assert after["total"] == before["total"], (
            f"manifest_backfill() has nothing to stamp for a ghost doc "
            f"either way now — before={before} after={after}"
        )

    def test_dangling_manifest_check_now_catches_a_ghost_documents_bad_reference(
        self,
    ) -> None:
        """RDR-191 rebase of the original FALSE-CLEAN PROOF (INVERTED). Pre-
        j862l, a ghost doc's manifest row landed with ``collection=NULL``
        and was therefore invisible to ``_check_dangling_manifests`` (which
        filters to ``collection IS NOT NULL``) — a false clean. Under the
        shipped design the row is written with a REAL, caller-supplied
        collection, so it is fully visible to that filter; since the chash
        was never backed by a real T3 chunk, the check must now correctly
        flag it as dangling. This is the check WORKING, not a residual gap.
        """
        import nexus.health as h

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "falseclean")
        collection = _ghost_write_collection("falseclean", seq)
        chash = _never_embedded_chash(seq + 2_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)
        assert len(active_reader().get_manifest(str(tumbler))) == 1

        results = h._check_dangling_manifests()
        assert results, "expected at least one HealthResult"
        assert any(not r.ok for r in results), (
            f"expected _check_dangling_manifests to flag the ghost "
            f"document's unbacked chash now that its row carries a real, "
            f"non-NULL collection — it must no longer be invisible: "
            f"{results}"
        )

    def test_c2_check_stays_an_honest_zero_alongside_a_real_dangling_finding(
        self,
    ) -> None:
        """RDR-191 rebase of THE FIX test. ``_check_manifest_null_collection``'s
        own population (rows with ``collection IS NULL``) is STRUCTURALLY
        EMPTY regardless of what else happens in the catalog — it must read
        an honest 0 even in the SAME catalog state where
        ``_check_dangling_manifests`` correctly flags a real problem (the
        ghost document's unbacked chash seeded here, same shape as the
        sibling test above). The two checks are orthogonal now: one proves
        "no blind spot", the other proves "a real defect is still caught" —
        they no longer collapse into a single "everything reads clean"
        assertion the way the pre-rebase version did.
        """
        import nexus.health as h

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "fix")
        collection = _ghost_write_collection("fix", seq)
        chash = _never_embedded_chash(seq + 3_000_000)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)
        assert len(active_reader().get_manifest(str(tumbler))) == 1

        dangling = h._check_dangling_manifests()
        null_collection = h._check_manifest_null_collection()

        assert len(null_collection) == 1
        nc = null_collection[0]
        if nc.detail == "none":
            # The honest, unconditional-zero shape (RDR-191 plan §7.3): no
            # engine-floor gate, no ambiguity — the population is empty.
            assert nc.ok is True
        else:
            # A pre-71gw2 or pre-route engine still in the fleet: ONLY the
            # unavailable/route-floor branches are acceptable here, and all
            # of them report ok=True. Anything else — in particular the
            # ok=False total>0 branch, the false-clean regression this
            # module exists to catch — must fail, not fall through
            # (review-round3: an open else silently accepted it).
            assert nc.ok is True and (
                nc.detail.startswith("skipped (")
                or nc.detail.startswith("informational")
            ), f"unexpected null-collection check shape: ok={nc.ok} detail={nc.detail!r}"

        assert any(not r.ok for r in dangling), (
            f"the ghost document's unbacked chash must still be caught by "
            f"the dangling-manifest check in this same run — a check with "
            f"no blind spot must not ALSO have gone blind to a real "
            f"problem: {dangling}"
        )

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

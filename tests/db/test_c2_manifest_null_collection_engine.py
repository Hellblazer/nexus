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
were OUT OF SCOPE for THIS module's original bead (health.py's false-clean
replacement was DEFERRED to RDR-191 Phase 6); this rebase only updated what
the ghost-doc write path actually does.

RDR-191 PHASE 6 UPDATE (nexus-o8dil.33), 2026-08-15: the deferred work
landed. ``manifest_backfill()`` (client method + SQL function) and
``health._check_dangling_manifests`` are RETIRED — the manifest-chunk FK
makes the dangling state they detected/fixed unreachable by construction.
``_check_manifest_null_collection`` is EXPLICITLY NOT RETIRED (Decision
item 4's own carve-out — the FK does not cover NULL-collection rows under
``MATCH SIMPLE``) and remains this module's live coverage target.

Every test is routed to a real per-test-tenant engine catalog by the autouse
``_pin_t2_substrate`` fixture (tests/conftest.py) — no explicit substrate
fixture request needed (same precedent as
``tests/db/test_du2dw_chash_conformance_report_engine.py``).
"""
from __future__ import annotations

from tests._catalog_fixture_ops import ActiveCatalog, active_reader, fk_dropped_for_dangling_seed

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
    the 1024-dim table, matching
    ``test_du2dw_chash_conformance_report_engine.py``'s convention) so the
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
        # nexus-dbzxb (RDR-191 Phase 5 Python collateral, idiom 3): this
        # chash is DELIBERATELY never backed by a real T3 chunk (see
        # _never_embedded_chash's docstring) — the whole point of this
        # module is a manifest row whose chash has no real content behind
        # it. fk_catalog_chunks_chunk makes that state unreachable via the
        # normal write path; drop the constraint for this one write so the
        # row lands genuinely dangling, exactly as this test needs.
        with fk_dropped_for_dangling_seed():
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

    # test_manifest_backfill_has_nothing_left_to_cover DELETED (RDR-191
    # Phase 6, nexus-o8dil.33): manifest_backfill() — client method AND
    # the nexus.manifest_backfill() SQL function — is RETIRED entirely
    # (catalog-030-retire-manifest-verify.xml). The test's premise (proving
    # it stays a vestigial 0-row no-op) is moot once the callable no longer
    # exists at all.

    # test_dangling_manifest_check_now_catches_a_ghost_documents_bad_reference
    # DELETED (RDR-191 Phase 6, nexus-o8dil.33): called
    # h._check_dangling_manifests(), also RETIRED — the manifest-chunk FK
    # makes the dangling state it detected unreachable BY CONSTRUCTION (the
    # fk_dropped_for_dangling_seed() context manager this test used to
    # simulate a dangling row is itself the tell: seeding this state now
    # requires artificially dropping the constraint that exists precisely
    # to prevent it in production).

    def test_c2_check_stays_an_honest_zero_for_a_ghost_documents_manifest_row(
        self,
    ) -> None:
        """RDR-191 Phase 6 rebase (nexus-o8dil.33) of THE FIX test.
        ``_check_manifest_null_collection``'s own population (rows with
        ``collection IS NULL``) is STRUCTURALLY EMPTY regardless of what
        else happens in the catalog — it must read an honest 0/informational
        even for a ghost document's manifest row with a genuinely dangling
        chash (no backing T3 chunk). The sibling half of this test that
        cross-checked ``_check_dangling_manifests`` catching the same
        dangling chash is DELETED alongside that now-retired check (Decision
        item 4's own explicit carve-out: this check stays, that one goes —
        the exclusion mechanized here rather than left as a comment)."""
        import nexus.health as h

        seq = _next_seq()
        cat, tumbler = _register_ghost(seq, "fix")
        collection = _ghost_write_collection("fix", seq)
        chash = _never_embedded_chash(seq + 3_000_000)
        with fk_dropped_for_dangling_seed():
            cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)
        assert len(active_reader().get_manifest(str(tumbler))) == 1

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

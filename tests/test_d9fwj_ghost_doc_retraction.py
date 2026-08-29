# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d9fwj: ghost-doc manifest retraction must not abandon cleanup.

``_retract_manifest_rows_for_chash`` (RDR-191 GATE-2, nexus-mmkqe/rnqbw)
unconditionally called ``writer.write_manifest(tumbler, remaining,
collection=entry.physical_collection)``. A ghost/sourceless catalog
document — one registered with an EMPTY ``physical_collection`` (a
documented live population; see ``nexus.health``'s null-collection
census) — has ``entry.physical_collection == ""``. Post-GATE-2,
``HttpCatalogClient.write_manifest`` rejects a blank/None ``collection``
client-side with ``ValueError`` (previously the engine inferred it, so a
blank value was silently tolerated).

Two consumer paths, both broken pre-fix:

* ``store_delete_catalog_cleanup`` (path A) — the ``ValueError`` reaches
  its own broad ``except Exception``, which returns an error string
  WITHOUT ever calling ``writer.delete_document`` — the whole catalog
  cleanup is abandoned. Regression vs the pre-GATE-2 behavior, where the
  same call succeeded.
* ``reap_catalog_manifest_for_chashes`` (path B) — a narrower
  ``except Exception`` around the retraction call alone logs and lets
  ``writer.delete_document`` proceed unconditionally afterward, so the
  tombstone still lands, but the manifest row is never retracted and the
  T3 chunk stays anti-join-protected forever.

The fix: guard a blank ``entry.physical_collection`` in
``_retract_manifest_rows_for_chash`` — fall back to a caller-supplied
``expected_collection`` when one is available, else skip retraction
outright (logged, not raised).

REACHABILITY NOTE. Both public callers ALSO carry a pre-existing
collection-scoping guard (nexus-c53hy / nexus-h7nax) that short-circuits
to "nothing to clean"/"skip this chash" whenever a non-``None``
``expected_collection`` does not match ``entry.physical_collection`` —
which, for a ghost, means ANY real non-blank ``expected_collection``
(the only value every current production call site ever passes) blocks
BEFORE retraction is ever attempted. So the literal ValueError
regression is reachable through the two public functions only via
``expected_collection=None`` — a real, precedented calling shape
(documented in both functions' own docstrings, and already used this
way by ``tests/test_5axey_chash_catalog_lookups.py`` /
``tests/test_kmo9h_catalog_gate_census.py`` for "genuinely nothing to
scope to" callers). The tests below exercise exactly that shape for
paths A and B, plus a direct unit test of the fallback-to-
``expected_collection`` behavior on the helper itself (which the two
public functions' own mismatch guards do not currently exercise, but
which the fix decision explicitly calls for as a defensive fallback for
any caller — present or future — that reaches the helper with a usable
scoped collection).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, active_reader, count_documents, seed_manifest_chunks

_REAL_COLLECTION = "knowledge__d9fwj-ghost__bge-base-en-v15-768__v1"


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _chash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _register_ghost_doc(cat: ActiveCatalog, *, title: str, chashes: list[str],
                         collection: str = _REAL_COLLECTION) -> str:
    """Register a GHOST/sourceless catalog document: ``content_type ==
    'knowledge'``, no ``file_path``, and — the defining trait —
    ``physical_collection`` OMITTED (defaults to ``""``; see
    ``HttpCatalogClient.register``'s default).

    Its manifest row(s) still carry a REAL, non-blank collection: on a
    converged install (catalog-025's NOT NULL promotion) a manifest
    write can never store a blank collection value even for a ghost, so
    a ghost's own manifest rows are indistinguishable in shape from any
    other document's — only the OWNING DOCUMENT's ``physical_collection``
    field is blank. ``meta.doc_id`` is stamped with the first chash so
    ``resolve_knowledge_doc_for_chash``'s unambiguous-match filter
    (``content_type == 'knowledge' and not file_path``) finds exactly
    one candidate per chash via the real reverse-manifest lookup
    (``docs_for_chashes``), matching production's resolution path.
    """
    owner = cat.register_owner("d9fwj-ghost", "curator")
    tumbler = cat.register(
        owner, title, content_type="knowledge", meta={"doc_id": chashes[0]},
        # physical_collection deliberately omitted -- defaults to "" (ghost)
    )
    seed_manifest_chunks(collection, chashes)
    cat.append_manifest_chunks(
        str(tumbler),
        [{"chash": c, "position": i} for i, c in enumerate(chashes)],
        collection=collection,
    )
    cat.resync_chunk_count_cache(str(tumbler))
    return str(tumbler)


class TestPathAStoreDeleteCatalogCleanup:
    """``store_delete_catalog_cleanup`` (nexus-b6enc C4) must not abandon
    ``delete_document`` when the resolved entry is a ghost."""

    def test_ghost_doc_still_gets_delete_document_called(self, tmp_path):
        """Pre-fix: the ValueError from _retract_manifest_rows_for_chash
        is caught by this function's own broad except, which returns an
        error string WITHOUT ever calling writer.delete_document — the
        catalog row survives (count_documents() stays 1). Post-fix: the
        retraction is safely skipped, delete_document runs, and the
        catalog row is gone.
        """
        from nexus.catalog.store_hook import store_delete_catalog_cleanup

        cat = ActiveCatalog()
        chash = _chash("d9fwj-ghost-delete-me")
        tumbler = _register_ghost_doc(cat, title="d9fwj Ghost To Delete", chashes=[chash])
        assert count_documents() == 1, "control: the ghost doc must exist before cleanup"

        deleted_tumbler, error = store_delete_catalog_cleanup(chash, expected_collection=None)

        assert error == "", (
            f"store_delete_catalog_cleanup must not report a cleanup error for "
            f"a ghost doc — the ValueError from a blank physical_collection "
            f"must never reach this function's return value. Got: {error!r}"
        )
        assert deleted_tumbler == tumbler
        assert count_documents() == 0, (
            "delete_document must have run: pre-fix, the broad except around "
            "the raised ValueError abandons the whole cleanup before "
            "delete_document is ever called, leaving the ghost row behind."
        )


class TestPathBReapCatalogManifestForChashes:
    """``reap_catalog_manifest_for_chashes`` must retract what it safely
    can and never let a ghost's blank collection raise past its own
    best-effort contract."""

    def test_ghost_doc_tombstone_proceeds_and_retraction_is_skipped_not_raised(
        self, tmp_path,
    ):
        """Pre-fix, the narrow except already keeps delete_document
        running unconditionally (this function's own documented
        best-effort contract) -- so the tombstone succeeding is not
        itself the falsifier. The falsifier is the retraction call
        itself: pre-fix it must raise ValueError (caught by the narrow
        except at the call site); post-fix it must return cleanly
        (the guard skips it, no exception at all). Reverting the fix
        makes this test's own direct call to the retraction helper
        raise instead of returning silently.
        """
        from nexus.catalog.store_hook import (
            _retract_manifest_rows_for_chash,
            reap_catalog_manifest_for_chashes,
        )
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

        cat = ActiveCatalog()
        chash = _chash("d9fwj-ghost-reap-me")
        tumbler = _register_ghost_doc(cat, title="d9fwj Ghost To Reap", chashes=[chash])
        assert count_documents() == 1, "control: the ghost doc must exist before reap"

        # The tombstone half of path B's contract: delete_document runs
        # unconditionally regardless of retraction outcome (documented
        # best-effort contract, unaffected by this fix).
        reap_catalog_manifest_for_chashes([chash], expected_collection=None)
        assert count_documents() == 0, (
            "reap_catalog_manifest_for_chashes must still tombstone the "
            "ghost document even when its own manifest retraction cannot "
            "proceed (best-effort contract, pre-existing and unaffected by "
            "this fix)."
        )

        # The retraction-specific falsifier: call the helper directly on a
        # SECOND ghost fixture (the first's tumbler is already tombstoned)
        # with no expected_collection fallback available. Pre-fix this
        # raises ValueError; post-fix it returns None having logged and
        # skipped.
        chash2 = _chash("d9fwj-ghost-reap-me-2")
        tumbler2 = _register_ghost_doc(cat, title="d9fwj Ghost To Reap 2", chashes=[chash2])
        reader = make_catalog_reader()
        writer = make_catalog_writer()
        try:
            entry = reader.resolve(tumbler2)
            assert entry is not None
            assert entry.physical_collection == "", (
                "control: the fixture must actually be a ghost (blank "
                "physical_collection) for this test to exercise the guard"
            )
            _retract_manifest_rows_for_chash(reader, writer, entry, chash2)
        finally:
            reader.close()
            writer.close()


class TestRetractionHelperExpectedCollectionFallback:
    """Direct unit coverage of ``_retract_manifest_rows_for_chash``'s
    fallback-to-``expected_collection`` behavior (the fix decision's
    second half). Neither public caller's own collection-mismatch guard
    (nexus-c53hy / nexus-h7nax) currently reaches this branch with a
    real ``expected_collection`` — both short-circuit to "nothing to
    do" first whenever a non-``None`` ``expected_collection`` disagrees
    with a ghost's blank ``physical_collection`` — so this test drives
    the helper directly, matching the fix decision's own wording
    ("fall back to the caller-supplied expected collection when one is
    available")."""

    def test_fallback_collection_is_used_to_stamp_the_retained_rows(self, tmp_path):
        from nexus.catalog.store_hook import _retract_manifest_rows_for_chash
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

        cat = ActiveCatalog()
        chash_retract = _chash("d9fwj-fallback-retract")
        chash_keep = _chash("d9fwj-fallback-keep")
        tumbler = _register_ghost_doc(
            cat, title="d9fwj Ghost Fallback", chashes=[chash_retract, chash_keep],
        )

        reader = make_catalog_reader()
        writer = make_catalog_writer()
        try:
            entry = reader.resolve(tumbler)
            assert entry is not None
            assert entry.physical_collection == "", "control: fixture must be a ghost"

            _retract_manifest_rows_for_chash(
                reader, writer, entry, chash_retract,
                expected_collection=_REAL_COLLECTION,
            )

            remaining = reader.get_manifest(tumbler)
            remaining_chashes = {r.chash for r in remaining}
            assert chash_retract not in remaining_chashes, (
                "the targeted chash must have been retracted using the "
                "expected_collection fallback, not skipped"
            )
            assert chash_keep in remaining_chashes, (
                "the untouched chash's row must survive the rewrite"
            )
        finally:
            reader.close()
            writer.close()

    def test_no_fallback_available_skips_without_raising(self, tmp_path):
        from nexus.catalog.store_hook import _retract_manifest_rows_for_chash
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

        cat = ActiveCatalog()
        chash = _chash("d9fwj-fallback-none-available")
        tumbler = _register_ghost_doc(cat, title="d9fwj Ghost No Fallback", chashes=[chash])

        reader = make_catalog_reader()
        writer = make_catalog_writer()
        try:
            entry = reader.resolve(tumbler)
            assert entry is not None
            assert entry.physical_collection == ""

            # Must not raise -- this is the exact call shape that raised
            # ValueError pre-fix.
            _retract_manifest_rows_for_chash(reader, writer, entry, chash)

            remaining = reader.get_manifest(tumbler)
            assert any(r.chash == chash for r in remaining), (
                "with no collection available at all, the row must be "
                "left in place (skipped), not silently dropped"
            )
        finally:
            reader.close()
            writer.close()

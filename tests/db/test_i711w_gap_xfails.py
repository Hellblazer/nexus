# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service-side contract tests for importer-sweep GAP items 13-17 (nexus-i711w.1).

Each test in this module states the CORRECT contract the service catalog owes
and is marked ``xfail(strict=True)`` against the FILED DEFECT bead that keeps
it failing today. The strict marker is the forcing function:

  - while the defect stands, the test xfails and the suite stays green;
  - if the behaviour drifts further, the test's guard asserts fail loudly;
  - when the fix lands, the test XPASSes and FAILS the suite — at that point
    REMOVE THE xfail MARKER, do NOT delete the test. These are the sole
    repo-wide pins on their contracts (the local-catalog tests that used to
    pin them die with the nexus-i711w SQLite deletion).

Items and their beads:

  13  T3-GC tombstone safety, method-level
      (both engine reads) AND verb-level
      (`nx t3 gc` consuming the leaking read) -> nexus-mqd6t
  14  supersede_collection semantics + guards  -> nexus-cecqy, nexus-g8z8n
                                                  (all four g8z8n guards:
                                                  1 unknown old_name, 2
                                                  already-superseded, 3
                                                  dangling new_name, 4
                                                  superseded_at NULL)
  15  resolve_path repo_root recombination
      + curator guard                          -> nexus-5i864
  16  legacy_grandfathered derivation on the
      bare register_collection path            -> nexus-cecqy (scope-corrected
                                                  comment, 2026-07-29)
  17  orphan-chunks audit degradation is
      REPORTED, never silent                   -> nexus-e9ru2 (CLOSED; the pin
                                                  is fully hermetic, so it
                                                  MOVED to the non-integration
                                                  tests/test_i711w_gap_item17_
                                                  orphan_audit.py and runs on
                                                  every push — see the pointer
                                                  comment at the bottom of
                                                  this file)

Fixture stack: same real-engine pattern as tests/db/test_http_catalog_integration.py
(hermetic PG + Liquibase-managed schema + shaded service JAR + HttpCatalogClient).
The module-scoped fixtures are imported from that module; pytest caches
module-scoped fixtures per requesting module, so this file gets its own fresh
PG + service instance and cannot contaminate (or be contaminated by) the
sibling module's data.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_i711w_gap_xfails.py -m integration -q
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# pytest resolves imported fixtures by name (same pattern as
# tests/db/test_http_shape_parity_integration.py). Each module gets its own
# module-scoped instances — fresh PG + fresh service for this file.
from tests.db.test_http_catalog_integration import (  # noqa: F401, PLC2701 — pytest resolves imported fixtures by name
    _ALL_PREREQS,
    _JAR,
    _JAVA,
    _PG_CTL,
    cat,
    pg_instance,
    service,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]


def _ch(seed: str) -> str:
    """Canonical fixture chash: the FULL 64-lowercase-hex sha256 (RDR-180)."""
    return hashlib.sha256(seed.encode()).hexdigest()


# ── item 13: T3-GC verb-level tombstone safety ──────────────────────────────
# nexus-i711w.1 item 13 -> nexus-mqd6t


class TestT3GcTombstoneSafety:
    """A tombstoned document's chunks must vanish from both catalog reads
    that feed T3 GC and search attribution.

    nexus-mqd6t: two engine reads miss the ``deleted_at IS NULL`` filter that
    nexus-23wlw added to their twenty siblings. The old local-catalog test
    (tests/test_catalog_manifest_read_api.py::TestChashesForCollection::
    test_chashes_for_collection_skips_deleted_documents) passed for a
    SUBSTRATE accident — SQLite hard-deletes — not a behavioural guarantee.
    """

    _OWNER_PREFIX = "1.5"

    def _owner(self, cat) -> str:
        cat.register_owner(
            name="i711w-gc-owner",
            owner_type="repo",
            tumbler_prefix=self._OWNER_PREFIX,
        )
        return self._OWNER_PREFIX

    # nexus-i711w.1 item 13 -> nexus-mqd6t (BUG 1)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-mqd6t BUG 1: CatalogRepository.chashesForCollection "
            "(~:1916) joins catalog_documents but has no `deleted_at IS "
            "NULL` predicate, so a tombstoned document's chunks stay in the "
            "returned chash set — the T3 GC alive-set (commands/t3.py:431), "
            "so `nx t3 gc` treats deleted docs' vectors as referenced and "
            "never collects them. STRICT: the engine fix makes this XPASS "
            "and forces the marker out."
        ),
    )
    def test_chashes_for_collection_excludes_tombstoned_doc(self, cat) -> None:
        owner = self._owner(cat)
        coll = "code__i711w-gc1__voyage-code-3__v1"
        t = cat.register(
            owner,
            "GC Tombstone Doc 1",
            content_type="code",
            physical_collection=coll,
            source_uri="file:///i711w/gc1/doc.md",
        )
        h = _ch("i711w-gc1-chunk")
        cat.write_manifest(str(t), [{"chash": h, "position": 0}])

        # NON-VACUITY guard: the chunk is visible while the doc is live.
        assert h in cat.chashes_for_collection(coll), (
            "guard: manifest write must surface the chash before the delete"
        )

        assert cat.delete_document(t) is True

        # CORRECT CONTRACT: a deleted document contributes nothing to the
        # T3 GC alive-set. (Service today: chash still present.)
        assert h not in cat.chashes_for_collection(coll)

    # nexus-i711w.1 item 13 -> nexus-mqd6t (BUG 2)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-mqd6t BUG 2: CatalogRepository.docsForChashes (~:1931) "
            "does not join catalog_documents AT ALL, so there is nothing to "
            "filter — a search hit is attributed to a document the user "
            "deleted (search_engine.py:104 maps result chunks back to docs). "
            "USER-VISIBLE, the worse of the two. STRICT: the engine fix "
            "makes this XPASS and forces the marker out."
        ),
    )
    def test_docs_for_chashes_excludes_tombstoned_doc(self, cat) -> None:
        owner = self._owner(cat)
        coll = "code__i711w-gc2__voyage-code-3__v1"
        t = cat.register(
            owner,
            "GC Tombstone Doc 2",
            content_type="code",
            physical_collection=coll,
            source_uri="file:///i711w/gc2/doc.md",
        )
        h = _ch("i711w-gc2-chunk")
        cat.write_manifest(str(t), [{"chash": h, "position": 0}])

        # NON-VACUITY guard: while live, the chash attributes to the doc.
        before = cat.docs_for_chashes([h])
        assert before.get(h) == [str(t)], (
            "guard: reverse lookup must attribute the chash before the delete"
        )

        assert cat.delete_document(t) is True

        # CORRECT CONTRACT: a deleted document is never attributed.
        after = cat.docs_for_chashes([h])
        assert str(t) not in after.get(h, [])

    # nexus-i711w.1 item 13 -> nexus-mqd6t (VERB level)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-mqd6t (verb level): `nx t3 gc` consumes "
            "chashes_for_collection as its alive-set (commands/t3.py:431), "
            "so BUG 1's missing `deleted_at IS NULL` predicate makes the "
            "verb treat a tombstoned document's chunks as still referenced "
            "and never collect them — the operator-visible consequence of "
            "the leaking read the two method-level tests above pin. STRICT: "
            "the engine fix makes this XPASS and forces the marker out."
        ),
    )
    def test_t3_gc_verb_collects_tombstoned_docs_chunks(
        self, cat, service, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VERB-LEVEL twin of the two method-level pins above.

        What this covers BEYOND the method-level tests: the full CLI path —
        Click parsing of `t3 gc -c <coll> --no-dry-run --yes`, the verb's
        PRODUCTION catalog resolution (`_make_catalog()` ->
        `make_catalog_reader()` in service mode -> an env-resolved
        HttpCatalogClient against THIS module's real engine — no catalog
        patch), the verb's consumption of the real
        ``chashes_for_collection`` read over HTTP as its alive-set, the
        orphan partition, and the batched delete call.

        What it does NOT cover: the engine-hosted pgvector serving leg.
        ``make_t3`` is patched to a real ``T3Database`` over the canonical
        ``InMemoryVectorClient`` substitute — the documented ``make_t3``
        test seam every verb-level test in tests/test_t3_gc.py uses —
        because this module's hermetic engine fixture provisions no
        embedding model and ``upsert_chunks`` embeds SERVER-side, so chunks
        cannot be seeded into the engine's own vector store here. The
        engine-side vector leg has its own gate
        (tests/db/test_http_combined_query_integration.py).
        """
        from unittest.mock import patch as _patch  # noqa: PLC0415 — test-local import

        from click.testing import CliRunner  # noqa: PLC0415 — test-local import

        from nexus.catalog import factory as catalog_factory  # noqa: PLC0415 — test-local import
        from nexus.cli import main  # noqa: PLC0415 — test-local import (heavy CLI import deferred to the one test that drives it)
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction  # noqa: PLC0415 — test-local import
        from nexus.db.t3 import T3Database  # noqa: PLC0415 — test-local import
        from tests.conftest import make_vector_test_client  # noqa: PLC0415 — test-local import

        base_url, token, _proc = service
        owner = self._owner(cat)
        coll = "code__i711w-gc3__voyage-code-3__v1"

        # Live control doc: proves the verb ran and did not blanket-delete.
        t_live = cat.register(
            owner,
            "GC Verb Live Doc",
            content_type="code",
            physical_collection=coll,
            source_uri="file:///i711w/gc3/live.md",
        )
        h_live = _ch("i711w-gc3-live-chunk")
        cat.write_manifest(str(t_live), [{"chash": h_live, "position": 0}])

        # Doomed doc: tombstoned below; its chunk is the GC subject.
        t_dead = cat.register(
            owner,
            "GC Verb Tombstoned Doc",
            content_type="code",
            physical_collection=coll,
            source_uri="file:///i711w/gc3/dead.md",
        )
        h_dead = _ch("i711w-gc3-dead-chunk")
        cat.write_manifest(str(t_dead), [{"chash": h_dead, "position": 0}])

        # Real T3Database over the canonical vector substitute. Both chunks
        # are 60 days old so they clear the default 30d orphan window.
        t3_db = T3Database(
            _client=make_vector_test_client(),
            _ef_override=MiniLMDirectEmbeddingFunction(),
        )
        long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        t3_db._client.get_or_create_collection(coll).add(
            ids=["live-0", "dead-0"],
            documents=["live chunk text", "dead chunk text"],
            metadatas=[
                {"chunk_text_hash": h_live, "indexed_at": long_ago},
                {"chunk_text_hash": h_dead, "indexed_at": long_ago},
            ],
        )

        # NON-VACUITY guards (hold pre- AND post-fix): the manifest surfaces
        # the doomed chash while its doc is live, and the tombstone lands.
        assert h_dead in cat.chashes_for_collection(coll), (
            "guard: manifest write must surface the chash before the delete"
        )
        assert cat.delete_document(t_dead) is True

        # Route the verb's catalog resolution at THIS module's engine through
        # the production factory (env-resolved service mode) — no _make_catalog
        # patch. The shared factory client is reset around the invoke so a
        # client built against this test's env cannot leak (the conftest
        # autouse reset also covers the cross-test direction).
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_URL", base_url)
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        catalog_factory.reset_shared_service_catalog_client_for_tests()
        try:
            with _patch("nexus.db.make_t3", return_value=t3_db):
                result = CliRunner().invoke(
                    main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
                )
        finally:
            catalog_factory.reset_shared_service_catalog_client_for_tests()

        # Guards that hold in BOTH states: the verb completed, and the live
        # doc's chunk survived (gc must never blanket-delete).
        assert result.exit_code == 0, result.output
        surviving = set(
            t3_db._client.get_collection(coll).get()["ids"]
        )
        assert "live-0" in surviving, result.output

        # CORRECT CONTRACT: the tombstoned doc's chunk is an orphan and gets
        # collected. (Service today: h_dead is still in the alive-set the
        # verb reads, so the chunk survives — this is the assertion that
        # xfails.)
        assert "dead-0" not in surviving, (
            "tombstoned document's chunk must be GC'd by the verb; "
            f"gc output:\n{result.output}"
        )


# ── item 14: supersede_collection semantics ─────────────────────────────────
# nexus-i711w.1 item 14 -> nexus-cecqy + nexus-g8z8n


class TestSupersedeCollectionSemantics:
    """The rename -> supersede pair must leave the OLD name present and
    marked superseded — the documented "keeps the event log and projection
    in sync" contract, and a Stage-2 exit criterion (no service-side
    semantic test existed at all; the only one asserted wire shape).
    """

    # nexus-i711w.1 item 14 -> nexus-cecqy
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-cecqy: service-mode rename DELETEs the old "
            "catalog_collections row (CatalogRepository.renameCollectionTxn "
            "step 3, key catalog_collections_deleted — forced by the "
            "fk-002/fk-003 ON UPDATE NO ACTION FKs), so the follow-up "
            "supersede_collection UPDATE matches 0 rows and the old name "
            "ends ABSENT instead of marked-superseded — while the CLI still "
            "echoes 'Emitted CollectionSuperseded'. Correct contract: the "
            "old row survives with superseded_by + non-NULL superseded_at, "
            "visible via get_collection AND list_collections."
        ),
    )
    def test_supersede_after_rename_marks_old_row(self, cat) -> None:
        old = "code__i711w-sup__voyage-code-3__v1"
        new = "code__i711w-sup__voyage-code-3__v2"
        cat.register_collection(
            old,
            content_type="code",
            owner_id="i711w-sup",
            embedding_model="voyage-code-3",
        )
        # NON-VACUITY guard: the old row exists before the rename.
        assert cat.get_collection(old) is not None

        cat.rename_collection(old, new)
        assert cat.get_collection(new) is not None, (
            "guard: rename must have produced the new registry row"
        )

        cat.supersede_collection(old, new)

        # CORRECT CONTRACT: old row present, marked superseded, timestamped.
        old_row = cat.get_collection(old)
        assert old_row is not None, (
            "old collection row must SURVIVE a rename+supersede as the "
            "superseded tombstone (service today: rename deleted it)"
        )
        assert old_row["superseded_by"] == new
        assert old_row.get("superseded_at"), "superseded_at must be non-NULL"

        by_name = {c["name"]: c for c in cat.list_collections()}
        assert old in by_name
        assert by_name[old]["superseded_by"] == new

    # nexus-i711w.1 item 14 -> nexus-g8z8n (guard 4)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-g8z8n guard 4: CatalogRepository.supersedeCollection (:2292-2303) "
            "binds superseded_at ONLY from the request body and "
            "HttpCatalogClient.supersede_collection (:1794-1808) never sends "
            "one, so superseded_at stays NULL — the supersession is "
            "untimestamped and unauditable. Correct contract: a supersede "
            "records a non-NULL superseded_at without the caller having to "
            "supply it (parity with local _WriteOps.supersede_collection, "
            "which stamps datetime.now(UTC))."
        ),
    )
    def test_supersede_records_timestamp_without_caller_supplied_at(
        self, cat
    ) -> None:
        old = "code__i711w-supat__voyage-code-3__v1"
        new = "code__i711w-supat__voyage-code-3__v2"
        cat.register_collection(
            old,
            content_type="code",
            owner_id="i711w-supat",
            embedding_model="voyage-code-3",
        )
        cat.register_collection(
            new,
            content_type="code",
            owner_id="i711w-supat",
            embedding_model="voyage-code-3",
        )

        cat.supersede_collection(old, new)

        row = cat.get_collection(old)
        assert row is not None
        # Passes today (the UPDATE does set superseded_by on a surviving row):
        assert row["superseded_by"] == new
        # Fails today — the actual g8z8n defect:
        assert row.get("superseded_at"), "superseded_at must be non-NULL"

    # nexus-i711w.1 item 14 -> nexus-g8z8n (guard 1)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-g8z8n guard 1: superseding an UNREGISTERED old_name "
            "silently no-ops — the bare UPDATE matches 0 rows and the "
            "handler replies 200 {'updated':0}; HttpCatalogClient discards "
            "the rowcount so nothing raises. Local parity contract "
            "(_WriteOps.supersede_collection): unknown old_name raises "
            "ValueError (the typo-on-explicit-action path must fail loud, "
            "per the no-silent-fallbacks directive)."
        ),
    )
    def test_supersede_unknown_old_name_raises(self, cat) -> None:
        new = "code__i711w-supguard__voyage-code-3__v2"
        cat.register_collection(
            new,
            content_type="code",
            owner_id="i711w-supguard",
            embedding_model="voyage-code-3",
        )
        missing = "code__i711w-nosuch__voyage-code-3__v1"
        assert cat.get_collection(missing) is None  # guard: truly unregistered

        # try/except-then-assert (not pytest.raises) so today's silent no-op
        # fails via AssertionError, matching the marker's `raises=`. An
        # unexpected exception TYPE from a future fix propagates and fails
        # the strict marker loudly instead of being absorbed.
        raised = False
        try:
            cat.supersede_collection(missing, new)
        except ValueError:
            raised = True
        assert raised, (
            "supersede of an unregistered old_name must raise ValueError "
            "(local parity), not silently no-op"
        )

    # nexus-i711w.1 item 14 -> nexus-g8z8n (guard 2)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-g8z8n guard 2: superseding an ALREADY-superseded old row "
            "silently OVERWRITES superseded_by — the bare UPDATE (`WHERE "
            "tenant_id=? AND name=?`, CatalogRepository.java:2292-2303) has "
            "no `superseded_by IS NULL` predicate and the handler replies "
            "200, so a second supersede rewrites the supersession chain "
            "unaudited. Local parity contract (_WriteOps.supersede_collection "
            "catalog_writes.py:270+, nexus-qpet.2): an already-superseded "
            "old_name raises ValueError — 'refusing to chain a second "
            "supersede event' — and the original pointer survives."
        ),
    )
    def test_supersede_already_superseded_old_row_conflicts(self, cat) -> None:
        old = "code__i711w-resup__voyage-code-3__v1"
        new1 = "code__i711w-resup__voyage-code-3__v2"
        new2 = "code__i711w-resup__voyage-code-3__v3"
        for name, ver in ((old, "v1"), (new1, "v2"), (new2, "v3")):
            cat.register_collection(
                name,
                content_type="code",
                owner_id="i711w-resup",
                embedding_model="voyage-code-3",
                model_version=ver,
            )

        cat.supersede_collection(old, new1)

        # NON-VACUITY guard (holds pre- AND post-fix): the precondition state
        # this guard is about — old is ALREADY superseded, by new1 — is real.
        row = cat.get_collection(old)
        assert row is not None
        assert row["superseded_by"] == new1, (
            "guard: first supersede must have landed before probing the "
            "re-supersede path"
        )

        raised = False
        try:
            cat.supersede_collection(old, new2)
        except ValueError:
            raised = True
        assert raised, (
            "supersede of an already-superseded row must conflict loudly "
            "(local parity: ValueError), not silently overwrite the chain"
        )
        # And the ORIGINAL pointer must survive the refused re-supersede.
        assert cat.get_collection(old)["superseded_by"] == new1

    # nexus-i711w.1 item 14 -> nexus-g8z8n (guard 3)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-g8z8n guard 3: superseding TO an UNREGISTERED new_name "
            "writes a DANGLING superseded_by pointer — neither "
            "HttpCatalogClient.supersede_collection (:1794-1808) nor the "
            "handler nor the bare UPDATE validates that superseded_by names "
            "a registered collection, so the pointer resolves to nothing and "
            "no foreign-key-style join can follow it. Local parity contract "
            "(_WriteOps.supersede_collection): unregistered new_name raises "
            "ValueError before anything is written."
        ),
    )
    def test_supersede_unregistered_new_name_raises(self, cat) -> None:
        old = "code__i711w-dangl__voyage-code-3__v1"
        cat.register_collection(
            old,
            content_type="code",
            owner_id="i711w-dangl",
            embedding_model="voyage-code-3",
        )
        missing_new = "code__i711w-dangl-nosuch__voyage-code-3__v2"

        # NON-VACUITY guards: the target really is unregistered, and the old
        # row starts un-superseded (so a dangling write would be observable).
        assert cat.get_collection(missing_new) is None
        row = cat.get_collection(old)
        assert row is not None
        assert not row.get("superseded_by")

        raised = False
        try:
            cat.supersede_collection(old, missing_new)
        except ValueError:
            raised = True
        assert raised, (
            "supersede to an unregistered new_name must raise ValueError "
            "(local parity), not write a dangling superseded_by pointer"
        )
        # And nothing may have been written to the old row.
        assert not cat.get_collection(old).get("superseded_by")


# ── item 15: resolve_path repo_root recombination + curator guard ───────────
# nexus-i711w.1 item 15 -> nexus-5i864


class TestResolvePathServiceParity:
    """HttpCatalogClient.resolve_path must implement the documented local
    resolution order (catalog_docs.py:462+), not a bare
    ``Path(entry.file_path)``.

    These are the service-side forms of the PORT-BLOCKED tests in
    tests/test_catalog_path.py::TestResolvePath — the ONLY repo-wide
    coverage of resolve_path. The service client today (
    http_catalog_client.py:1150-1152) drops the owner lookup entirely:
    no repo_root recombination, no curator guard, no empty-repo_root
    None. Consequence: the auto-linker (link_generator.py:131/:219)
    silently emits ZERO links in service mode from any CWD that is not
    the repo root — or WRONG links if the CWD collides (nexus-3e4s
    contamination class, one layer out).
    """

    # nexus-i711w.1 item 15 -> nexus-5i864 (defect 1: repo_root recombination)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-5i864 defect 1: HttpCatalogClient.resolve_path "
            "(:1150-1152) drops the owner.repo_root recombination, so a "
            "stored RELATIVE file_path comes back as a relative Path and the "
            "caller anchors it on CWD — the nexus-3e4s CWD-anchoring class "
            "(~6,500 contaminated rows last time), with no write-path guard "
            "behind it service-side. Correct contract (catalog_docs.py "
            "resolution order step 5): Path(owner.repo_root) / file_path."
        ),
    )
    def test_relative_file_path_anchors_on_owner_repo_root(self, cat) -> None:
        repo_root = "/i711w/anchored-repo"
        cat.register_owner(
            name="i711w-anchored-repo",
            owner_type="repo",
            repo_root=repo_root,
            tumbler_prefix="1.7",
        )
        t = cat.register(
            "1.7",
            "Anchored Doc",
            content_type="code",
            file_path="src/anchored.py",
            source_uri="file:///i711w/anchored-repo/src/anchored.py",
        )

        resolved = cat.resolve_path(t)
        assert resolved == Path(repo_root) / "src" / "anchored.py"

    # nexus-i711w.1 item 15 -> nexus-5i864 (defect 2: curator guard)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-5i864 defect 2: the curator guard is dropped. "
            "Curator-owned documents (PDFs, standalone docs) must resolve to "
            "None (catalog_docs.py resolution order steps 3-4 — the curator "
            "check precedes even the absolute-path return) but come back as "
            "a bare Path, so the auto-linker will read_text() a PDF and scan "
            "the mojibake for file paths."
        ),
    )
    def test_curator_owner_resolves_none(self, cat) -> None:
        cat.register_owner(
            name="i711w-papers",
            owner_type="curator",
            tumbler_prefix="1.8",
        )
        # Absolute file_path on purpose: makes the assertion unambiguous —
        # only the curator guard (not path recombination) can produce None.
        t = cat.register(
            "1.8",
            "Curator PDF",
            content_type="paper",
            file_path="/i711w/library/paper.pdf",
            source_uri="file:///i711w/library/paper.pdf",
        )

        assert cat.resolve_path(t) is None

    # nexus-i711w.1 item 15 -> nexus-5i864 (defect 1, legacy-owner leg)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-5i864: a repo owner with EMPTY repo_root (pre-RDR-137 "
            "legacy row awaiting its re-index heal) must resolve a relative "
            "file_path to None (catalog_docs.py step 6, with the "
            "catalog_resolve_path_legacy_owner_missing_repo_root DEBUG "
            "breadcrumb); the service client returns the relative Path "
            "instead, which the caller then mis-anchors on CWD."
        ),
    )
    def test_empty_repo_root_owner_resolves_none(self, cat) -> None:
        cat.register_owner(
            name="i711w-legacy-repo",
            owner_type="repo",
            tumbler_prefix="1.9",
        )
        t = cat.register(
            "1.9",
            "Legacy Owner Doc",
            content_type="code",
            file_path="src/legacy.py",
            source_uri="file:///i711w/legacy/src/legacy.py",
        )

        assert cat.resolve_path(t) is None


# ── item 16: legacy_grandfathered derivation ────────────────────────────────
# nexus-i711w.1 item 16 -> nexus-cecqy


class TestLegacyGrandfatheredDerivation:
    """A non-conformant collection name registered through the bare
    ``register_collection(name)`` path must land ``legacy_grandfathered=True``,
    the way the local catalog derives it.
    """

    # nexus-i711w.1 item 16 -> nexus-cecqy (legacy_grandfathered half,
    # scope-corrected comment 2026-07-29)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "nexus-cecqy (legacy_grandfathered half): local "
            "Catalog.register_collection DERIVES legacy_grandfathered = not "
            "is_conformant_collection_name(name) (catalog/catalog.py:1501); "
            "the engine does NO inference (CatalogRepository.java:2171 binds "
            "the literal 0) and HttpCatalogClient.register_collection "
            "defaults the kwarg False (:1681), so every bare-else call site "
            "(indexer.py:791, catalog_cmds/collections.py:138 and :340 — BY "
            "CONSTRUCTION the non-conformant branch) lands non-conformant "
            "names UN-flagged in service mode. Recommended fix per the bead: "
            "derive inside HttpCatalogClient.register_collection when the "
            "caller did not pass the flag explicitly."
        ),
    )
    def test_bare_register_nonconformant_name_flagged_legacy(self, cat) -> None:
        from nexus.corpus import is_conformant_collection_name

        # Legacy 2-segment shape — the vacuity trap named on the bead: the
        # CONFORMANT case is correct-by-accident at the False default, so
        # this test MUST use a non-conformant name.
        name = "docs__i711w-legacy71b"
        assert not is_conformant_collection_name(name)  # non-vacuity guard

        cat.register_collection(name)  # the bare-else call-site shape

        row = cat.get_collection(name)
        assert row is not None
        assert row["legacy_grandfathered"] is True

    def test_conformant_name_not_flagged_legacy(self, cat) -> None:
        """Passing companion (correct today): a CONFORMANT name stays
        un-flagged. Pins the other half of the derivation so a future 'fix'
        that blanket-flags every service registration True also fails."""
        from nexus.corpus import is_conformant_collection_name

        name = "docs__i711w-conf__voyage-context-3__v1"
        assert is_conformant_collection_name(name)

        cat.register_collection(
            name,
            content_type="docs",
            owner_id="i711w-conf",
            embedding_model="voyage-context-3",
        )

        row = cat.get_collection(name)
        assert row is not None
        assert row["legacy_grandfathered"] is False


# ── item 17: orphan-chunks audit degradation must be REPORTED ───────────────
# nexus-i711w.1 item 17 -> nexus-e9ru2 (CLOSED; reporting landed via nexus-kmo9h)
#
# MOVED (2026-07-29, stacked-review HIGH finding):
# TestOrphanAuditServiceModeDegradation now lives in
# tests/test_i711w_gap_item17_orphan_audit.py. The test is fully hermetic
# (monkeypatch-only — no engine, no PG, no network), but this module's
# pytestmark gates everything behind `-m integration` + jar/PG prereqs, so
# leaving it here meant the pin NEVER ran in the routine suite. As a plain
# unit test it now runs on every push.

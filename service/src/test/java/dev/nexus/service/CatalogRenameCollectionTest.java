// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-164 P3 (bead nexus-77vve) — CatalogRepository.renameCollection coherent re-home.
 *
 * <p>Verifies the single transactional service-side collection rename: under the fk-002/fk-003
 * {@code ON UPDATE NO ACTION} FKs, it re-homes every in-Postgres denorm-collection table X-&gt;Y
 * by INSERT-new-registry / re-home-children / DELETE-old-registry (never UPDATEs
 * catalog_collections.name), returns per-table counts, leaves no orphan under the old name,
 * round-trips X-&gt;Y-&gt;X, is tenant-isolated via RLS, and preserves the RDR-162 cross-model
 * COPY branch (target already registered → repoint documents only, both registry rows kept).
 * The chunks-present case is the one the pre-P3 bare-UPDATE rename could not satisfy.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogRenameCollectionTest {

    private static final String TENANT_A = "ren-a";
    private static final String TENANT_B = "ren-b";
    private static final String TENANT_C = "ren-c";
    private static final String OLD  = "knowledge__ren__minilm-l6-v2-384__v1";
    private static final String NEW  = "knowledge__ren__minilm-l6-v2-384__v2";
    private static final String SVC_ROLE = "svc_ren_casc";
    private static final String SVC_PASS = "svc_ren_casc_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));

        // Seed an identical full collection under BOTH tenants (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedFullCollection(su, TENANT_A, OLD);
            seedFullCollection(su, TENANT_B, OLD);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void renameCollection_returnsExactPerTableCounts_chunksPresent() {
        // The chunks-present case: the pre-P3 bare UPDATE catalog_collections.name was blocked
        // by NO-ACTION children; the coherent re-home must succeed and report every table.
        // nexus-h8rf6 wave review: the canonical branch deletes registry row X — the
        // CollectionRegistry cache must evict OLD and mark NEW known.
        dev.nexus.service.db.CollectionRegistry.markKnown(TENANT_A, OLD);
        Map<String, Integer> c = repo.renameCollection(TENANT_A, OLD, NEW);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, OLD))
            .as("registry cache evicted for old name").isFalse();
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, NEW))
            .as("registry cache marked for new name").isTrue();
        assertThat(c.get("catalog_collections_inserted")).as("registry Y inserted").isEqualTo(1);
        // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
        // taxonomy_centroids_384/768/1024 unify into single nexus.chunks /
        // nexus.taxonomy_centroids -- one cascade-count key each, summing what
        // three keys used to report (2+1+1 chunks, 1+1+1 centroids).
        assertThat(c.get("chunks")).as("chunks (unified, 2+1+1 across the three seeded dims)").isEqualTo(4);
        assertThat(c).as("RDR-187: no chash_index leg in the cascade").doesNotContainKey("chash_index");
        assertThat(c.get("topic_assignments")).as("topic_assignments (by source_collection)").isEqualTo(2);
        assertThat(c.get("topics")).as("topics").isEqualTo(1);
        assertThat(c.get("taxonomy_meta")).as("taxonomy_meta (RESTRICT child)").isEqualTo(1);
        assertThat(c.get("taxonomy_centroids")).as("taxonomy_centroids (unified, 1+1+1)").isEqualTo(3);
        assertThat(c.get("document_aspects")).as("document_aspects (both rows doc-rooted, hygiene-001)").isEqualTo(2);
        assertThat(c.get("document_highlights")).as("document_highlights").isEqualTo(1);
        assertThat(c.get("aspect_extraction_queue")).as("aspect_extraction_queue (both rows doc-rooted, hygiene-001)").isEqualTo(2);
        assertThat(c.get("catalog_documents")).as("catalog_documents").isEqualTo(1);
        assertThat(c.get("relevance_log")).as("relevance_log (re-homed, no FK)").isEqualTo(2);
        assertThat(c.get("search_telemetry")).as("search_telemetry (re-homed, no FK)").isEqualTo(2);
        assertThat(c.get("hook_failures")).as("hook_failures (re-homed, no FK)").isEqualTo(1);
        assertThat(c.get("catalog_collections_superseded"))
            .as("registry X retired as a superseded tombstone (nexus-cecqy)").isEqualTo(1);
    }

    @Test @Order(20)
    void renameCollection_noOrphanUnderOldName_allPresentUnderNew() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
            // taxonomy_centroids_384/768/1024 collapse to "chunks" and
            // "taxonomy_centroids" -- one orphan-check each, not three.
            for (String tbl : List.of("chunks", "topics", "taxonomy_meta",
                    "taxonomy_centroids",
                    "document_aspects", "document_highlights", "aspect_extraction_queue",
                    "relevance_log", "search_telemetry")) {
                assertThat(rows(su, "SELECT COUNT(*) FROM nexus." + tbl
                    + " WHERE tenant_id='" + TENANT_A + "' AND collection='" + OLD + "'"))
                    .as("no orphan in " + tbl + " under OLD").isZero();
            }
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("hook_failures orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + OLD + "'")).as("assignment orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + OLD + "'")).as("catalog_documents orphans").isZero();
            // nexus-cecqy: OLD is RETIRED, not deleted — a superseded tombstone that
            // records where the collection went. It carries no children (every table
            // above is asserted empty under OLD) and superseded_by != '' keeps it out of
            // collectionForTuple's live-tuple resolution.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + OLD + "' AND superseded_by='" + NEW + "' AND superseded_at IS NOT NULL"))
                .as("old registry row retired as a tombstone").isEqualTo(1);
            // Present under NEW.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + NEW + "'")).as("new registry row present").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("chunks under NEW (unified, 2+1+1)").isEqualTo(4);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("taxonomy_meta under NEW").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("hook_failures under NEW").isEqualTo(1);
            // Symmetric presence sweep for the remaining re-homed tables.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("document_highlights under NEW").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_aspects WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("document_aspects under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("aspect_extraction_queue under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("centroids under NEW (unified, 1+1+1)").isEqualTo(3);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + NEW + "'")).as("topic_assignments under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.relevance_log WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("relevance_log under NEW").isEqualTo(2);
        }
    }

    @Test @Order(30)
    void renameCollection_isTenantIsolated_tenantBUntouched() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + OLD + "'")).as("tenant B old registry intact").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + NEW + "'")).as("tenant B has no NEW row").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + OLD + "'")).as("tenant B chunks intact under OLD (unified)").isEqualTo(4);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + OLD + "'")).as("tenant B taxonomy_meta intact").isEqualTo(1);
        }
    }

    @Test @Order(40)
    void renameCollection_roundTrip_newBackToOld() throws Exception {
        // Y -> X: tenant A currently lives under NEW; rename it back and confirm the inverse.
        Map<String, Integer> c = repo.renameCollection(TENANT_A, NEW, OLD);
        // nexus-cecqy: X is a TOMBSTONE at this point (the Order(10) rename retired it),
        // so step 1 upserts onto it — the count is still 1, but via DO UPDATE. The revive
        // is the whole point: renaming back onto a retired name brings it to life.
        assertThat(c.get("catalog_collections_inserted")).as("registry X revived").isEqualTo(1);
        assertThat(c.get("chunks")).as("chunks back (unified, 2+1+1)").isEqualTo(4);
        assertThat(c.get("catalog_collections_superseded")).as("registry Y retired").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + NEW + "' AND superseded_by='" + OLD + "'"))
                .as("NEW retired after round-trip").isEqualTo(1);
            // OLD must be REVIVED, not still carrying its own tombstone markers from the
            // forward rename — otherwise the restored collection is invisible to
            // collectionForTuple and the round trip only looks complete.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + OLD + "' AND superseded_by='' AND superseded_at IS NULL"))
                .as("OLD restored and revived").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("chunks restored under OLD (unified)").isEqualTo(4);
            // Back-direction must restore the derived tables too (not just chunks/registry).
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("taxonomy_meta restored").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("document_highlights restored").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("centroids restored (unified)").isEqualTo(3);
        }
    }

    @Test @Order(45)
    void renameCollection_identityBeltMatches_revivesNormally() throws Exception {
        // nexus-2sovp: the ADDITIVE identity belt only ever NARROWS what already
        // succeeds — passing the caller's own correct observation of the target's
        // superseded_by must not change the outcome of a rename that would have
        // succeeded anyway (the existing round-trip test above covers the 3-arg,
        // belt-inert overload; this is the same shape through the 4-arg overload).
        final String a = "knowledge__ren-belt-ok__minilm-l6-v2-384__v1";
        final String b = "knowledge__ren-belt-ok__minilm-l6-v2-384__v2";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, a)
               .execute();
        }
        repo.renameCollection(TENANT_A, a, b); // A -> B; A is now a tombstone, superseded_by=B.
        // Rename back B -> A, threading the belt with the OBSERVED value at A: "b" — the
        // same fact a real caller reading A's row would see.
        Map<String, Integer> c = repo.renameCollection(TENANT_A, b, a, b);
        assertThat(c.get("catalog_collections_inserted")).as("A revived").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + a + "' AND superseded_by=''")).as("A revived and live").isEqualTo(1);
        }
    }

    @Test @Order(46)
    void renameCollection_identityBeltMismatch_refusesEvenThoughEmptinessWouldAllowIt() throws Exception {
        // nexus-2sovp: the belt exists for a caller that does NOT replicate
        // CatalogHandler's own identity pre-check (a future CLI path, migration step, or
        // scheduled repair calling this method directly). Force exactly that: rename
        // A -> B leaves A as an EMPTY tombstone (superseded_by=B) — the pre-existing
        // emptiness check has nothing to object to — then call renameCollection with a
        // WRONG expected value for A's superseded_by. The belt must refuse BY NAME, and
        // the refusal must be additive: it does not touch the emptiness check's own
        // throw, it is a second, independent guard.
        final String a  = "knowledge__ren-belt-bad__minilm-l6-v2-384__v1";
        final String b  = "knowledge__ren-belt-bad__minilm-l6-v2-384__v2";
        final String c2 = "knowledge__ren-belt-bad__minilm-l6-v2-384__v3";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, a)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, c2)
               .execute();
        }
        repo.renameCollection(TENANT_A, a, b); // A -> B; A is now an EMPTY tombstone, superseded_by=B.

        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, c2, a, "not-" + b))
            .as("a stale/wrong observed superseded_by must refuse the revive by name, "
                + "not silently proceed because emptiness alone would have allowed it")
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining(a)
            .hasMessageContaining(b);

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + a + "' AND superseded_by='" + b + "'"))
                .as("the tombstone must survive the refused revive unchanged").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + c2 + "'"))
                .as("the source must be untouched: no half-done rename").isEqualTo(1);
        }
    }

    @Test @Order(50)
    void renameCollection_crossModelCopyBranch_targetExists_repointsDocsAndManifests() throws Exception {
        // RDR-162 regression: pre-register the TARGET (simulating the bge-768 cross-model
        // chunk upsert), then rename. catalog_documents.physical_collection repoints AND
        // (nexus-x6kdz critique HIGH) the manifest's denormalized collection re-homes with
        // it — the old docs-only pin actively green-lit the silent-empty combined-query
        // state (docs at the target, manifests at the source). Both registry rows remain.
        final String src = "code__ren-xm__minilm-l6-v2-384__v1";
        final String tgt = "code__ren-xm__bge-768__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // source registry + a document pointing at it
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, src)
               .execute();
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT_A, "xm-doc-1", "XM Doc", src)
               .execute();
            // target registry already exists (cross-model copy registered it)
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, tgt)
               .execute();
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for the manifest insert below, at BOTH
            // ends: the pre-rename manifest row needs a chunk under src, and
            // renameCollectionTxn's COPY branch (target pre-registered) never
            // touches nexus.chunks itself -- only catalog_documents +
            // catalog_document_chunks (containsOnlyKeys assertion below) -- so the
            // post-rename manifest row (now pointing at tgt) needs its OWN
            // same-chash chunk under tgt too, matching what the REAL cross-model
            // copy this branch models would already have written there.
            byte[] xmChash = "e".repeat(32).getBytes(StandardCharsets.US_ASCII);
            insertChunk384(ctx, TENANT_A, src, xmChash, vector(384));
            insertChunk768(ctx, TENANT_A, tgt, xmChash, vector(768));
            // a manifest row still homed at the SOURCE (the pre-rename state)
            su.createStatement().execute("ALTER TABLE nexus.catalog_document_chunks NO FORCE ROW LEVEL SECURITY");
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                           CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                           CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(TENANT_A, "xm-doc-1", 0, xmChash, src)
               .execute();
            su.createStatement().execute("ALTER TABLE nexus.catalog_document_chunks FORCE ROW LEVEL SECURITY");
        }
        Map<String, Integer> c = repo.renameCollection(TENANT_A, src, tgt);
        assertThat(c).as("cross-model branch re-homes docs AND manifests")
            .containsOnlyKeys("catalog_documents", "catalog_document_chunks");
        assertThat(c.get("catalog_documents")).as("one doc repointed").isEqualTo(1);
        assertThat(c.get("catalog_document_chunks")).as("one manifest row re-homed").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + src + "'")).as("source registry row KEPT").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + tgt + "'")).as("target registry row KEPT").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + tgt + "'")).as("doc now under target").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + src + "'")).as("no doc left under source").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + tgt + "'")).as("manifest row homed at target").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + src + "'")).as("no manifest row left under source").isZero();
        }
    }

    @Test @Order(60)
    void renameCollection_midTransactionFailure_rollsBackEverything() throws Exception {
        // Atomicity regression (replaces the old FK-violation test): the whole re-home is
        // ONE withTenant transaction. Force a mid-sequence failure AFTER the registry INSERT
        // and child re-homes by colliding the search_telemetry PK (tenant_id, ts, query_hash,
        // collection): a row pre-seeded under NEW shares (ts, query_hash) with an OLD row, so
        // UPDATE ...SET collection=NEW collides -> the entire transaction must roll back,
        // leaving OLD fully intact and NEW absent. search_telemetry has no FK, so the NEW-side
        // row needs no NEW registry and does not trip the targetExists cross-model branch.
        final String old = "knowledge__ren-rb__minilm-l6-v2-384__v1";
        final String neu = "knowledge__ren-rb__minilm-l6-v2-384__v2";
        final OffsetDateTime ts = OffsetDateTime.of(2026, 1, 1, 0, 0, 0, 0, java.time.ZoneOffset.UTC);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_C, old)
               .execute();
            insertChunk384(ctx, TENANT_C, old, chashBytes("rbchunk"), vector(384));
            // OLD telemetry row that will try to move to (ts,'collide',NEW)...
            ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS,
                           SEARCH_TELEMETRY.QUERY_HASH, SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT,
                           SEARCH_TELEMETRY.KEPT_COUNT)
               .values(TENANT_C, ts, "collide", old, 1, 1)
               .execute();
            // ...but that PK already exists under NEW -> UPDATE collision mid-transaction.
            ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS,
                           SEARCH_TELEMETRY.QUERY_HASH, SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT,
                           SEARCH_TELEMETRY.KEPT_COUNT)
               .values(TENANT_C, ts, "collide", neu, 9, 9)
               .execute();
        }

        assertThatThrownBy(() -> repo.renameCollection(TENANT_C, old, neu))
            .as("mid-transaction PK collision propagates").isInstanceOf(Exception.class);

        try (Connection su = pg.createConnection("")) {
            // Everything rolled back: OLD intact, NEW registry never created.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_C
                + "' AND name='" + old + "'")).as("OLD registry intact after rollback").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_C
                + "' AND name='" + neu + "'")).as("NEW registry NOT created (rolled back)").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + old + "'")).as("chunk NOT re-homed (rolled back)").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + neu + "'")).as("no chunk under NEW").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.search_telemetry WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + old + "'")).as("OLD telemetry intact").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-34wrg option (c) — CollectionMergeRefused names WHICH table blocked
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void renameCollection_mergeRefusal_namesAuditOnlyTable_gcAudit() throws Exception {
        // A retired target whose ONLY row anywhere is an audit breadcrumb (gc_audit) — the
        // false-refusal shape nexus-34wrg exists to fix: no content, no merge hazard.
        final String src = "knowledge__nrg-audit-src__minilm-l6-v2-384__v1";
        final String tgt = "knowledge__nrg-audit-tgt__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, src)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, tgt)
               .execute();
            ctx.insertInto(GC_AUDIT, GC_AUDIT.TENANT_ID, GC_AUDIT.OPERATION, GC_AUDIT.COLLECTION)
               .values(TENANT_A, "purge", tgt)
               .execute();
        }
        assertThat(repo.supersedeCollection(TENANT_A, tgt, "knowledge__nrg-audit-successor__minilm-l6-v2-384__v1", ""))
            .as("precondition: target retired").isEqualTo(1);

        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, src, tgt))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .as("must NAME gc_audit and identify it as an audit trail entry, not real data")
            .hasMessageContaining("gc_audit")
            .hasMessageContaining("audit trail entry")
            .hasMessageContaining("no content");
    }

    @Test @Order(71)
    void renameCollection_mergeRefusal_namesTheDataTable_whenRealDataExists() throws Exception {
        // The other side of the same message: a retired target that holds REAL data (a
        // document) must be named as such, distinctly from the audit-only case above.
        final String src = "knowledge__nrg-data-src__minilm-l6-v2-384__v1";
        final String tgt = "knowledge__nrg-data-tgt__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, src)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, tgt)
               .execute();
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT_A, "nrg-data-doc", "Doc", tgt)
               .execute();
        }
        assertThat(repo.supersedeCollection(TENANT_A, tgt, "knowledge__nrg-data-successor__minilm-l6-v2-384__v1", ""))
            .as("precondition: target retired").isEqualTo(1);

        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, src, tgt))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .as("must NAME catalog_documents as REAL data, not an audit breadcrumb")
            .hasMessageContaining("real data in 'catalog_documents'");
    }

    @Test @Order(72)
    void renameCollection_trulyEmptyRetiredTarget_revivesSuccessfully() throws Exception {
        // A retired target with NOTHING in any scoped table — the legitimate undo-rename
        // case nexus-34wrg protects: it must succeed, not be refused.
        final String src = "knowledge__nrg-empty-src__minilm-l6-v2-384__v1";
        final String tgt = "knowledge__nrg-empty-tgt__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, src)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_A, tgt)
               .execute();
        }
        assertThat(repo.supersedeCollection(TENANT_A, tgt, src, ""))
            .as("precondition: target retired").isEqualTo(1);

        Map<String, Integer> counts = repo.renameCollection(TENANT_A, src, tgt);
        assertThat(counts).as("truly-empty retired target revives without refusal").isNotNull();
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    /** Seed one full collection (all re-homed lifecycle tables) for {@code tenant}. Superuser; bypasses RLS. */
    private static void seedFullCollection(Connection su, String tenant, String coll) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, coll)
           .execute();
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                       CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
           .values(tenant, "rn-doc-1", "Doc 1", coll)
           .execute();
        // chunks: 2/1/1 across three dims, one unified nexus.chunks table (RDR-191).
        insertChunk384(ctx, tenant, coll, chashBytes("rn384a"), vector(384));
        // RDR-194 P3d (nexus-tk070.p3d): rn384b and rn768a below are REPURPOSED
        // (HexFormat.parseHex(hexChash(...)) identity, not the file's own
        // chashBytes(seed) escape-format shape) to ALSO back the two
        // topic_assignments rows further down via topic_assignments_chunk_fk --
        // reusing two of the four already-seeded chunks rather than minting two
        // more, so the "chunks (unified, 2+1+1)" count assertion elsewhere stays
        // exactly 4. The two encodings are NOT interchangeable (chashBytes(seed)
        // stores raw ASCII bytes of a hex-digit-shaped string via bytea "escape
        // format"; HexFormat.parseHex(...) stores the genuine hex-decoded bytes),
        // so this chunk's chash is no longer chashBytes("rn384b") -- nothing else
        // in this file references it by that name (unlike rn384a, reused by the
        // manifest INSERT below).
        insertChunk384(ctx, tenant, coll, hexChashBytes("rn-doc-1"), vector(384));
        insertChunk768(ctx, tenant, coll, hexChashBytes("rn-doc-2"), vector(768));
        insertChunk1024(ctx, tenant, coll, chashBytes("rn1024a"), vector(1024));
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below -- reuse rn384a's
        // chash (rather than minting a fifth, distinct chunk) so the "chunks
        // (unified, 2+1+1)" count assertion elsewhere stays exactly 4.
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under coll, so stamp the manifest row the same.
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                       CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                       CATALOG_DOCUMENT_CHUNKS.COLLECTION)
           .values(tenant, "rn-doc-1", 0, chashBytes("rn384a"), coll)
           .execute();
        // (chash_index seeds removed — RDR-187/nexus-piwya.9: router dropped)
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + coll).hashCode());
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                       TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
           .values(topicId, tenant, "topic-rn", coll, 0, OffsetDateTime.now(), "pending")
           .execute();
        // taxonomy_meta: 1 (fk-003-4 RESTRICT)
        ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
           .values(tenant, coll)
           .execute();
        // topic_assignments: 2 (source_collection=coll). doc_id is bytea now
        // (nexus-tk070.p3c) — a genuine 64-hex chash, independent of the catalog
        // tumbler string (topic_assignments.doc_id has no FK to catalog_documents).
        // RDR-194 P3d (nexus-tk070.p3d): HexFormat.parseHex here (NOT a bare string
        // literal, which would store the ASCII bytes of the hex STRING via bytea
        // "escape format", never matching a real decoded row) so both rows resolve
        // against the two REPURPOSED chunks seeded above.
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(tenant, hexChashBytes("rn-doc-1"), topicId, "projection", coll, OffsetDateTime.now())
           .execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(tenant, hexChashBytes("rn-doc-2"), topicId, "projection", coll, OffsetDateTime.now())
           .execute();
        // centroids: one per dim, one unified nexus.taxonomy_centroids table (RDR-191).
        // PK is (tenant_id, collection, topic_id) -- three DIFFERENT topic_ids, not
        // the shared `topicId` above (would collide on the unified PK; pre-unification
        // these lived in three separate physical tables and could share one topic_id).
        // hygiene-001 step 9b (nexus-tk070.p6a follow-on): label is NOT NULL now,
        // no default -- supply it explicitly.
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_384)
           .values(tenant, coll, topicId, "", vector(384))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_768)
           .values(tenant, coll, topicId + 1, "", vector(768))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_1024)
           .values(tenant, coll, topicId + 2, "", vector(1024))
           .execute();
        // document_aspects: 2, both doc-rooted at rn-doc-1 (the collection's only
        // registered catalog_documents row -- catalog_documents count elsewhere in
        // this fixture is asserted ==1, so a second row cannot be introduced here).
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): doc_id/source_uri are both
        // NOT NULL now, reversing fk-001-2's nullable conversion -- the second row
        // used to be DOC-LESS (doc_id=NULL); that state is no longer representable,
        // so it is now a second row against the same doc, distinguished by source_path.
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
           .values(tenant, coll, "/p/a1.md", OffsetDateTime.now(), "v1", "docling", "rn-doc-1", "file:///p/a1.md")
           .execute();
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
           .values(tenant, coll, "/p/a2.md", OffsetDateTime.now(), "v1", "docling", "rn-doc-1", "file:///p/a2.md")
           .execute();
        // document_highlights: 1. hygiene-001 step 3: source_uri is NOT NULL now too.
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                       DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                       DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
           .values(tenant, "rn-doc-1", coll, "file:///rn-doc-1-hl", "hi", OffsetDateTime.now())
           .execute();
        // aspect_extraction_queue: 2, both doc-rooted at rn-doc-1 (hygiene-001 step 2:
        // doc_id is NOT NULL now -- see the document_aspects comment above for why
        // the second row is no longer DOC-LESS).
        ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                       ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                       ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
           .values(tenant, coll, "/p/q1.md", "pending", OffsetDateTime.now(), "rn-doc-1")
           .execute();
        ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                       ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                       ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
           .values(tenant, coll, "/p/q2.md", "pending", OffsetDateTime.now(), "rn-doc-1")
           .execute();
        // search_telemetry: 2 (no FK, but re-homed)
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                       SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT)
           .values(tenant, OffsetDateTime.now(), "qh1", coll, 10, 5)
           .execute();
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                       SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT)
           .values(tenant, OffsetDateTime.now(), "qh2", coll, 8, 4)
           .execute();
        // hook_failures: 1 (no FK, but re-homed)
        ctx.insertInto(HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID, HOOK_FAILURES.COLLECTION,
                       HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR, HOOK_FAILURES.OCCURRED_AT)
           .values(tenant, "rn-doc-1", coll, "post_store", "boom", OffsetDateTime.now())
           .execute();
        // relevance_log: 2 (no FK, but re-homed — RDR-164 §Approach Phase 3 third audit table)
        // nexus-lgdel.l1: chunk_id must be canonical 64-hex TEXT now
        // (relevance_log_chunk_id_canonical_check, legacy-001-drop-chash-
        // alias.xml) — 'ch1'/'ch2' no longer pass. NOT the chashBytes(seed) helper
        // below: that produces a 32-ASCII-char bytea value for the chunks.chash
        // column; chunk_id here is TEXT and needs a real 64-hex STRING.
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                       RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                       RELEVANCE_LOG.TIMESTAMP)
           .values(tenant, "q1", "0b2ace5def2ecf1234ae0db2a062b83fe40dd330121844b37bc8bdfb6b2f3ea5", coll, "click",
                   "s1", OffsetDateTime.now())
           .execute();
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                       RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                       RELEVANCE_LOG.TIMESTAMP)
           .values(tenant, "q2", "f7088d7f354fadfc6fe69df2f0a9f2057a715e9a62672258a88ee200e72f1c22", coll, "skip",
                   "s1", OffsetDateTime.now())
           .execute();
    }

    /** RDR-191 (nexus-o8dil.48): chunks_384/768/1024 unified into nexus.chunks -- one
     *  insert helper per dim since jOOQ's typed column list is fixed at compile time. */
    private static void insertChunk384(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    private static void insertChunk768(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_768)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    private static void insertChunk1024(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_1024)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    /** A pgvector value with every one of {@code dim} components equal to {@code 0.1}. */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /** 32-char hex-alphabet label, stored as its own ASCII bytes (no real hash semantics --
     *  matches the pre-conversion raw-SQL behavior of a bare string literal into a
     *  {@code bytea} column via PostgreSQL's escape-format input, {@link #hexChash} below is
     *  the genuine-hash counterpart). */
    private static byte[] chashBytes(String seed) {
        String label = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return label.getBytes(StandardCharsets.US_ASCII);
    }

    private static byte[] hexChashBytes(String seed) {
        return java.util.HexFormat.of().parseHex(hexChash(seed));
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c), unlike {@link #chashBytes} above which is a
     *  32-char synthetic id used only for the chunks/manifest {@code chash} column. */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static int rows(Connection su, String sql) throws Exception {
        var rs = su.createStatement().executeQuery(sql);
        rs.next();
        return rs.getInt(1);
    }
}

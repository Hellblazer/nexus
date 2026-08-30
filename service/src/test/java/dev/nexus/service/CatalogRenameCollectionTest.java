// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + a + "')");
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
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + a + "')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + c2 + "')");
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
            // source registry + a document pointing at it
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + src + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT_A + "', 'xm-doc-1', 'XM Doc', '" + src + "')");
            // target registry already exists (cross-model copy registered it)
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + tgt + "')");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for the manifest insert below, at BOTH
            // ends: the pre-rename manifest row needs a chunk under src, and
            // renameCollectionTxn's COPY branch (target pre-registered) never
            // touches nexus.chunks itself -- only catalog_documents +
            // catalog_document_chunks (containsOnlyKeys assertion below) -- so the
            // post-rename manifest row (now pointing at tgt) needs its OWN
            // same-chash chunk under tgt too, matching what the REAL cross-model
            // copy this branch models would already have written there.
            su.createStatement().execute("INSERT INTO nexus.chunks "
                + "(tenant_id, collection, chash, chunk_text, embedding_384) VALUES "
                + "('" + TENANT_A + "', '" + src + "', '" + "e".repeat(32) + "', 'text', "
                + "('[" + "0.1,".repeat(383) + "0.1]')::vector)");
            su.createStatement().execute("INSERT INTO nexus.chunks "
                + "(tenant_id, collection, chash, chunk_text, embedding_768) VALUES "
                + "('" + TENANT_A + "', '" + tgt + "', '" + "e".repeat(32) + "', 'text', "
                + "('[" + "0.1,".repeat(767) + "0.1]')::vector)");
            // a manifest row still homed at the SOURCE (the pre-rename state)
            su.createStatement().execute("ALTER TABLE nexus.catalog_document_chunks NO FORCE ROW LEVEL SECURITY");
            su.createStatement().execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + TENANT_A + "', 'xm-doc-1', 0, '" + "e".repeat(32) + "', '" + src + "')");
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
        final String ts = "2026-01-01 00:00:00+00";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_C + "', '" + old + "')");
            su.createStatement().execute(chunkInsert(TENANT_C, old, 384, "rbchunk"));
            // OLD telemetry row that will try to move to (ts,'collide',NEW)...
            su.createStatement().execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
                + "VALUES ('" + TENANT_C + "', '" + ts + "', 'collide', '" + old + "', 1, 1)");
            // ...but that PK already exists under NEW -> UPDATE collision mid-transaction.
            su.createStatement().execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
                + "VALUES ('" + TENANT_C + "', '" + ts + "', 'collide', '" + neu + "', 9, 9)");
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
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + src + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + tgt + "')");
            su.createStatement().execute("INSERT INTO nexus.gc_audit (tenant_id, operation, collection) VALUES ('"
                + TENANT_A + "', 'purge', '" + tgt + "')");
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
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + src + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + tgt + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT_A + "', 'nrg-data-doc', 'Doc', '" + tgt + "')");
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
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + src + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + tgt + "')");
        }
        assertThat(repo.supersedeCollection(TENANT_A, tgt, src, ""))
            .as("precondition: target retired").isEqualTo(1);

        Map<String, Integer> counts = repo.renameCollection(TENANT_A, src, tgt);
        assertThat(counts).as("truly-empty retired target revives without refusal").isNotNull();
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    /** Seed one full collection (all re-homed lifecycle tables) for {@code tenant}. Superuser; bypasses RLS. */
    private static void seedFullCollection(Connection su, String tenant, String coll) throws Exception {
        var st = su.createStatement();
        st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + coll + "')");
        st.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', 'Doc 1', '" + coll + "')");
        // chunks: 2/1/1 across three dims, one unified nexus.chunks table (RDR-191).
        st.execute(chunkInsert(tenant, coll, 384, "rn384a"));
        // RDR-194 P3d (nexus-tk070.p3d): rn384b and rn768a below are REPURPOSED
        // (decode(hexChash(...),'hex') identity, not the file's own chash(seed)
        // escape-format shape) to ALSO back the two topic_assignments rows
        // further down via topic_assignments_chunk_fk -- reusing two of the four
        // already-seeded chunks rather than minting two more, so the "chunks
        // (unified, 2+1+1)" count assertion elsewhere stays exactly 4. The two
        // encodings are NOT interchangeable (chash(seed) stores raw ASCII bytes
        // of a hex-digit-shaped string via bytea "escape format"; decode(...,
        // 'hex') stores the genuine hex-decoded bytes), so this chunk's chash is
        // no longer chash("rn384b") -- nothing else in this file references it
        // by that name (unlike rn384a, reused by the manifest INSERT below).
        st.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
            + "VALUES ('" + tenant + "', '" + coll + "', decode('" + hexChash("rn-doc-1") + "', 'hex'), 'text', " + vec(384) + "::vector)");
        st.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768) "
            + "VALUES ('" + tenant + "', '" + coll + "', decode('" + hexChash("rn-doc-2") + "', 'hex'), 'text', " + vec(768) + "::vector)");
        st.execute(chunkInsert(tenant, coll, 1024, "rn1024a"));
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below -- reuse rn384a's
        // chash (rather than minting a fifth, distinct chunk) so the "chunks
        // (unified, 2+1+1)" count assertion elsewhere stays exactly 4.
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under coll, so stamp the manifest row the same.
        st.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', 0, '" + chash("rn384a") + "', '" + coll + "')");
        // (chash_index seeds removed — RDR-187/nexus-piwya.9: router dropped)
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + coll).hashCode());
        st.execute("INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES (" + topicId + ", '" + tenant + "', 'topic-rn', '" + coll + "', 0, NOW(), 'pending')");
        // taxonomy_meta: 1 (fk-003-4 RESTRICT)
        st.execute("INSERT INTO nexus.taxonomy_meta (tenant_id, collection) VALUES ('" + tenant + "', '" + coll + "')");
        // topic_assignments: 2 (source_collection=coll). doc_id is bytea now
        // (nexus-tk070.p3c) — a genuine 64-hex chash, independent of the catalog
        // tumbler string (topic_assignments.doc_id has no FK to catalog_documents).
        // RDR-194 P3d (nexus-tk070.p3d): decode(...,'hex') here (NOT a bare string
        // literal, which would store the ASCII bytes of the hex STRING via bytea
        // "escape format", never matching a real decode()'d chunks row) so both
        // rows resolve against the two REPURPOSED chunks seeded above.
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', decode('" + hexChash("rn-doc-1") + "', 'hex'), " + topicId + ", 'projection', '" + coll + "', NOW())");
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', decode('" + hexChash("rn-doc-2") + "', 'hex'), " + topicId + ", 'projection', '" + coll + "', NOW())");
        // centroids: one per dim, one unified nexus.taxonomy_centroids table (RDR-191).
        // PK is (tenant_id, collection, topic_id) -- three DIFFERENT topic_ids, not
        // the shared `topicId` above (would collide on the unified PK; pre-unification
        // these lived in three separate physical tables and could share one topic_id).
        // hygiene-001 step 9b (nexus-tk070.p6a follow-on): label is NOT NULL now,
        // no default -- supply it explicitly.
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, label, embedding_384) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + topicId + ", '', " + vec(384) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, label, embedding_768) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + (topicId + 1) + ", '', " + vec(768) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, label, embedding_1024) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + (topicId + 2) + ", '', " + vec(1024) + "::vector)");
        // document_aspects: 2, both doc-rooted at rn-doc-1 (the collection's only
        // registered catalog_documents row -- catalog_documents count elsewhere in
        // this fixture is asserted ==1, so a second row cannot be introduced here).
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): doc_id/source_uri are both
        // NOT NULL now, reversing fk-001-2's nullable conversion -- the second row
        // used to be DOC-LESS (doc_id=NULL); that state is no longer representable,
        // so it is now a second row against the same doc, distinguished by source_path.
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id, source_uri) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/a1.md', NOW(), 'v1', 'docling', 'rn-doc-1', 'file:///p/a1.md')");
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id, source_uri) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/a2.md', NOW(), 'v1', 'docling', 'rn-doc-1', 'file:///p/a2.md')");
        // document_highlights: 1. hygiene-001 step 3: source_uri is NOT NULL now too.
        st.execute("INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, source_uri, highlights_md, ingested_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', '" + coll + "', 'file:///rn-doc-1-hl', 'hi', NOW())");
        // aspect_extraction_queue: 2, both doc-rooted at rn-doc-1 (hygiene-001 step 2:
        // doc_id is NOT NULL now -- see the document_aspects comment above for why
        // the second row is no longer DOC-LESS).
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/q1.md', 'pending', NOW(), 'rn-doc-1')");
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/q2.md', 'pending', NOW(), 'rn-doc-1')");
        // search_telemetry: 2 (no FK, but re-homed)
        st.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
            + "VALUES ('" + tenant + "', NOW(), 'qh1', '" + coll + "', 10, 5)");
        st.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
            + "VALUES ('" + tenant + "', NOW(), 'qh2', '" + coll + "', 8, 4)");
        // hook_failures: 1 (no FK, but re-homed)
        st.execute("INSERT INTO nexus.hook_failures (tenant_id, doc_id, collection, hook_name, error, occurred_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', '" + coll + "', 'post_store', 'boom', NOW())");
        // relevance_log: 2 (no FK, but re-homed — RDR-164 §Approach Phase 3 third audit table)
        // nexus-lgdel.l1: chunk_id must be canonical 64-hex TEXT now
        // (relevance_log_chunk_id_canonical_check, legacy-001-drop-chash-
        // alias.xml) — 'ch1'/'ch2' no longer pass. NOT the chash(seed) helper
        // below: that produces a 32-ASCII-char string relying on Postgres's
        // bytea escape-literal cast (32 chars -> 32 bytes) for the chunks.chash
        // BYTEA column; chunk_id here is TEXT and needs a real 64-hex STRING.
        st.execute("INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
            + "VALUES ('" + tenant + "', 'q1', '0b2ace5def2ecf1234ae0db2a062b83fe40dd330121844b37bc8bdfb6b2f3ea5', "
            + "'" + coll + "', 'click', 's1', NOW())");
        st.execute("INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
            + "VALUES ('" + tenant + "', 'q2', 'f7088d7f354fadfc6fe69df2f0a9f2057a715e9a62672258a88ee200e72f1c22', "
            + "'" + coll + "', 'skip', 's1', NOW())");
    }

    /** RDR-191 (nexus-o8dil.48): chunks_384/768/1024 unified into nexus.chunks --
     *  {@code dim} now selects the target embedding_&lt;dim&gt; column, not a table. */
    private static String chunkInsert(String tenant, String coll, int dim, String seed) {
        return "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_" + dim + ") "
            + "VALUES ('" + tenant + "', '" + coll + "', '" + chash(seed) + "', 'text', " + vec(dim) + "::vector)";
    }

    private static String vec(int dim) {
        return IntStream.range(0, dim).mapToObj(i -> "0.1").collect(Collectors.joining(",", "'[", "]'"));
    }

    private static String chash(String seed) {
        return (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c), unlike {@link #chash} above which is a 32-char
     *  synthetic id used only for the chunks/manifest {@code chash} column. */
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

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
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P2 (bead nexus-ybdoc) — CatalogRepository.deleteCollection ordered-DELETE cascade.
 *
 * <p>Verifies the single transactional service-side collection delete: it purges every
 * in-Postgres lifecycle table in dependency order (registry row last, RESTRICT FKs as a
 * safety net), returns per-table counts, leaves no orphans, and is tenant-isolated via RLS.
 * Two regression anchors: nexus-tquoj (aspect_extraction_queue purged by its `collection`
 * tag, not via any doc_id-keyed cascade the fk-001 document delete would otherwise have to
 * reach through — originally proven with a doc-less NULL-doc_id row; hygiene-001 step 2
 * (nexus-tk070.p6a follow-on) makes doc_id NOT NULL, so the fixture now proves the same
 * purge-by-collection property with a second doc-rooted row instead) and nexus-cugrk
 * (taxonomy_centroids_* purged by collection — no FK to topics).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogDeleteCollectionCascadeTest {

    private static final String TENANT_A = "del-casc-a";
    private static final String TENANT_B = "del-casc-b";
    private static final String COLL = "knowledge__del-casc__voyage-context-3__v1";
    private static final String SVC_ROLE = "svc_del_casc";
    private static final String SVC_PASS = "svc_del_casc_pass";

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

        // Seed an identical collection under BOTH tenants (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedFullCollection(su, TENANT_A);
            seedFullCollection(su, TENANT_B);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void deleteCollection_returnsExactPerTableCounts() {
        // nexus-h8rf6 wave review: a pre-delete CollectionRegistry entry must be
        // evicted by the delete — a stale entry would make later writers skip
        // re-registration if the collection name is reused.
        dev.nexus.service.db.CollectionRegistry.markKnown(TENANT_A, COLL);
        Map<String, Integer> counts = repo.deleteCollection(TENANT_A, COLL);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, COLL))
            .as("registry cache evicted post-delete").isFalse();
        // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
        // taxonomy_centroids_384/768/1024 are unified into single nexus.chunks /
        // nexus.taxonomy_centroids tables -- the cascade-count keys collapse from
        // three to one each, and the per-table count sums what three fixture
        // rows (2 + 1 + 1, one per dim) used to split across three keys.
        assertThat(counts.get("chunks")).as("chunks (unified, 2+1+1 across the three seeded dims)").isEqualTo(4);
        assertThat(counts).as("RDR-187: no chash_index leg in the cascade").doesNotContainKey("chash_index");
        assertThat(counts.get("topic_assignments")).as("topic_assignments (by source_collection)").isEqualTo(2);
        assertThat(counts.get("topics")).as("topics").isEqualTo(1);
        assertThat(counts.get("taxonomy_meta")).as("taxonomy_meta (RESTRICT child)").isEqualTo(1);
        // CROSS-LANGUAGE CONTRACT (T2 nexus/rdr-191-batch-E3-2026-08-13 [22462]):
        // collection_rename.py / collection_purge.py read this key literally as
        // "taxonomy_centroids" -- must not regress to a per-dim key shape.
        assertThat(counts.get("taxonomy_centroids")).as("taxonomy_centroids (unified, cugrk, 1+1+1)").isEqualTo(3);
        assertThat(counts.get("document_aspects")).as("document_aspects (both rows doc-rooted, hygiene-001)").isEqualTo(2);
        assertThat(counts.get("document_highlights")).as("document_highlights").isEqualTo(1);
        assertThat(counts.get("aspect_extraction_queue")).as("aspect_extraction_queue (tquoj, both rows doc-rooted, hygiene-001)").isEqualTo(2);
        assertThat(counts.get("catalog_documents")).as("catalog_documents").isEqualTo(1);
        assertThat(counts.get("catalog_collections")).as("registry row").isEqualTo(1);
    }

    @Test @Order(20)
    void deleteCollection_leavesNoOrphansForTenantA() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
            // taxonomy_centroids_384/768/1024 collapse to "chunks" and
            // "taxonomy_centroids" -- one orphan-check each, not three.
            for (String tbl : List.of("chunks", "topics", "taxonomy_meta",
                    "taxonomy_centroids",
                    "document_aspects", "document_highlights", "aspect_extraction_queue")) {
                assertThat(rows(su, "SELECT COUNT(*) FROM nexus." + tbl
                    + " WHERE tenant_id='" + TENANT_A + "' AND collection='" + COLL + "'"))
                    .as("no orphan rows in " + tbl).isZero();
            }
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + COLL + "'")).as("assignment orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + COLL + "'")).as("catalog_documents orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TENANT_A
                + "' AND doc_id='dc-doc-1'")).as("manifest rows cascaded via catalog_documents").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + COLL + "'")).as("registry row gone").isZero();
        }
    }

    @Test @Order(30)
    void deleteCollection_isTenantIsolated_tenantBUntouched() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + COLL + "'")).as("tenant B registry intact").isEqualTo(1);
            // RDR-191 (nexus-o8dil.48): nexus.chunks unified -- all 4 seeded rows
            // (2x embedding_384 + 1x embedding_768 + 1x embedding_1024) are one
            // table now, not 2 in chunks_384 alone.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B chunks intact").isEqualTo(4);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B queue intact").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B centroids intact").isEqualTo(3);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B taxonomy_meta intact").isEqualTo(1);
        }
    }

    // ── F8d: collection-cascade scoping asymmetry (nexus-o8dil.40) ────────────

    private static final String TENANT_F8D = "del-casc-f8d";
    private static final String COLL_HOME = "knowledge__del-casc-f8d-home__voyage-context-3__v1";
    private static final String COLL_DEL  = "knowledge__del-casc-f8d-del__voyage-context-3__v1";

    /**
     * RDR-191 F8d: {@code deleteCollectionTxn} scopes T3 chunk deletion by
     * {@code chunks_*.collection} (step 1) but reaches the manifest ONLY via
     * the {@code catalog_documents} cascade, scoped by {@code
     * physical_collection} (step 6/fk-001). A manifest row whose OWN {@code
     * collection} column names the collection being deleted, but whose
     * PARENT DOCUMENT is homed in a DIFFERENT collection, sits outside both
     * scopes as currently wired: its chunk (scoped by chunks.collection) IS
     * deleted in step 1, but its manifest row (parent doc homed elsewhere)
     * is NOT reached by step 6's cascade -- a dangling manifest row,
     * produced by the very operation meant to clean up after itself. This
     * reproduces exactly that state and asserts the manifest row does not
     * survive the deletion of the collection it (denormalized-)names.
     */
    @Test @Order(40)
    void deleteCollection_scopingAsymmetry_manifestRowNamingDeletedCollection_alsoRemoved() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_F8D, COLL_HOME)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_F8D, COLL_DEL)
               .execute();
            // The document is homed in COLL_HOME.
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT_F8D, "f8d-doc", "F8D Doc", COLL_HOME)
               .execute();
            // Its chunk content lives in COLL_DEL (the collection about to be deleted).
            // RDR-191 (nexus-o8dil.48): nexus.chunks unified -- embedding_1024
            // replaces the bare embedding column now that chunks_1024 is retired.
            insertChunk1024(ctx, TENANT_F8D, COLL_DEL, chashBytes("f8dasym"), vector(1024));
            // The manifest row's OWN denormalized `collection` names COLL_DEL
            // too (drifted from the doc's real physical_collection -- the
            // exact shape F8c's reclassification-without-reindex leaves
            // behind), so it is reachable by neither scope symmetrically.
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                           CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                           CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(TENANT_F8D, "f8d-doc", 0, chashBytes("f8dasym"), COLL_DEL)
               .execute();
        }

        repo.deleteCollection(TENANT_F8D, COLL_DEL);

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND doc_id='f8d-doc' AND collection='" + COLL_DEL + "'"))
                .as("F8d: a manifest row denormalized to the deleted collection must not "
                    + "survive though its owning document is homed elsewhere")
                .isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND collection='" + COLL_DEL + "'"))
                .as("the T3 chunk content for the deleted collection is gone").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND tumbler='f8d-doc'"))
                .as("the owning document, homed in a DIFFERENT collection, must "
                    + "survive the deletion of COLL_DEL")
                .isEqualTo(1);
        }
    }

    // ── catalog_links cascade via the PRODUCTION call path (nexus-tk070.p1 ──
    // review fix 2, RDR-194 § D2, critic S2): existing cascade coverage
    // above deletes via raw superuser SQL (bypassing the tenantScope
    // GUC/RLS context deleteCollectionTxn runs under); this seeds and reads
    // through the repository API instead, and drives the delete through
    // repo.deleteCollection -- the SAME production call path deleteCollectionTxn
    // is invoked from -- so the fk_catalog_links_from_document/_to_document
    // cascade is exercised under real RLS, not just raw SQL.

    private static final String TENANT_LINK = "del-casc-links";
    private static final String COLL_LINK_HOME  = "knowledge__del-casc-links-home__voyage-context-3__v1";
    private static final String COLL_LINK_OTHER = "knowledge__del-casc-links-other__voyage-context-3__v1";

    @Test @Order(50)
    void deleteCollection_cascadesCatalogLinks_viaProductionCallPath() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_LINK, COLL_LINK_HOME)
               .execute();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(TENANT_LINK, COLL_LINK_OTHER)
               .execute();
        }

        // dl-src is homed in the collection about to be deleted; dl-dst and
        // dl-survivor2 are homed elsewhere and must survive.
        repo.upsertDocument(TENANT_LINK, Map.of(
            "tumbler", "dl-src", "title", "src", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLL_LINK_HOME));
        repo.upsertDocument(TENANT_LINK, Map.of(
            "tumbler", "dl-dst", "title", "dst", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLL_LINK_OTHER));
        repo.upsertDocument(TENANT_LINK, Map.of(
            "tumbler", "dl-survivor2", "title", "survivor2", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLL_LINK_OTHER));

        // One link FROM the doomed doc, one link TO it (both directions of the
        // FK exercised), and one link entirely OUTSIDE the deleted collection
        // that must survive.
        assertThat(repo.upsertLink(TENANT_LINK, Map.of(
            "from_tumbler", "dl-src", "to_tumbler", "dl-dst",
            "link_type", "cites", "created_by", "test"))).isTrue();
        assertThat(repo.upsertLink(TENANT_LINK, Map.of(
            "from_tumbler", "dl-dst", "to_tumbler", "dl-src",
            "link_type", "relates", "created_by", "test"))).isTrue();
        assertThat(repo.upsertLink(TENANT_LINK, Map.of(
            "from_tumbler", "dl-dst", "to_tumbler", "dl-survivor2",
            "link_type", "cites", "created_by", "test"))).isTrue();

        // Non-vacuous: confirm there is actually something to cascade before
        // the delete runs.
        assertThat(repo.linksFrom(TENANT_LINK, "dl-src", (List<String>) null))
            .as("pre-delete: link FROM the doomed doc exists").hasSize(1);
        assertThat(repo.linksTo(TENANT_LINK, "dl-src", (List<String>) null))
            .as("pre-delete: link TO the doomed doc exists").hasSize(1);
        assertThat(repo.linksFrom(TENANT_LINK, "dl-dst", (List<String>) null))
            .as("pre-delete: both of dl-dst's links (to the doomed doc, and to a "
                + "surviving doc) exist").hasSize(2);

        // The production call path: deleteCollection -> deleteCollectionTxn.
        repo.deleteCollection(TENANT_LINK, COLL_LINK_HOME);

        assertThat(repo.linksFrom(TENANT_LINK, "dl-src", (List<String>) null))
            .as("link FROM the deleted doc cascaded via fk_catalog_links_from_document").isEmpty();
        assertThat(repo.linksTo(TENANT_LINK, "dl-src", (List<String>) null))
            .as("link TO the deleted doc cascaded via fk_catalog_links_to_document").isEmpty();
        assertThat(repo.linksFrom(TENANT_LINK, "dl-dst", (List<String>) null))
            .as("the link between the two SURVIVING documents (both outside the "
                + "deleted collection) remains").hasSize(1);
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    /** Seed one full collection (all lifecycle tables) for {@code tenant}. Superuser; bypasses RLS. */
    private static void seedFullCollection(Connection su, String tenant) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, COLL)
           .execute();
        // catalog_documents + manifest
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                       CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
           .values(tenant, "dc-doc-1", "Doc 1", COLL)
           .execute();
        // chunks: 2/1/1 across three dims, one unified nexus.chunks table (RDR-191).
        insertChunk384(ctx, tenant, COLL, chashBytes("dc384a"), vector(384));
        // RDR-194 P3d (nexus-tk070.p3d): dc384b and dc768a below are REPURPOSED
        // (HexFormat.parseHex(hexChash(...)) identity, not the file's own
        // chashBytes(seed) escape-format shape) to ALSO back the two
        // topic_assignments rows further down via topic_assignments_chunk_fk --
        // reusing two of the four already-seeded chunks rather than minting two
        // more, so the "chunks (unified, 2+1+1)" count assertion stays exactly 4.
        // The two encodings are NOT interchangeable (chashBytes(seed) stores raw
        // ASCII bytes of a hex-digit-shaped string via bytea "escape format";
        // HexFormat.parseHex(...) stores the genuine hex-decoded bytes), so this
        // chunk's chash is no longer chashBytes("dc384b") -- nothing else in this
        // file references it by that name (unlike dc384a, reused by the manifest
        // INSERT below).
        insertChunk384(ctx, tenant, COLL, hexChashBytes("dc-doc-1"), vector(384));
        insertChunk768(ctx, tenant, COLL, hexChashBytes("dc-doc-2"), vector(768));
        insertChunk1024(ctx, tenant, COLL, chashBytes("dc1024a"), vector(1024));
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below -- reuse dc384a's
        // chash (rather than minting a fifth, distinct chunk) so the "chunks
        // (unified, 2+1+1)" count assertion above stays exactly 4.
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under COLL, so stamp the manifest row the same.
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                       CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                       CATALOG_DOCUMENT_CHUNKS.COLLECTION)
           .values(tenant, "dc-doc-1", 0, chashBytes("dc384a"), COLL)
           .execute();
        // (chash_index seeds removed — RDR-187/nexus-piwya.9: router dropped)
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + COLL).hashCode());
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                       TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
           .values(topicId, tenant, "topic-dc", COLL, 0, OffsetDateTime.now(), "pending")
           .execute();
        // taxonomy_meta: 1 (fk-003-4 RESTRICT — must be purged before the registry row)
        ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
           .values(tenant, COLL)
           .execute();
        // topic_assignments: 2, both with source_collection=COLL, referencing the topic
        // nexus-tk070.p3c: doc_id is bytea now — a genuine 64-hex chash, not the
        // catalog tumbler string (topic_assignments.doc_id is independent of
        // catalog_documents.tumbler; see the class javadoc / nexus-sa14p).
        // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires a
        // matching nexus.chunks row for each of these two INSERTs -- HexFormat.parseHex
        // here (NOT a bare string literal, which would store the ASCII bytes of the
        // hex STRING via bytea "escape format", never matching a real decoded row)
        // so both rows resolve against the two REPURPOSED chunks seeded above (dc384b's
        // and dc768a's slots, now carrying hexChash("dc-doc-1")/hexChash("dc-doc-2")
        // identities).
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(tenant, hexChashBytes("dc-doc-1"), topicId, "projection", COLL, OffsetDateTime.now())
           .execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(tenant, hexChashBytes("dc-doc-2"), topicId, "projection", COLL, OffsetDateTime.now())
           .execute();
        // centroids: one per dim (cugrk), one unified nexus.taxonomy_centroids table
        // (RDR-191). PK is (tenant_id, collection, topic_id) -- three DIFFERENT
        // topic_ids, not the shared `topicId` above (which would collide on the
        // unified PK; pre-unification these lived in three separate physical
        // tables and could legally share one topic_id).
        // hygiene-001 step 9b (nexus-tk070.p6a follow-on): label is NOT NULL now,
        // no default -- supply it explicitly.
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_384)
           .values(tenant, COLL, topicId, "", vector(384))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_768)
           .values(tenant, COLL, topicId + 1, "", vector(768))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_1024)
           .values(tenant, COLL, topicId + 2, "", vector(1024))
           .execute();
        // document_aspects: 2, both doc-rooted at dc-doc-1 (the collection's only
        // registered catalog_documents row -- catalog_documents count elsewhere in
        // this fixture is asserted ==1, so a second row cannot be introduced here).
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): doc_id/source_uri are both
        // NOT NULL now, reversing fk-001-2's nullable conversion -- the second row
        // used to be the DOC-LESS (doc_id=NULL) class nexus-tquoj's regression anchor
        // exercised; that state is no longer representable. The purge-by-collection
        // property tquoj guards (deleteCollection purges rows the fk-001 document
        // cascade cannot reach) can no longer be discriminated here: the doc-less
        // state cannot exist, so both rows go by their `collection` tag
        // (deleteCollectionTxn step 5), ahead of the doc_id-keyed cascade (step 6).
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
           .values(tenant, COLL, "/p/a1.md", OffsetDateTime.now(), "v1", "docling", "dc-doc-1", "file:///p/a1.md")
           .execute();
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
           .values(tenant, COLL, "/p/a2.md", OffsetDateTime.now(), "v1", "docling", "dc-doc-1", "file:///p/a2.md")
           .execute();
        // document_highlights: 1 (doc-rooted). hygiene-001 step 3: source_uri NOT NULL now.
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                       DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                       DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
           .values(tenant, "dc-doc-1", COLL, "file:///dc-doc-1-hl", "hi", OffsetDateTime.now())
           .execute();
        // aspect_extraction_queue: 2, both doc-rooted at dc-doc-1 (hygiene-001 step 2:
        // doc_id is NOT NULL now -- see the document_aspects comment above).
        ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                       ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                       ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
           .values(tenant, COLL, "/p/q1.md", "pending", OffsetDateTime.now(), "dc-doc-1")
           .execute();
        ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                       ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                       ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
           .values(tenant, COLL, "/p/q2.md", "pending", OffsetDateTime.now(), "dc-doc-1")
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

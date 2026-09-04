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

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P4 (bead nexus-jcx6w) — verify the document-level fk-001 ON DELETE CASCADE
 * fires in the service path, and document what it does NOT cover.
 *
 * <p>fk-001 (fk-001-catalog-cross-store.xml) makes four child tables cascade-delete when
 * their parent {@code catalog_documents(tenant_id, tumbler)} row is HARD-deleted:
 * {@code document_aspects}, {@code document_highlights}, {@code aspect_extraction_queue},
 * {@code catalog_document_chunks}. {@code topic_assignments} has NO document-rooted FK
 * (only a lookup index — its {@code doc_id} is a chunk content-hash, not a tumbler), so it
 * survives a document delete; its collection-scoped cleanup is the taxonomy cascade, not
 * fk-001. This isolates the FK behaviour from the explicit per-table deletes in
 * {@link CatalogDeleteCollectionCascadeTest}.
 *
 * <p>Three semantics are pinned: (1) a HARD {@code DELETE FROM catalog_documents} cascades to
 * the four FK children (the path {@code deleteCollection} takes); (2) the service's
 * {@code deleteDocument} API is a SOFT tombstone ({@code UPDATE deleted_at}) — it does NOT
 * fire {@code ON DELETE CASCADE}, so children intentionally survive a tombstone; (3) the
 * composite {@code (tenant_id, doc_id)} FK isolates tenants — deleting one tenant's document
 * never cascades another tenant's identically-named document's children.
 *
 * <p>KNOWN OPEN GAP (RDR-164 P4, NOT closed): because {@code topic_assignments} has no
 * document-rooted FK, a per-document HARD purge would leave its assignments orphaned. Today
 * the only hard-delete path is {@code deleteCollection}, which cleans {@code topic_assignments}
 * explicitly by {@code source_collection} (P2) — so no orphan accumulates in practice. But a
 * future per-document hard-purge path (e.g. trash-empty) MUST clean assignments explicitly;
 * fk-001 will not. Tracked as a follow-on bead (see RDR-164 P4 finding).
 *
 * <p>SCOPE NOTE (§Approach P4 bullet 2 — "retire redundant service-mode client cleanup"):
 * confirmed there is nothing to retire at the per-document level. {@code _WriteOps.delete_document}
 * is the LOCAL-mode SQLite manifest cascade (kept; no FK in sqlite); {@code HttpCatalogClient.
 * delete_document} is a bare POST that relies on the soft-delete endpoint. No service-mode client
 * fan-out exists here to remove. Collection-level client-orchestration retirement is P5's scope.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogDocumentCascadeTest {

    private static final String TENANT = "doc-casc";
    private static final String TENANT_B = "doc-casc-b";
    private static final String COLL = "knowledge__doc-casc__minilm-l6-v2-384__v1";
    private static final String SVC_ROLE = "svc_doc_casc";
    private static final String SVC_PASS = "svc_doc_casc_pass";

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
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void softDelete_tombstone_doesNotCascade() throws Exception {
        // The service's deleteDocument API is a soft tombstone (UPDATE deleted_at). It must
        // NOT fire fk-001 ON DELETE CASCADE — the doc-rooted children survive the tombstone.
        // nexus-tk070.p1 (RDR-194 § D2): the same applies to catalog_links now that
        // fk_catalog_links_from_document/_to_document exist — soft delete does not fire
        // ON DELETE CASCADE, so a link naming a tombstoned endpoint survives (exactly the
        // case CatalogRepository#orphanedLinks still reports).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, "soft-doc-1");
            seedDocument(su, TENANT, "soft-doc-2");
            seedLink(su, TENANT, "soft-doc-1", "soft-doc-2");
        }
        int n = repo.deleteDocument(TENANT, "soft-doc-1");
        assertThat(n).as("soft delete tombstoned one row").isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT deleted_at IS NOT NULL FROM nexus.catalog_documents WHERE tenant_id='"
                + TENANT + "' AND tumbler='soft-doc-1'")).as("row tombstoned, not removed").isEqualTo(1);
            // Children survive the tombstone (no cascade on UPDATE deleted_at).
            assertChildCounts(su, TENANT, "soft-doc-1", 1, 1, 1, 1);
            assertThat(countLinks(su, TENANT, "soft-doc-1"))
                .as("catalog_links survive a soft-delete (tombstone) of an endpoint").isEqualTo(1);
        }
    }

    @Test @Order(20)
    void hardDelete_catalogDocument_cascadesFourChildren_topicAssignmentSurvives() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, "hard-doc-1");
            seedDocument(su, TENANT, "hard-doc-2");
            // topic_assignment for the doc — it has NO document-rooted FK, so it must survive.
            seedTopicAssignment(su, TENANT, "hard-doc-1");
            // nexus-tk070.p1 (RDR-194 § D2): one link with hard-doc-1 as the FROM
            // endpoint, one with it as the TO endpoint — both sides of the FK.
            seedLink(su, TENANT, "hard-doc-1", "hard-doc-2");
            seedLink(su, TENANT, "hard-doc-2", "hard-doc-1");
        }
        // Sanity: children present before the hard delete.
        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, "hard-doc-1", 1, 1, 1, 1);
            assertThat(countLinks(su, TENANT, "hard-doc-1")).as("links present before delete").isEqualTo(2);
        }

        // HARD delete the catalog_documents row (the path deleteCollection takes); fk-001
        // cascades to the four doc-rooted children. FK checks run as table owner (bypass RLS),
        // so a superuser DELETE exercises the same constraint the svc-role delete would.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id='" + TENANT + "' AND tumbler='hard-doc-1'");
            assertThat(n).as("one catalog_documents row hard-deleted").isEqualTo(1);
        }

        try (Connection su = pg.createConnection("")) {
            // The four fk-001 CASCADE children are gone.
            assertChildCounts(su, TENANT, "hard-doc-1", 0, 0, 0, 0);
            // topic_assignments has NO document-rooted FK (fk-001 changeset 1 = index only) — survives.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT
                + "' AND doc_id=decode('" + hexChash("hard-doc-1") + "', 'hex')"))
                .as("topic_assignments NOT cascaded by document delete (no doc-rooted FK)").isEqualTo(1);
            // nexus-tk070.p1 (RDR-194 § D2): fk_catalog_links_from_document/_to_document
            // now cascade BOTH links (either endpoint = hard-doc-1) off this same hard delete.
            assertThat(countLinks(su, TENANT, "hard-doc-1"))
                .as("catalog_links cascaded on both sides by fk_catalog_links_from_document/_to_document")
                .isEqualTo(0);
            // hard-doc-2 itself (the surviving OTHER endpoint) is untouched.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT
                + "' AND tumbler='hard-doc-2'")).as("the other endpoint's document row survives").isEqualTo(1);
        }
    }

    @Test @Order(30)
    void hardDelete_compositeFkIsolatesTenants() throws Exception {
        // fk-001's headline property: the FK is composite (tenant_id, doc_id) → catalog_documents
        // (tenant_id, tumbler). Two tenants can share a tumbler value; deleting tenant A's document
        // must cascade ONLY tenant A's children — tenant B's identically-named document and its
        // children are untouched (the composite match requires the same tenant_id).
        final String shared = "xt-doc";
        final String partner = "xt-doc-partner";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, shared);
            seedDocument(su, TENANT, partner);
            seedDocument(su, TENANT_B, shared);
            seedDocument(su, TENANT_B, partner);
            // nexus-tk070.p1 (RDR-194 § D2): the composite FK is (tenant_id, from|to_tumbler)
            // -> (tenant_id, tumbler) — a link is per-tenant even though both tenants use the
            // identical tumbler string.
            seedLink(su, TENANT, shared, partner);
            seedLink(su, TENANT_B, shared, partner);
        }
        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, shared, 1, 1, 1, 1);
            assertChildCounts(su, TENANT_B, shared, 1, 1, 1, 1);
            assertThat(countLinks(su, TENANT, shared)).isEqualTo(1);
            assertThat(countLinks(su, TENANT_B, shared)).isEqualTo(1);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id='" + TENANT + "' AND tumbler='" + shared + "'");
        }

        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, shared, 0, 0, 0, 0);       // tenant A cascaded
            assertChildCounts(su, TENANT_B, shared, 1, 1, 1, 1);     // tenant B untouched
            assertThat(countLinks(su, TENANT, shared))
                .as("tenant A's link cascaded").isEqualTo(0);
            assertThat(countLinks(su, TENANT_B, shared))
                .as("tenant B's identically-tumblered link untouched").isEqualTo(1);
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Assert the four fk-001 child tables hold the expected row counts for {@code tenant}/{@code tumbler}. */
    private void assertChildCounts(Connection su, String tenant, String tumbler,
                                   int aspects, int highlights, int queue, int manifest) throws Exception {
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_aspects WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("document_aspects").isEqualTo(aspects);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("document_highlights").isEqualTo(highlights);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("aspect_extraction_queue").isEqualTo(queue);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("catalog_document_chunks manifest").isEqualTo(manifest);
    }

    /** Seed one document + its four fk-001 child rows (aspects/highlights/queue/manifest) for {@code tenant}. */
    private static void seedDocument(Connection su, String tenant, String tumbler) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        // Register the collection first (document_aspects/queue carry an fk-003 collection FK).
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, COLL)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                       CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
           .values(tenant, tumbler, "Doc", COLL)
           .execute();
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below.
        byte[] manifestChash = chashBytes("man" + tenant + tumbler);
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, COLL, manifestChash, "text", vector384())
           .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH)
           .doNothing()
           .execute();
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under COLL, so stamp the manifest row the same.
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                       CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                       CATALOG_DOCUMENT_CHUNKS.COLLECTION)
           .values(tenant, tumbler, 0, manifestChash, COLL)
           .execute();
        // hygiene-001 steps 1/3 (nexus-tk070.p6a follow-on): document_aspects.source_uri
        // and document_highlights.source_uri are both NOT NULL now.
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
           .values(tenant, COLL, "/p/a-" + tenant + "-" + tumbler + ".md", java.time.OffsetDateTime.now(), "v1",
                   "docling", tumbler, "file:///p/a-" + tenant + "-" + tumbler + ".md")
           .execute();
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                       DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                       DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
           .values(tenant, tumbler, COLL, "file:///hl-" + tenant + "-" + tumbler, "hi", java.time.OffsetDateTime.now())
           .execute();
        ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                       ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                       ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
           .values(tenant, COLL, "/p/q-" + tenant + "-" + tumbler + ".md", "pending", java.time.OffsetDateTime.now(),
                   tumbler)
           .execute();
    }

    /** Seed a topic + a topic_assignment keyed to {@code docId} for {@code tenant}. doc_id is
     *  bytea now (nexus-tk070.p3c) — {@code docId} is hashed via {@link #hexChash} to a genuine
     *  64-hex chash, independent of the catalog tumbler string (topic_assignments.doc_id has
     *  no FK to catalog_documents — see the class javadoc / nexus-sa14p). */
    private static void seedTopicAssignment(Connection su, String tenant, String docId) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, COLL)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
        // Mask to 32 bits before widening so Math.abs cannot overflow on Integer.MIN_VALUE.
        long topicId = (tenant + docId).hashCode() & 0xFFFFFFFFL;
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                       TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
           .values(topicId, tenant, "topic-dc", COLL, 0, java.time.OffsetDateTime.now(), "pending")
           .execute();
        // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires a
        // matching nexus.chunks row for this INSERT to succeed at all -- HexFormat.parseHex
        // on BOTH the chunk and the assignment produces the SAME genuine binary bytes
        // decode(..., 'hex') used to (a bare string literal into a bytea column stores the
        // ASCII bytes of the hex STRING via "escape format", never matching a real
        // decode()'d row).
        byte[] realChash = java.util.HexFormat.of().parseHex(hexChash(docId));
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, COLL, realChash, "text", vector384())
           .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH)
           .doNothing()
           .execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(tenant, realChash, topicId, "projection", COLL, java.time.OffsetDateTime.now())
           .execute();
    }

    /** Seed one catalog_links row directly (raw SQL, bypassing the repository's
     *  requireLiveEndpoints guard — both endpoints already exist via seedDocument). */
    private static void seedLink(Connection su, String tenant, String fromTumbler, String toTumbler) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER,
                       CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT)
           .values(tenant, fromTumbler, toTumbler, "relates", "test", java.time.OffsetDateTime.now())
           .execute();
    }

    /** Count catalog_links rows naming {@code tumbler} on EITHER side for {@code tenant}. */
    private static int countLinks(Connection su, String tenant, String tumbler) throws Exception {
        return rows(su, "SELECT COUNT(*) FROM nexus.catalog_links WHERE tenant_id='" + tenant
            + "' AND (from_tumbler='" + tumbler + "' OR to_tumbler='" + tumbler + "')");
    }

    /** 32-char hex-alphabet label, stored as its own ASCII bytes (no real hash semantics --
     *  matches the pre-conversion raw-SQL behavior of a bare string literal into a
     *  {@code bytea} column via PostgreSQL's escape-format input, {@link #hexChash} below is
     *  the genuine-hash counterpart). */
    private static byte[] chashBytes(String seed) {
        String label = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return label.getBytes(StandardCharsets.US_ASCII);
    }

    /** A 384-dim pgvector value with every component {@code 0.1}. */
    private static Vector vector384() {
        float[] v = new float[384];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
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
        // boolean expressions (deleted_at IS NOT NULL) come back as t/f; coerce to 1/0.
        Object v = rs.getObject(1);
        if (v instanceof Boolean b) return b ? 1 : 0;
        return rs.getInt(1);
    }
}

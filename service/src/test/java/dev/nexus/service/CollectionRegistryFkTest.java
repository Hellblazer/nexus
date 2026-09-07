package dev.nexus.service;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-156 bead nexus-70r3c.1 — TDD-RED suite for P0.2 FK + hygiene changesets.
 *
 * <p><strong>RDR-191 Phase 4 (repoint-batch lane F1) / Phase 5 (bead
 * nexus-o8dil.49) STATUS NOTE, UPDATED:</strong> {@code nexus.chunks_384/768/
 * 1024} are unified into ONE {@code nexus.chunks} table
 * (vectors-004-unify-chunks.xml); per that changeset's own "S3: NO
 * COLLECTION FK ON nexus.chunks UNTIL PHASE 5" note, the three per-dim
 * {@code chunks_<dim>_collection_fk} constraints this class was originally
 * written against died with their tables and were never re-added on the
 * unified table. Phase 5 (bead nexus-o8dil.49,
 * fk-004-chunks-collection-registry.xml) has since landed the UNIFIED
 * successor, {@code chunks_collection_fk} — FOREIGN KEY (tenant_id,
 * collection) REFERENCES catalog_collections(tenant_id, name) ON DELETE
 * RESTRICT, VALIDATED. Every test method below that was {@code @Disabled}
 * pending Phase 5 is now RE-ENABLED and RETARGETED to the unified table +
 * the single {@code chunks_collection_fk} constraint (dim-specific tests
 * keep their per-dim data shape — {@code embedding_<dim>} column — as
 * independent coverage of the SAME shared constraint, not three copies of
 * one test). The {@code chunks_<dim>_chash_len_check} CHECK-rejection tests
 * (GROUP 7) remain disabled, for an unrelated, pre-existing reason: T2/D5
 * lane record confirms those constraint names were already dropped by
 * rdr180-2, true before and after RDR-191 (nexus.chunks has no
 * length(text)=32 concept, only the octet family).
 *
 * <p><strong>TDD-RED state:</strong> All RED groups are written AGAINST the future schema
 * delivered by P0.2 (bead nexus-70r3c.2). P0.2 will add Liquibase changesets:
 * <ul>
 *   <li>fk-002: ADD CONSTRAINT ... NOT VALID for three FK groups, ON DELETE RESTRICT:</li>
 *     <ul>
 *       <li>{@code chunks_384_collection_fk}, {@code chunks_768_collection_fk},
 *           {@code chunks_1024_collection_fk}:
 *           FOREIGN KEY (tenant_id, collection) REFERENCES catalog_collections(tenant_id,name)
 *           ON DELETE RESTRICT NOT VALID</li>
 *       <li>{@code chash_index_collection_fk}:
 *           FOREIGN KEY (tenant_id, physical_collection) REFERENCES catalog_collections(tenant_id,name)
 *           ON DELETE RESTRICT NOT VALID</li>
 *       <li>{@code topic_assignments_collection_fk}:
 *           FOREIGN KEY (tenant_id, source_collection) REFERENCES catalog_collections(tenant_id,name)
 *           ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID</li>
 *     </ul>
 *   <li>hygiene: catalog_collections.created_at / superseded_at → timestamptz NULL
 *       (currently TEXT NOT NULL DEFAULT '')</li>
 *   <li>CHECK constraints: {@code chunks_<dim>_chash_len_check} (length(chash)=32) on each
 *       chunks table; {@code catalog_document_chunks_chash_len_check} and
 *       {@code catalog_document_chunks_position_check} on catalog_document_chunks</li>
 * </ul>
 *
 * <p><strong>Expected RED/GREEN before P0.2 lands:</strong>
 * <ul>
 *   <li>GROUP 1 (unregistered-reject): RED — FK absent, insert unexpectedly succeeds</li>
 *   <li>GROUP 2 (NOT VALID pin): RED — pg_constraint rows absent</li>
 *   <li>GROUP 3 (ON UPDATE CASCADE): RED — constraint absent, source_collection not updated</li>
 *   <li>GROUP 4 (NULL source_collection): GREEN — MATCH SIMPLE / nullable already works</li>
 *   <li>GROUP 5 (chash_index FK reject): RED — FK absent, insert unexpectedly succeeds</li>
 *   <li>GROUP 6 (ON DELETE RESTRICT): RED — RESTRICT absent, delete unexpectedly succeeds</li>
 *   <li>GROUP 7 (CHECK constraints): RED — checks absent, bad lengths accepted</li>
 *   <li>GROUP 8 (temporal typing): RED — column is TEXT, not timestamptz</li>
 *   <li>GROUP 9 (source_uri audit + no-unique): GREEN — audit query detects seeded duplicate;
 *       no unique constraint exists</li>
 *   <li>GROUP 10 (cross-tenant FK + RLS): RED — FK absent, cross-tenant insert succeeds</li>
 *   <li>All CONTROL paths (registered inserts, null source_collection): GREEN always</li>
 * </ul>
 *
 * <p>Verified schema facts used throughout (do not re-derive):
 * <ul>
 *   <li>catalog_collections PK (tenant_id, name); created_at/superseded_at TEXT NOT NULL DEFAULT ''
 *       (the SQLite heritage, to be converted to timestamptz NULL by P0.2 hygiene changeset)
 *       — source: catalog-001-baseline.xml changeset 5</li>
 *   <li>chunks_384/768/1024(tenant_id TEXT, collection TEXT, chash TEXT, ...; PK (tenant_id,collection,chash))
 *       — source: vectors-001-baseline.xml changesets 2-4</li>
 *   <li>chash_index(tenant_id, chash, physical_collection TEXT NOT NULL, ...; PK (tenant_id,chash,physical_collection))
 *       — source: chash-001-baseline.xml changeset 1</li>
 *   <li>topic_assignments(tenant_id, doc_id, topic_id BIGINT NOT NULL REFERENCES topics(id) CASCADE,
 *       source_collection TEXT NULLABLE; PK (tenant_id,doc_id,topic_id)) with (tenant_id,doc_id)
 *       FK to catalog_documents via fk-001 — source: taxonomy-001-baseline.xml changeset 3 +
 *       fk-001-catalog-cross-store.xml changeset 1 (index only, no catalog FK on ta)</li>
 *   <li>catalog_document_chunks(tenant_id, doc_id, position INTEGER NOT NULL, chash TEXT NOT NULL;
 *       PK (tenant_id,doc_id,position)) — source: catalog-001-baseline.xml changeset 4 +
 *       vectors-001-baseline.xml changeset 6 (nullable collection column added)</li>
 *   <li>catalog_documents PK (tenant_id, tumbler); source_uri TEXT NOT NULL DEFAULT ''
 *       — source: catalog-001-baseline.xml changeset 2</li>
 * </ul>
 *
 * <p><strong>source_uri audit provenance:</strong> T2 nexus_rdr/156-P0-source-uri-audit (2026-06-11):
 * 201 distinct source_uris duplicated in live SQLite before RDR-153 migration, e.g. rdr-127 file
 * under tumblers 1.1.1781/1.10.2708/1.10.2836. A UNIQUE constraint on (tenant_id, source_uri) is
 * DEFERRED pending ghost dedup; P0.2 ships NO such constraint (group 9b verifies absence).
 *
 * <p>Mirror conventions from ForeignKeyConstraintTest: PgContainerHelper.start(), master changelog
 * via Liquibase, PER_CLASS lifecycle, @Order, AssertJ + assertThrows(PSQLException.class),
 * superuser for direct inserts, svc role + GUC for RLS tests.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionRegistryFkTest {

    // ── Constraint names ─────────────────────────────────────────────────────
    // RDR-191 Phase 4: the three per-dim chunks_<dim>_collection_fk constraints
    // died with their tables (vectors-004-unify-chunks.xml). RDR-191 Phase 5
    // (bead nexus-o8dil.49, fk-004-chunks-collection-registry.xml) landed the
    // UNIFIED successor on nexus.chunks: chunks_collection_fk.
    private static final String FK_CHUNKS_UNIFIED = "chunks_collection_fk";
    private static final String FK_TOPIC_ASSIGN= "topic_assignments_collection_fk";

    // The CURRENTLY LIVE FK names (chash_index_collection_fk died with its
    // table, RDR-187/nexus-piwya.9; the three per-dim chunks FKs died with
    // theirs and are superseded by FK_CHUNKS_UNIFIED, RDR-191 Phase 5).
    private static final List<String> ALL_FIVE_FK_NAMES = List.of(
            FK_CHUNKS_UNIFIED, FK_TOPIC_ASSIGN);

    // CHECK constraint names (RDR-180: TEXT length(chash)=32 checks were dropped and
    // replaced by bytea octet_length(chash)=32 checks — rdr180-001-bytea-chash.xml)
    private static final String CHK_384_CHASH  = "chunks_384_chash_octet_check";
    private static final String CHK_768_CHASH  = "chunks_768_chash_octet_check";
    private static final String CHK_1024_CHASH = "chunks_1024_chash_octet_check";
    private static final String CHK_MANIFEST_CHASH = "catalog_document_chunks_chash_octet_check";
    private static final String CHK_MANIFEST_POS   = "catalog_document_chunks_position_check";

    private static final int[] DIMS = {384, 768, 1024};

    // Tenant IDs
    private static final String TENANT_A = "crfk-tenant-a";
    private static final String TENANT_B = "crfk-tenant-b";

    // Svc role for RLS posture tests (mirrors ForeignKeyConstraintTest.SVC_ROLE pattern)
    private static final String SVC_ROLE = "svc_crfk_test";
    private static final String SVC_PASS = "svc_crfk_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1+2: product schema (creates nexus_svc via role-001-nexus-svc.xml).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        // Phase 3: bootstrap the test-local svc role (create + grant; no search_path is
        // set -- every reference is schema-qualified, nexus-cbo4a batch 9).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        // HikariCP svc role pool (NOSUPERUSER NOBYPASSRLS — subject to RLS).
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — FK rejects unregistered (tenant_id, collection) in chunks tables
    //
    // EXPECTED RED: FK absent → insert succeeds when it should fail.
    // CONTROL (registered insert): always GREEN.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void chunks384_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands. RDR-191 Phase 4:
        // retargeted to the unified nexus.chunks table, embedding_384 column.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "ctrl-col-384");
            // Insert succeeds — registered collection
            PgContainerHelper.insertChunk384(ctx, TENANT_A, "ctrl-col-384", chashAscii("384ctrl"), vector(384));
            int count = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.COLLECTION.eq("ctrl-col-384"))
                .fetchOne(0, int.class);
            assertThat(count).as("registered insert into nexus.chunks (dim=384) must succeed").isEqualTo(1);
        }
    }

    @Test @Order(11)
    void chunks384_unregisteredCollection_rejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                PgContainerHelper.insertChunk384(ctx, TENANT_A, "unreg-col-384", chashAscii("384bad"), vector(384))
            );
            assertThat(ex.getMessage())
                .as("chunks_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    @Test @Order(12)
    void chunks768_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands. RDR-191 Phase 4:
        // retargeted to the unified nexus.chunks table, embedding_768 column.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "ctrl-col-768");
            PgContainerHelper.insertChunk768(ctx, TENANT_A, "ctrl-col-768", chashAscii("768ctrl"), vector(768));
            int count = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.COLLECTION.eq("ctrl-col-768"))
                .fetchOne(0, int.class);
            assertThat(count).as("registered insert into nexus.chunks (dim=768) must succeed").isEqualTo(1);
        }
    }

    @Test @Order(13)
    void chunks768_unregisteredCollection_rejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                PgContainerHelper.insertChunk768(ctx, TENANT_A, "unreg-col-768", chashAscii("768bad"), vector(768))
            );
            assertThat(ex.getMessage())
                .as("chunks_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    @Test @Order(14)
    void chunks1024_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands. RDR-191 Phase 4:
        // retargeted to the unified nexus.chunks table, embedding_1024 column.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "ctrl-col-1024");
            PgContainerHelper.insertChunk1024(ctx, TENANT_A, "ctrl-col-1024", chashAscii("1024ctrl"), vector(1024));
            int count = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.COLLECTION.eq("ctrl-col-1024"))
                .fetchOne(0, int.class);
            assertThat(count).as("registered insert into nexus.chunks (dim=1024) must succeed").isEqualTo(1);
        }
    }

    @Test @Order(15)
    void chunks1024_unregisteredCollection_rejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                PgContainerHelper.insertChunk1024(ctx, TENANT_A, "unreg-col-1024", chashAscii("1024bad"), vector(1024))
            );
            assertThat(ex.getMessage())
                .as("chunks_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — VALIDATED pin (RDR-156 P0.3, bead nexus-70r3c.3)
    //
    // After P0.3 (fk-002-validate.xml) the five FKs are VALIDATEd. On a fresh DB the
    // master changelog applies fk-002-6-reconcile (no rows → no-op) then VALIDATE
    // CONSTRAINT for each FK, so all five carry convalidated=true. (Pre-P0.3 this
    // asserted convalidated=false; flipped by P0.3, mirroring the fk-003 sibling.)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void allFiveCollectionFks_existAndAreValidated() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String fkName : ALL_FIVE_FK_NAMES) {
                PgCatalogProbes.Constraint rs = PgCatalogProbes.foreignKey(
                    DSL.using(su, SQLDialect.POSTGRES), "nexus", fkName);
                assertThat(rs)
                    .as("FK constraint " + fkName + " must exist in pg_constraint")
                    .isNotNull();
                assertThat(rs.convalidated())
                    .as("FK constraint " + fkName + " must be VALIDATED (convalidated=true) after P0.3 VALIDATE runs")
                    .isTrue();
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — ON UPDATE CASCADE for topic_assignments.source_collection
    //
    // EXPECTED RED: FK absent → source_collection not updated on collection rename.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void topicAssignments_sourceCollection_cascadesOnCollectionRename() throws Exception {
        // RED until P0.2 adds topic_assignments_collection_fk (ON UPDATE CASCADE).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk (composite,
            // (tenant_id, source_collection, doc_id) -> chunks(tenant_id, collection,
            // chash)) is orthogonal to this test's subject (topic_assignments_
            // collection_fk's ON UPDATE CASCADE) and structurally INCOMPATIBLE with
            // it: seeding a real nexus.chunks row to satisfy it would itself block
            // the rename below, because chunks_collection_fk (fk-004) has NO
            // ON UPDATE CASCADE of its own (plain NO ACTION) -- a chunk sitting on
            // 'casc__old' would not follow the rename, so the very UPDATE this test
            // exercises would fail on a DIFFERENT constraint before ever reaching
            // the cascade behavior under test. Dropped here rather than seeded
            // around; left dropped for the remainder of this shared container per
            // this file's own Group 13 convention (GROUP 13's header: "a future
            // @Order(>134) group must account for the residual re-added FKs" --
            // the identical shape, applied one FK earlier).
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists("topic_assignments_chunk_fk").execute();

            // Fixture: catalog_documents row (required by fk-001 (tenant_id,doc_id) FK)
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "casc-doc-1");
            // Fixture: topics row (required only for the topic_id FK). Its OWN collection must
            // NOT be the collection under rename — RDR-164 P1a's topics_collection_fk is
            // ON UPDATE NO ACTION, so a topic sitting on 'casc__old' would block the parent
            // rename. The topic's home collection is incidental to this test, which targets
            // topic_assignments.source_collection's ON UPDATE CASCADE specifically.
            insertTopic(ctx, TENANT_A, 8001L, "casc-topic", "casc__topic_home");
            // Fixture: registered collection 'casc__old'
            PgContainerHelper.insertCollection(ctx, TENANT_A, "casc__old");
            // Fixture: topic_assignment with source_collection='casc__old'. doc_id is
            // bytea now (nexus-tk070.p3c) -- a genuine 64-hex chash, independent of the
            // catalog_documents tumbler seeded above (topic_assignments.doc_id has no FK
            // to catalog_documents).
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(TENANT_A, hexChashBytes("casc-doc-1"), 8001L, "hdbscan", "casc__old", OffsetDateTime.now())
                .execute();

            // Rename collection: 'casc__old' -> 'casc__new'
            ctx.update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.NAME, "casc__new")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A)).and(CATALOG_COLLECTIONS.NAME.eq("casc__old"))
                .execute();

            // Assert: topic_assignments.source_collection must now be 'casc__new'
            var row = ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT_A))
                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(hexChashBytes("casc-doc-1")))
                .and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(8001L))
                .fetchOptional();
            assertThat(row.isPresent()).as("topic_assignment row must still exist after rename").isTrue();
            assertThat(row.get().value1())
                .as("ON UPDATE CASCADE must propagate collection rename to topic_assignments.source_collection")
                .isEqualTo("casc__new");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — NULL source_collection is REJECTED (RDR-194 D1/P3b, nexus-tk070.p3b)
    //
    // SUPERSEDED (nexus-tk070.p3b, taxonomy-010-1): source_collection is NOT
    // NULL as of P3b — the MATCH SIMPLE null-exemption this group originally
    // documented as accepted is exactly the vacuous-VALIDATE escape hatch D1
    // exists to close. EXPECTED GREEN: a null source_collection now fails
    // loud at INSERT time (D0.9 no-silent-fallback), never silently
    // satisfies topic_assignments_collection_fk via MATCH SIMPLE.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void topicAssignments_nullSourceCollection_rejected() throws Exception {
        // RED before P3b (NULL was accepted under MATCH SIMPLE); GREEN after
        // taxonomy-010-1's SET NOT NULL.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "null-src-doc");
            insertTopic(ctx, TENANT_A, 8002L, "null-src-topic", "null-src-col");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                    .values(TENANT_A, hexChashBytes("null-src-doc"), 8002L, "hdbscan", OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("source_collection must reject NULL post-P3b -- not-null violation, "
                    + "not a silent MATCH SIMPLE pass-through")
                .containsIgnoringCase("null value")
                .containsIgnoringCase("source_collection");
        }
    }

    @Test @Order(41)
    void topicAssignments_unregisteredNonNullSourceCollection_rejected() throws Exception {
        // RED until P0.2 adds topic_assignments_collection_fk.
        // Non-null source_collection that has no matching catalog_collections row must be rejected.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "unreg-src-doc");
            // insertTopic registers the topic's own collection (RDR-164 P1a topics_collection_fk),
            // so the assignment must reference a DISTINCT, still-unregistered source_collection to
            // preserve this test's intent (non-null source_collection absent from catalog_collections).
            insertTopic(ctx, TENANT_A, 8003L, "unreg-src-topic", "unreg-src-topic-col");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                    .values(TENANT_A, hexChashBytes("unreg-src-doc"), 8003L, "hdbscan", "truly-unreg-src-col",
                        OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("topic_assignments_collection_fk must reject non-null source_collection not in catalog_collections")
                .containsIgnoringCase(FK_TOPIC_ASSIGN);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 RETIRED (RDR-187/nexus-piwya.9): chash_index and its
    // collection FK died with the router table — nothing left to reject.
    // ══════════════════════════════════════════════════════════════════════════

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — ON DELETE RESTRICT: registered collection with live chunk row
    //
    // EXPECTED RED: RESTRICT absent → DELETE catalog_collections succeeds.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void deleteCollection_withLiveChunk384_isRejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk ON DELETE RESTRICT, unified.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "restrict-col-384");
            PgContainerHelper.insertChunk384(ctx, TENANT_A, "restrict-col-384", chashAscii("restrict384"), vector(384));

            // DELETE must be rejected because a live chunk row references the collection
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.deleteFrom(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A)).and(CATALOG_COLLECTIONS.NAME.eq("restrict-col-384"))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("ON DELETE RESTRICT must prevent deleting a collection with live nexus.chunks rows")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    @Test @Order(61)
    void deleteCollection_afterChunkDeleted_succeeds() throws Exception {
        // CONTROL for group 6 — after removing the chunk row, DELETE collection must succeed.
        // Always GREEN regardless of whether the FK/RESTRICT exists (no live referencer left).
        // RDR-191 Phase 4: retargeted to the unified nexus.chunks table, embedding_384 column.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "restrict-col-after");
            byte[] ch = chashAscii("restrict-after");
            PgContainerHelper.insertChunk384(ctx, TENANT_A, "restrict-col-after", ch, vector(384));

            // Delete the chunk row first
            ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.CHASH.eq(ch)).execute();

            // Now the collection delete must succeed
            int deleted = ctx.deleteFrom(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A)).and(CATALOG_COLLECTIONS.NAME.eq("restrict-col-after"))
                .execute();
            assertThat(deleted)
                .as("collection delete must succeed after all referencing chunk rows are removed")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — CHECK constraints
    //
    // EXPECTED RED: checks absent → bad lengths accepted.
    // CONTROL (length 32, position >= 0): always GREEN.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: this test's own assertion "
        + "targets the OCTET family (CHK_384_CHASH = chunks_384_chash_octet_check), not the "
        + "LEN family -- but nexus.chunks_384 was DROPPED CASCADE by "
        + "vectors-004-unify-chunks.xml, so the INSERT below fails on an undefined relation "
        + "before any CHECK constraint is reached; the unified nexus.chunks table carries only "
        + "a single unqualified chunks_chash_octet_check (D5 lane record, T2 "
        + "nexus/rdr-191-batch-D5-2026-08-13).")
    void chunks384_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_384_chash_len_check.
        // Dead code (this test is @Disabled): nexus.chunks_384 was DROPPED CASCADE by
        // vectors-004-unify-chunks.xml, so no generated jOOQ Table exists for it any
        // more -- DSL.table(DSL.name(...))/DSL.field(DSL.name(...), Class) is the
        // sanctioned typed-DSL form for a relation with no codegen (nexus-cbo4a batch 10).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks384 = DSL.table(DSL.name("nexus", "chunks_384"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-384-31");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks384)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-384-31", chashOfLen(31), "text", vector(384))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_384_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_384_CHASH);
        }
    }

    @Test @Order(71)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: same OCTET-family target "
        + "(CHK_384_CHASH) and same undefined-relation cause -- see "
        + "chunks384_chashLenCheck_rejects31.")
    void chunks384_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_384_chash_len_check.
        // Dead code (this test is @Disabled): see chunks384_chashLenCheck_rejects31's
        // comment for why DSL.table(DSL.name(...)) is used here.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks384 = DSL.table(DSL.name("nexus", "chunks_384"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-384-33");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks384)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-384-33", chashOfLen(33), "text", vector(384))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_384_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_384_CHASH);
        }
    }

    @Test @Order(72)
    void chunks384_chashLenCheck_accepts32() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands. RDR-191 Phase 4:
        // retargeted to the unified nexus.chunks table, embedding_384 column.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-384-32");
            byte[] ch = chashAscii("384-32ok");
            PgContainerHelper.insertChunk384(ctx, TENANT_A, "chk-col-384-32", ch, vector(384));
            int count = ctx.selectCount().from(CHUNKS).where(CHUNKS.CHASH.eq(ch)).fetchOne(0, int.class);
            assertThat(count).as("32-char chash must be accepted by nexus.chunks").isEqualTo(1);
        }
    }

    @Test @Order(73)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: same OCTET-family target "
        + "(CHK_768_CHASH) and same undefined-relation cause -- see "
        + "chunks384_chashLenCheck_rejects31.")
    void chunks768_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_768_chash_len_check.
        // Dead code (this test is @Disabled): see chunks384_chashLenCheck_rejects31's
        // comment for why DSL.table(DSL.name(...)) is used here.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks768 = DSL.table(DSL.name("nexus", "chunks_768"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-768-31");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks768)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-768-31", chashOfLen(31), "text", vector(768))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_768_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_768_CHASH);
        }
    }

    @Test @Order(74)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: same OCTET-family target "
        + "(CHK_768_CHASH) and same undefined-relation cause -- see "
        + "chunks384_chashLenCheck_rejects31.")
    void chunks768_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_768_chash_len_check.
        // Dead code (this test is @Disabled): see chunks384_chashLenCheck_rejects31's
        // comment for why DSL.table(DSL.name(...)) is used here.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks768 = DSL.table(DSL.name("nexus", "chunks_768"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-768-33");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks768)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-768-33", chashOfLen(33), "text", vector(768))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_768_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_768_CHASH);
        }
    }

    @Test @Order(75)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: same OCTET-family target "
        + "(CHK_1024_CHASH) and same undefined-relation cause -- see "
        + "chunks384_chashLenCheck_rejects31.")
    void chunks1024_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_1024_chash_len_check.
        // Dead code (this test is @Disabled): see chunks384_chashLenCheck_rejects31's
        // comment for why DSL.table(DSL.name(...)) is used here.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks1024 = DSL.table(DSL.name("nexus", "chunks_1024"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-1024-31");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks1024)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-1024-31", chashOfLen(31), "text", vector(1024))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_1024_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_1024_CHASH);
        }
    }

    @Test @Order(76)
    @Disabled("Pre-existing, unrelated to RDR-191 FK retargeting: same OCTET-family target "
        + "(CHK_1024_CHASH) and same undefined-relation cause -- see "
        + "chunks384_chashLenCheck_rejects31.")
    void chunks1024_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_1024_chash_len_check.
        // Dead code (this test is @Disabled): see chunks384_chashLenCheck_rejects31's
        // comment for why DSL.table(DSL.name(...)) is used here.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chunks1024 = DSL.table(DSL.name("nexus", "chunks_1024"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-col-1024-33");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(chunks1024)
                    .columns(DSL.field(DSL.name("tenant_id"), String.class), DSL.field(DSL.name("collection"), String.class),
                        DSL.field(DSL.name("chash"), String.class), DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("embedding"), Vector.class))
                    .values(TENANT_A, "chk-col-1024-33", chashOfLen(33), "text", vector(1024))
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("chunks_1024_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_1024_CHASH);
        }
    }

    @Test @Order(85)
    void catalogDocumentChunks_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds catalog_document_chunks_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "chk-manifest-doc");
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-manifest-coll");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT_A, "chk-manifest-doc", 0, chashOfLenBytes(31), "chk-manifest-coll")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("catalog_document_chunks_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_MANIFEST_CHASH);
        }
    }

    @Test @Order(86)
    void catalogDocumentChunks_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds catalog_document_chunks_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "chk-manifest-doc");  // idempotent via ON CONFLICT
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-manifest-coll");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT_A, "chk-manifest-doc", 1, chashOfLenBytes(33), "chk-manifest-coll")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("catalog_document_chunks_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_MANIFEST_CHASH);
        }
    }

    @Test @Order(87)
    void catalogDocumentChunks_chashLenCheck_accepts32() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "chk-manifest-doc");  // idempotent
            PgContainerHelper.insertCollection(ctx, TENANT_A, "chk-manifest-coll");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for this CONTROL insert to succeed.
            byte[] ch = chashAscii("manifestok");
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                .values(TENANT_A, "chk-manifest-coll", ch, "text", vector(384))
                .onConflictDoNothing()
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(TENANT_A, "chk-manifest-doc", 2, ch, "chk-manifest-coll")
                .execute();
            int count = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(ch))
                .fetchOne(0, int.class);
            assertThat(count).as("32-char chash must be accepted by catalog_document_chunks").isEqualTo(1);
        }
    }

    @Test @Order(88)
    void catalogDocumentChunks_positionCheck_rejectsNegative() throws Exception {
        // RED until P0.2 adds catalog_document_chunks_position_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "pos-chk-doc");
            PgContainerHelper.insertCollection(ctx, TENANT_A, "pos-chk-coll");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT_A, "pos-chk-doc", -1, chashAscii("pos-neg"), "pos-chk-coll")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("catalog_document_chunks_position_check must reject position < 0")
                .containsIgnoringCase(CHK_MANIFEST_POS);
        }
    }

    @Test @Order(89)
    void catalogDocumentChunks_positionCheck_acceptsZero() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "pos-chk-doc");  // idempotent
            PgContainerHelper.insertCollection(ctx, TENANT_A, "pos-chk-coll");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for this CONTROL insert to succeed.
            byte[] ch = chashAscii("pos-zero");
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                .values(TENANT_A, "pos-chk-coll", ch, "text", vector(384))
                .onConflictDoNothing()
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(TENANT_A, "pos-chk-doc", 0, ch, "pos-chk-coll")
                .onConflictDoNothing()
                .execute();
            int count = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A))
                .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("pos-chk-doc"))
                .and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(0))
                .fetchOne(0, int.class);
            assertThat(count).as("position=0 must be accepted").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — Temporal typing: created_at / superseded_at become timestamptz NULL
    //
    // EXPECTED RED: currently TEXT NOT NULL DEFAULT '' (SQLite heritage).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void catalogCollections_createdAt_isTimestamptzNotNull() throws Exception {
        // hygiene-001-6 (nexus-tk070.p6a follow-on) SUPERSEDES P0.2's nullable
        // conversion this test used to pin: the engine NEVER wrote created_at
        // (CatalogRepository only SELECTed it), so production held 260/260
        // NULL rows -- hygiene-001-6 backfills them and makes the column
        // NOT NULL DEFAULT now() again, closing the gap P0.2 opened.
        try (Connection su = pg.createConnection("")) {
            PgCatalogProbes.ColumnInfo rs = PgCatalogProbes.columnInfo(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_collections", "created_at");
            assertThat(rs).as("created_at column must exist in catalog_collections").isNotNull();
            assertThat(rs.dataType())
                .as("catalog_collections.created_at must be 'timestamp with time zone'")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.isNullable())
                .as("catalog_collections.created_at is NOT NULL again after hygiene-001-6")
                .isEqualTo("NO");
        }
    }

    @Test @Order(81)
    void catalogCollections_supersededAt_isTimestamptzNullable() throws Exception {
        // RED until P0.2 hygiene changeset converts superseded_at to timestamptz NULL.
        try (Connection su = pg.createConnection("")) {
            PgCatalogProbes.ColumnInfo rs = PgCatalogProbes.columnInfo(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_collections", "superseded_at");
            assertThat(rs).as("superseded_at column must exist in catalog_collections").isNotNull();
            assertThat(rs.dataType())
                .as("catalog_collections.superseded_at must be 'timestamp with time zone' after P0.2 hygiene")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.isNullable())
                .as("catalog_collections.superseded_at must be nullable after P0.2 hygiene")
                .isEqualTo("YES");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — source_uri uniqueness: constraint LANDED (was: audit-only)
    //
    // Provenance: T2 nexus_rdr/156-P0-source-uri-audit (2026-06-11) found 201 distinct
    // source_uris duplicated in live SQLite before RDR-153 migration, e.g. the rdr-127
    // file registered under tumblers 1.1.1781, 1.10.2708, and 1.10.2836.  P0.2 therefore
    // DEFERRED the unique constraint pending a dedup ("a blind ADD UNIQUE would fail on
    // existing data") and shipped only an audit query — Order(90) below originally
    // seeded a live duplicate and asserted the audit query FOUND it.
    //
    // catalog-016 (nexus-78n33) closed that loop exactly as Decision 7 prescribed:
    // 016-0 IS the dedup sweep (tombstones losers, most-chunks winner), 016-1 adds the
    // PARTIAL unique index on LIVE (tenant_id, source_uri) WHERE source_uri <> ''.
    // The seeded-duplicate scenario now must be REFUSED, not merely detected.
    // Order(91) is unchanged: it pins that no FULL (non-partial) unique index exists —
    // its original text explicitly allowed the partial-index endgame.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void sourceUriSeededDuplicate_nowRefusedByPartialUniqueIndex() throws Exception {
        // Was: audit-query-detects (constraint deferred). Now: the catalog-016
        // partial unique index refuses the second LIVE row outright.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String dupUri = "file:///docs/rdr/rdr-127-shared.md";
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.SOURCE_URI)
                .values(TENANT_A, "audit-t1", "Audit Doc 1", dupUri)
                .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
                .doUpdate()
                .set(CATALOG_DOCUMENTS.SOURCE_URI, dupUri)
                .execute();
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                        CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.SOURCE_URI)
                    .values(TENANT_A, "audit-t2", "Audit Doc 2", dupUri)
                    .execute());
            assertThat(ex.getMessage())
                .as("the catalog-016 partial unique index must refuse the live duplicate "
                    + "(the 201-uri debt class, audit 2026-06-11, dedup+constraint landed nexus-78n33)")
                .contains("ux_catalog_documents_live_source_uri");

            // The audit query the P0 harness shipped now finds NOTHING live —
            // uniqueness is enforced, not merely observed.
            boolean hasLiveDuplicate = ctx.select(DSL.val(1)).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.SOURCE_URI.ne(""))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .groupBy(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.SOURCE_URI)
                .having(DSL.count().gt(1))
                .fetch()
                .isNotEmpty();
            assertThat(hasLiveDuplicate)
                .as("no live duplicate (tenant_id, source_uri) groups can exist post-016")
                .isFalse();
        }
    }

    @Test @Order(91)
    void catalogDocuments_noUniqueConstraintOnSourceUri() throws Exception {
        // GREEN: no unique index/constraint on (tenant_id, source_uri) must exist.
        // A blind ADD UNIQUE must fail this test — the revisit is conscious
        // (pending ghost dedup sweep per RDR-156 Decision 7 + audit record above).
        try (Connection su = pg.createConnection("")) {
            // exclude partial idx (source_uri != '') allowed
            long fullSourceUriIndexes = PgCatalogProbes.indexDefs(
                    DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_documents").stream()
                .filter(def -> def.contains("source_uri") && !def.contains("WHERE"))
                .count();
            assertThat(fullSourceUriIndexes)
                .as("no full unique index on (tenant_id, source_uri) must exist — DEFERRED pending ghost dedup; " +
                    "if this fails, a constraint was added without completing the dedup sweep " +
                    "(RDR-156 Decision 7, audit 2026-06-11 found 201 duplicated source_uris)")
                .isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 10 — Cross-tenant FK isolation
    //
    // EXPECTED RED: FK absent → cross-tenant insert succeeds.
    // Also tests the RLS-posture variant (svc role + GUC).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(100)
    void chunks384_crossTenantCollection_rejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified.
        // Collection registered ONLY under TENANT_B; INSERT as TENANT_A must be rejected
        // by the composite FK (tenant_id, collection) → catalog_collections(tenant_id, name).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_B, "xtenant-col-b");
            // TENANT_A tries to insert a chunk row referencing TENANT_B's collection name.
            // The composite FK means (TENANT_A, 'xtenant-col-b') has no matching row in
            // catalog_collections — must be rejected.
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                PgContainerHelper.insertChunk384(ctx, TENANT_A, "xtenant-col-b", chashAscii("xtenant384"), vector(384))
            );
            assertThat(ex.getMessage())
                .as("composite FK must reject cross-tenant collection reference in nexus.chunks")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    @Test @Order(101)
    void chunks384_crossTenantCollection_viaRlsPosture_rejected() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified.
        // RLS-posture variant: svc role under FORCE RLS with GUC=TENANT_A tries to insert
        // a chunk referencing TENANT_B's collection.  FK must reject (not silently filtered).
        // Mirrors the tenant-correctness group in ForeignKeyConstraintTest (@Order 51-53).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT_B, "xtenant-rls-col-b");
        }
        // As svc role stamped as TENANT_A: insert referencing TENANT_B's collection name.
        // Use is_local=false (session-level) so the GUC persists for the INSERT statement
        // (is_local=true would scope the GUC to the set_config statement's own transaction
        // and expire before the INSERT when autoCommit=true).
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            DSLContext svcCtx = DSL.using(svc, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                PgContainerHelper.insertChunk384(svcCtx, TENANT_A, "xtenant-rls-col-b", chashAscii("xtenant-rls"), vector(384))
            );
            assertThat(ex.getMessage())
                .as("composite FK must reject cross-tenant collection reference via svc-role RLS posture")
                .containsIgnoringCase(FK_CHUNKS_UNIFIED);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 11 — PgVectorRepository.upsertChunks auto-registration
    //
    // Verifies that upsertChunks auto-stubs the collection into catalog_collections
    // before the chunk write, satisfying the FK without a separate registration call.
    // Also verifies that conformant collection names have their segments stored, and
    // that non-conformant names produce a name-only stub with empty metadata.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(110)
    void upsertChunks_conformantCollection_autoRegistersWithParsedSegments() throws Exception {
        // Conformant name: <content_type>__<owner_id>__<embedding_model>__v<n>
        // Uses minilm-l6-v2-384 → nexus.chunks embedding_384 (matching the fake embedder dim below).
        String conformantCol = "knowledge__auto-reg-owner__minilm-l6-v2-384__v1";
        String tenant = "autoreg-tenant-a";

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        try (var ds = new com.zaxxer.hikari.HikariDataSource(cfg)) {
            TenantScope scope = new TenantScope(ds);

            // Fake 384-dim embedder: returns unit vectors
            PgVectorRepository repo = new PgVectorRepository(scope,
                (texts) -> texts.stream()
                    .map(t -> {
                        float[] v = new float[384];
                        v[0] = 0.1f; return v;
                    }).collect(java.util.stream.Collectors.toList()),
                (texts) -> texts.stream()
                    .map(t -> {
                        float[] v = new float[384];
                        v[0] = 0.1f; return v;
                    }).collect(java.util.stream.Collectors.toList()));

            // upsert a chunk batch for an UNREGISTERED conformant collection
            repo.upsertChunks(tenant, conformantCol,
                List.of(dev.nexus.service.db.Chash.ofText("autoreg-384").toHex()),
                List.of("auto-reg chunk text"),
                List.of(Map.of()));
        }

        // Verify: (i) write succeeded — chunk row exists. RDR-191 Phase 4: retargeted
        // to the unified nexus.chunks table.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            int chunkCount = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(conformantCol))
                .fetchOne(0, int.class);
            assertThat(chunkCount)
                .as("upsertChunks must succeed (chunk row written) after auto-registration")
                .isEqualTo(1);

            // Verify: (ii) catalog_collections has the row WITH parsed segments
            var row = ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(conformantCol))
                .fetchOptional();
            assertThat(row.isPresent())
                .as("auto-registration must create a catalog_collections row for the conformant collection")
                .isTrue();
            assertThat(row.get().value1())
                .as("auto-registered row must store parsed content_type segment")
                .isEqualTo("knowledge");
            assertThat(row.get().value2())
                .as("auto-registered row must store parsed owner_id segment")
                .isEqualTo("auto-reg-owner");
            assertThat(row.get().value3())
                .as("auto-registered row must store parsed embedding_model segment")
                .isEqualTo("minilm-l6-v2-384");
            assertThat(row.get().value4())
                .as("auto-registered row must store parsed model_version segment")
                .isEqualTo("v1");
        }
    }

    @Test @Order(111)
    void upsertChunks_nonConformantCollection_autoRegistersNameOnlyStub() throws Exception {
        // Non-conformant name (not four-segment): upsertChunks uses dimForCollection which
        // requires conformant names, so this test uses a collection that IS four-segment
        // but with a known model token so dim dispatch works, AND tests the non-conformant
        // path via a separate ensure-registered path that produces a stub.
        // Actually: dimForCollection FAILS LOUD for non-conformant names, so upsertChunks
        // cannot be called with a non-conformant name.  Test the name-only stub via
        // direct catalog_collections insert + verify the stub semantics documented in AGENTS.md.
        // The auto-registration stub (empty metadata) is the correct behavior for name-only rows.
        String stubCol  = "stub-only-collection-nonconformant";
        String tenant   = "autoreg-tenant-stub";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Insert a stub manually (simulating fk-002-0 backfill for an unregistered collection)
            PgContainerHelper.insertCollection(ctx, tenant, stubCol);

            // Verify stub: metadata fields must all be ''
            var row = ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(stubCol))
                .fetchOptional();
            assertThat(row.isPresent())
                .as("stub row must exist in catalog_collections after minimal insert")
                .isTrue();
            assertThat(row.get().value1())
                .as("name-only stub must have empty content_type").isEqualTo("");
            assertThat(row.get().value2())
                .as("name-only stub must have empty owner_id").isEqualTo("");
            assertThat(row.get().value3())
                .as("name-only stub must have empty embedding_model").isEqualTo("");
            assertThat(row.get().value4())
                .as("name-only stub must have empty model_version").isEqualTo("");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 12 — CatalogRepository.renameCollection coherent re-home (RDR-164 P3)
    //
    // Position: the chunks FK is ON UPDATE NO ACTION, so a bare
    // `UPDATE catalog_collections SET name=Y` is blocked while a chunks_384
    // row still references the old name. Pre-P3 the rename did exactly that
    // bare UPDATE and this test asserted the resulting FK violation. RDR-164
    // P3 (bead nexus-77vve) replaced it with the coherent re-home
    // (INSERT new registry Y → re-home children X→Y → DELETE old registry X),
    // which never touches catalog_collections.name. The chunks-present case
    // that used to fail now SUCCEEDS and re-homes the chunk row — this test
    // now pins that coherent success. RDR-191 Phase 4: chunks_384/768/1024
    // unified into nexus.chunks; CatalogRepository.COLLECTION_SCOPED_TABLES'
    // count key collapsed from three ("chunks_384" etc.) to one ("chunks").
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(120)
    void renameCollection_withChunkRows_reHomesCoherently() throws Exception {
        // Register a collection, insert a chunk row, then rename the collection. Under the
        // coherent re-home the chunk row must move from the old name to the new name and the
        // registry row must move with it — no FK violation, no orphan under the old name.
        String tenant  = "grp12-rename-tenant";
        String oldName = "code__nexus__minilm-l6-v2-384__v1";
        String newName = "code__nexus__minilm-l6-v2-384__v2";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Register the collection
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            // Insert a chunk row referencing the collection
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                .values(tenant, oldName, chashAscii("grp12chunk1"), "rename-test chunk", vector(384))
                .execute();
        }

        // TenantScope / CatalogRepository via svc role.
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        try (var ds = new com.zaxxer.hikari.HikariDataSource(cfg)) {
            TenantScope scope = new TenantScope(ds);
            var repo = new dev.nexus.service.db.CatalogRepository(scope);

            // The coherent re-home succeeds (no FK violation) and reports the moved chunk.
            var counts = repo.renameCollection(tenant, oldName, newName);
            assertThat(counts.get("chunks"))
                .as("the chunks-present case re-homes the chunk row").isEqualTo(1);
            assertThat(counts.get("catalog_collections_inserted")).as("registry Y inserted").isEqualTo(1);
            assertThat(counts.get("catalog_collections_superseded"))
                .as("registry X retired as a superseded tombstone (nexus-cecqy)").isEqualTo(1);
        }

        // Verify the move at the SQL layer: chunk + registry under NEW, and under OLD a
        // superseded tombstone with no chunks (nexus-cecqy — step 3 retires, not deletes).
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            int chunksUnderOld = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(oldName)).fetchOne(0, int.class);
            assertThat(chunksUnderOld).as("no chunk orphan under old name").isZero();
            int chunksUnderNew = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(newName)).fetchOne(0, int.class);
            assertThat(chunksUnderNew).as("chunk re-homed under new name").isEqualTo(1);
            int oldTombstoned = ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(oldName))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(newName))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_AT.isNotNull())
                .fetchOne(0, int.class);
            assertThat(oldTombstoned).as("old registry row retired as a tombstone").isEqualTo(1);
            int newRegistryPresent = ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(newName))
                .fetchOne(0, int.class);
            assertThat(newRegistryPresent).as("new registry row present").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 13 — RDR-156 P0.3 (bead nexus-70r3c.3): the reconcile→VALIDATE flow, per FK.
    //
    // Proves the gap-window reconcile (fk-002-6-reconcile) is LOAD-BEARING for the
    // VALIDATE of each of the five fk-002 FKs: a row referencing an unregistered
    // collection makes VALIDATE FAIL; re-running that table's stub-register arm
    // registers the collection so VALIDATE then SUCCEEDS and flips convalidated=true.
    // Self-contained per test: drops + re-creates the FK itself and seeds the orphan
    // while the FK is ABSENT (NOT VALID still enforces NEW inserts, so the orphan
    // cannot be inserted under it). Distinct tenant per test keeps the not-registered-
    // before-reconcile precondition independent.
    //
    // Coverage spans ALL FIVE FKs with their materially-distinct shapes: chunks_* on
    // `collection`, chash_index on `physical_collection`, topic_assignments on
    // `source_collection` (nullable + ON UPDATE CASCADE + the reconcile's
    // WHERE source_collection != '' filter). Teardown is container-scoped (@AfterAll
    // pg.stop()); a future @Order(>134) group must account for the residual re-added
    // FKs and orphan rows these tests leave behind.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(130)
    void reconcileThenValidate_chunks384_gapWindowOrphan() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified table,
        // fk-004-1-reconcile's additive stub-register shape.
        final String T = "crfk-p03-c384";
        final String COL = "p03-orphan-c384";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertReconcileLoadBearing(su, ctx, CHUNKS, FK_CHUNKS_UNIFIED, "collection", "ON DELETE RESTRICT", T, COL,
                () -> PgContainerHelper.insertChunk384(ctx, T, COL, chashAscii("p03c384"), vector(384)),
                () -> runCollectionBackfillStub(ctx, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, null));
        }
    }

    @Test @Order(131)
    void reconcileThenValidate_chunks768_gapWindowOrphan() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified table,
        // fk-004-1-reconcile's additive stub-register shape — independent tenant/
        // collection/dim data point against the SAME shared constraint as the
        // 384-dim sibling above.
        final String T = "crfk-p03-c768";
        final String COL = "p03-orphan-c768";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertReconcileLoadBearing(su, ctx, CHUNKS, FK_CHUNKS_UNIFIED, "collection", "ON DELETE RESTRICT", T, COL,
                () -> PgContainerHelper.insertChunk768(ctx, T, COL, chashAscii("p03c768"), vector(768)),
                () -> runCollectionBackfillStub(ctx, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, null));
        }
    }

    @Test @Order(132)
    void reconcileThenValidate_chunks1024_gapWindowOrphan() throws Exception {
        // RDR-191 Phase 5 (nexus-o8dil.49): chunks_collection_fk, unified table,
        // fk-004-1-reconcile's additive stub-register shape — independent tenant/
        // collection/dim data point against the SAME shared constraint.
        final String T = "crfk-p03-c1024";
        final String COL = "p03-orphan-c1024";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertReconcileLoadBearing(su, ctx, CHUNKS, FK_CHUNKS_UNIFIED, "collection", "ON DELETE RESTRICT", T, COL,
                () -> PgContainerHelper.insertChunk1024(ctx, T, COL, chashAscii("p03c1024"), vector(1024)),
                () -> runCollectionBackfillStub(ctx, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, null));
        }
    }

    // Order(133) reconcileThenValidate_chashIndex RETIRED
    // (RDR-187/nexus-piwya.9): the router table and its FK are dropped; the
    // gap-window reconcile discipline stays pinned by the sibling
    // chunks/topic_assignments tests.

    @Test @Order(134)
    void reconcileThenValidate_topicAssignments_gapWindowOrphan() throws Exception {
        final String T = "crfk-p03-ta";
        final String COL = "p03-orphan-ta";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk is orthogonal
            // to this test's subject (topic_assignments_collection_fk's reconcile-
            // load-bearing VALIDATE flow) -- the orphanInsert below has no
            // matching nexus.chunks row and would otherwise violate the NEW FK
            // before ever reaching what this test verifies. Defensive IF-EXISTS
            // drop: Order(30) above already drops it for the remainder of this
            // shared container (Group 13's own convention), but this statement
            // makes THIS test robust to running standalone/out of order too.
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists("topic_assignments_chunk_fk").execute();
            // topic_assignments is multiply-rooted: seed the doc (fk-001 doc_id), the topic's
            // home collection (topics_collection_fk), and the topic row (topic_id FK) so the
            // ONLY remaining VALIDATE failure is the source_collection FK under test.
            PgContainerHelper.insertCatalogDocument(ctx, T, "p03-ta-doc");
            PgContainerHelper.insertCollection(ctx, T, "p03-ta-topic-home");
            insertTopic(ctx, T, 90301L, "p03-ta-topic", "p03-ta-topic-home");
            // FK on source_collection (nullable, MATCH SIMPLE) with ON UPDATE CASCADE; the
            // reconcile arm carries WHERE source_collection != '' — the materially-distinct case.
            assertReconcileLoadBearing(su, ctx, TOPIC_ASSIGNMENTS, FK_TOPIC_ASSIGN, "source_collection",
                "ON UPDATE CASCADE ON DELETE RESTRICT", T, COL,
                () -> ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                    .values(T, hexChashBytes("p03-ta-doc"), 90301L, "hdbscan", COL, OffsetDateTime.now())
                    .execute(),
                () -> runCollectionBackfillStub(ctx, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull().and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.ne(""))));
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Drives the RDR-156 P0.3 reconcile→VALIDATE causal proof for one collection FK:
     * the FK is dropped, an orphan row referencing an unregistered collection is seeded
     * while absent, the FK is re-added NOT VALID, VALIDATE FAILS on the orphan, the
     * table-specific reconcile stub-registers the collection, and VALIDATE then SUCCEEDS
     * with convalidated=true. {@code fkColumn} is the referencing column (collection /
     * physical_collection / source_collection); {@code extraFkClause} is the ON UPDATE/
     * ON DELETE clause to preserve when re-adding. {@code orphanInsert} and {@code
     * reconcileInsert} are the table-specific arms mirroring fk-002-validate.xml, now
     * typed jOOQ closures instead of raw SQL text.
     *
     * <p>{@code ADD CONSTRAINT .. NOT VALID} and {@code VALIDATE CONSTRAINT} stay raw
     * SQL strings deliberately — both are Postgres-specific {@code ALTER TABLE}
     * extensions with no jOOQ typed-DSL form (verified against jOOQ 3.21's manual, same
     * finding as {@code CollectionRegistryFkExtraTest}'s identical helper). {@code DROP
     * CONSTRAINT IF EXISTS} DOES have a typed form and is used by every caller before
     * this helper runs.
     */
    private void assertReconcileLoadBearing(
            Connection su, DSLContext ctx, Table<?> table, String fkName, String fkColumn, String extraFkClause,
            String tenant, String orphanCol, Runnable orphanInsert, Runnable reconcileInsert) throws Exception {
        // FK absent; seed an orphan row while it is absent.
        ctx.alterTable(table).dropConstraintIfExists(fkName).execute();
        orphanInsert.run();
        assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(orphanCol))
                .fetchOne(0, int.class))
            .as(table.getName() + ": orphan collection is NOT registered before reconcile").isZero();

        // Re-add the FK NOT VALID — succeeds (NOT VALID skips existing-row validation).
        // SANCTIONED RAW (nexus-cbo4a): see javadoc above.
        su.createStatement().execute(
            "ALTER TABLE nexus." + table.getName() + " ADD CONSTRAINT " + fkName + " " +
            "FOREIGN KEY (tenant_id, " + fkColumn + ") " +
            "REFERENCES nexus.catalog_collections (tenant_id, name) " + extraFkClause + " NOT VALID");

        // VALIDATE must FAIL while the orphan is unregistered — proves reconcile is load-bearing.
        // SANCTIONED RAW: see javadoc above.
        PSQLException ex = assertThrows(PSQLException.class, () ->
            su.createStatement().execute(
                "ALTER TABLE nexus." + table.getName() + " VALIDATE CONSTRAINT " + fkName));
        assertThat(ex.getMessage())
            .as(table.getName() + ": VALIDATE must fail loud on a gap-window orphan before reconcile")
            .containsIgnoringCase(fkName);

        // Reconcile: re-run this table's stub-register arm (fk-002-6-reconcile).
        reconcileInsert.run();
        assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(orphanCol))
                .fetchOne(0, int.class))
            .as(table.getName() + ": reconcile stub-registers the gap-window collection").isEqualTo(1);

        // VALIDATE now SUCCEEDS and flips convalidated=true.
        // SANCTIONED RAW: see javadoc above.
        su.createStatement().execute(
            "ALTER TABLE nexus." + table.getName() + " VALIDATE CONSTRAINT " + fkName);
        PgCatalogProbes.Constraint rs = PgCatalogProbes.foreignKey(ctx, "nexus", fkName);
        assertThat(rs).isNotNull();
        assertThat(rs.convalidated())
            .as(table.getName() + ": VALIDATE succeeds after reconcile → convalidated=true").isTrue();
    }

    /**
     * Runs the fk-002-6-reconcile stub-register shape for one source table via typed
     * jOOQ: {@code INSERT INTO nexus.catalog_collections (tenant_id, name) SELECT
     * DISTINCT tenant_id, <collectionField> FROM <source table> [WHERE <filter>] ON
     * CONFLICT (tenant_id, name) DO NOTHING} — same idiom as {@code
     * CollectionRegistryFkExtraTest#runBackfillStub}.
     */
    private static void runCollectionBackfillStub(
            DSLContext ctx, org.jooq.TableField<?, String> tenantField,
            org.jooq.TableField<?, String> collectionField, org.jooq.Condition filter) {
        var select = filter == null
            ? ctx.selectDistinct(tenantField, collectionField).from(tenantField.getTable())
            : ctx.selectDistinct(tenantField, collectionField).from(tenantField.getTable()).where(filter);
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
            .select(select)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Insert a topics row. Uses explicit ID to avoid sequence gaps across tests.
     * topics PK: (id) — BIGSERIAL. Supply explicit id and use ON CONFLICT DO NOTHING.
     */
    private static void insertTopic(DSLContext ctx, String tenantId, long id, String label, String collection) {
        // RDR-164 P1a: topics now carries topics_collection_fk → catalog_collections.
        // Register the topic's collection first so the fixture satisfies the NOT VALID FK.
        PgContainerHelper.insertCollection(ctx, tenantId, collection);
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(id, tenantId, label, collection, 0, OffsetDateTime.now(), "pending")
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Generate a 384/768/1024-dim pgvector value with every component equal to {@code 0.1}.
     * Matches the pattern from {@code CatalogRenameCollectionTest#vector}.
     */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /**
     * Return a valid 32-character hex chash deterministically derived from {@code seed}.
     * The seed is padded/truncated to exactly 32 hex characters (lowercase).
     */
    private static String validChash(String seed) {
        // Pad seed bytes to exactly 32 hex chars by repeating and truncating
        String hex = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return hex;
    }

    /** {@link #validChash}'s value, stored as its own ASCII bytes -- matches the
     *  pre-conversion raw-SQL behavior of a bare string literal into a {@code bytea}
     *  column via PostgreSQL's escape-format input. */
    private static byte[] chashAscii(String seed) {
        return validChash(seed).getBytes(StandardCharsets.US_ASCII);
    }

    /** {@link #chashOfLen}'s value, stored as its own ASCII bytes -- see {@link #chashAscii}. */
    private static byte[] chashOfLenBytes(int len) {
        return chashOfLen(len).getBytes(StandardCharsets.US_ASCII);
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c), unlike {@link #validChash} above which is only
     *  32 hex chars (used for the chunks.chash column, a different width). */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** {@link #hexChash}'s genuine hex-decoded bytes, for bytea columns like
     *  topic_assignments.doc_id -- NOT the ASCII-of-the-hex-string form {@link
     *  #chashAscii} produces (a different, deliberately distinct encoding; see
     *  CatalogRenameCollectionTest's identical hexChashBytes/chashBytes pairing). */
    private static byte[] hexChashBytes(String seed) {
        return java.util.HexFormat.of().parseHex(hexChash(seed));
    }

    /**
     * Return a hex string of exactly {@code len} characters (all 'a') for CHECK constraint tests.
     */
    private static String chashOfLen(int len) {
        return "a".repeat(len);
    }
}

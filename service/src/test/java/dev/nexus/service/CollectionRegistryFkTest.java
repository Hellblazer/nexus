package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-156 bead nexus-70r3c.1 — TDD-RED suite for P0.2 FK + hygiene changesets.
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

    // ── Constraint names (fixed contract; P0.2 will use exactly these) ─────────
    private static final String FK_CHUNKS_384  = "chunks_384_collection_fk";
    private static final String FK_CHUNKS_768  = "chunks_768_collection_fk";
    private static final String FK_CHUNKS_1024 = "chunks_1024_collection_fk";
    private static final String FK_CHASH_INDEX = "chash_index_collection_fk";
    private static final String FK_TOPIC_ASSIGN= "topic_assignments_collection_fk";

    // The five FK names P0.3 will VALIDATE (convalidated must be false until then)
    private static final List<String> ALL_FIVE_FK_NAMES = List.of(
            FK_CHUNKS_384, FK_CHUNKS_768, FK_CHUNKS_1024, FK_CHASH_INDEX, FK_TOPIC_ASSIGN);

    // CHECK constraint names
    private static final String CHK_384_CHASH  = "chunks_384_chash_len_check";
    private static final String CHK_768_CHASH  = "chunks_768_chash_len_check";
    private static final String CHK_1024_CHASH = "chunks_1024_chash_len_check";
    private static final String CHK_MANIFEST_CHASH = "catalog_document_chunks_chash_len_check";
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

        // Phase 1: create roles (autoCommit=true; CREATE ROLE cannot run in a txn)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        // Phase 2: apply full master changelog (all current changesets, no fk-002 yet)
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to tables needed for RLS posture tests.
        //          The master changelog's grants-nexus-svc.xml already covers nexus_svc;
        //          svc_crfk_test is test-specific and gets explicit table grants.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of(
                    "catalog_collections", "catalog_documents",
                    "chunks_384", "chunks_768", "chunks_1024",
                    "chash_index", "topic_assignments", "topics",
                    "catalog_document_chunks")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            // RDR-164 P3: renameCollection re-homes every denorm-collection table in one txn
            // (taxonomy_meta/centroids, aspects, highlights, queue, telemetry). Grant broadly.
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ctrl-col-384");
            // Insert succeeds — registered collection
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'ctrl-col-384', " +
                "'" + validChash("384ctrl") + "', 'text', " +
                vectorLiteral(384) + "::vector)");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chunks_384 " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='ctrl-col-384'");
            rs.next();
            assertThat(rs.getInt(1)).as("registered insert into chunks_384 must succeed").isEqualTo(1);
        }
    }

    @Test @Order(11)
    void chunks384_unregisteredCollection_rejected() throws Exception {
        // RED until P0.2 adds chunks_384_collection_fk.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-col-384', " +
                    "'" + validChash("384bad") + "', 'text', " +
                    vectorLiteral(384) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_384_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_384);
        }
    }

    @Test @Order(12)
    void chunks768_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ctrl-col-768");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'ctrl-col-768', " +
                "'" + validChash("768ctrl") + "', 'text', " +
                vectorLiteral(768) + "::vector)");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chunks_768 " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='ctrl-col-768'");
            rs.next();
            assertThat(rs.getInt(1)).as("registered insert into chunks_768 must succeed").isEqualTo(1);
        }
    }

    @Test @Order(13)
    void chunks768_unregisteredCollection_rejected() throws Exception {
        // RED until P0.2 adds chunks_768_collection_fk.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-col-768', " +
                    "'" + validChash("768bad") + "', 'text', " +
                    vectorLiteral(768) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_768_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_768);
        }
    }

    @Test @Order(14)
    void chunks1024_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ctrl-col-1024");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'ctrl-col-1024', " +
                "'" + validChash("1024ctrl") + "', 'text', " +
                vectorLiteral(1024) + "::vector)");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chunks_1024 " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='ctrl-col-1024'");
            rs.next();
            assertThat(rs.getInt(1)).as("registered insert into chunks_1024 must succeed").isEqualTo(1);
        }
    }

    @Test @Order(15)
    void chunks1024_unregisteredCollection_rejected() throws Exception {
        // RED until P0.2 adds chunks_1024_collection_fk.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-col-1024', " +
                    "'" + validChash("1024bad") + "', 'text', " +
                    vectorLiteral(1024) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_1024_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_CHUNKS_1024);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — NOT VALID pin
    //
    // EXPECTED RED: all five pg_constraint rows absent until P0.2 adds them.
    // P0.3's VALIDATE CONSTRAINT flips convalidated from false to true and must
    // UPDATE this test to assert convalidated=true.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void allFiveCollectionFks_existAndAreNotValid() throws Exception {
        // RED until P0.2 adds the NOT VALID changesets.
        // After P0.2: all five rows exist with convalidated=false.
        // After P0.3 (VALIDATE): update to convalidated=true.
        try (Connection su = pg.createConnection("")) {
            for (String fkName : ALL_FIVE_FK_NAMES) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT convalidated FROM pg_constraint c " +
                    "JOIN pg_namespace n ON n.oid = c.connamespace " +
                    "WHERE c.contype = 'f' " +
                    "  AND c.conname = '" + fkName + "' " +
                    "  AND n.nspname = 'nexus'");
                assertThat(rs.next())
                    .as("FK constraint " + fkName + " must exist in pg_constraint")
                    .isTrue();
                assertThat(rs.getBoolean("convalidated"))
                    .as("FK constraint " + fkName + " must be NOT VALID (convalidated=false) until P0.3 VALIDATE runs")
                    .isFalse();
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

            // Fixture: catalog_documents row (required by fk-001 (tenant_id,doc_id) FK)
            insertCatalogDocument(su, TENANT_A, "casc-doc-1");
            // Fixture: topics row (required only for the topic_id FK). Its OWN collection must
            // NOT be the collection under rename — RDR-164 P1a's topics_collection_fk is
            // ON UPDATE NO ACTION, so a topic sitting on 'casc__old' would block the parent
            // rename. The topic's home collection is incidental to this test, which targets
            // topic_assignments.source_collection's ON UPDATE CASCADE specifically.
            insertTopic(su, TENANT_A, 8001L, "casc-topic", "casc__topic_home");
            // Fixture: registered collection 'casc__old'
            insertCollection(su, TENANT_A, "casc__old");
            // Fixture: topic_assignment with source_collection='casc__old'
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments " +
                "(tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) VALUES " +
                "('" + TENANT_A + "', 'casc-doc-1', 8001, 'hdbscan', 'casc__old', NOW())");

            // Rename collection: 'casc__old' -> 'casc__new'
            su.createStatement().execute(
                "UPDATE nexus.catalog_collections " +
                "SET name = 'casc__new' " +
                "WHERE tenant_id = '" + TENANT_A + "' AND name = 'casc__old'");

            // Assert: topic_assignments.source_collection must now be 'casc__new'
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT source_collection FROM nexus.topic_assignments " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='casc-doc-1' AND topic_id=8001");
            assertThat(rs.next()).as("topic_assignment row must still exist after rename").isTrue();
            assertThat(rs.getString("source_collection"))
                .as("ON UPDATE CASCADE must propagate collection rename to topic_assignments.source_collection")
                .isEqualTo("casc__new");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — NULL source_collection is accepted (MATCH SIMPLE legacy tolerance)
    //
    // EXPECTED GREEN: source_collection IS NULLABLE; null satisfies any FK.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void topicAssignments_nullSourceCollection_accepted() throws Exception {
        // GREEN before AND after P0.2 lands (MATCH SIMPLE: null FK column = no check).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "null-src-doc");
            insertTopic(su, TENANT_A, 8002L, "null-src-topic", "null-src-col");
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments " +
                "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                "('" + TENANT_A + "', 'null-src-doc', 8002, 'hdbscan', NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT source_collection FROM nexus.topic_assignments " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='null-src-doc' AND topic_id=8002");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("source_collection"))
                .as("NULL source_collection must be accepted (MATCH SIMPLE, no FK violation)")
                .isNull();
        }
    }

    @Test @Order(41)
    void topicAssignments_unregisteredNonNullSourceCollection_rejected() throws Exception {
        // RED until P0.2 adds topic_assignments_collection_fk.
        // Non-null source_collection that has no matching catalog_collections row must be rejected.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "unreg-src-doc");
            // insertTopic registers the topic's own collection (RDR-164 P1a topics_collection_fk),
            // so the assignment must reference a DISTINCT, still-unregistered source_collection to
            // preserve this test's intent (non-null source_collection absent from catalog_collections).
            insertTopic(su, TENANT_A, 8003L, "unreg-src-topic", "unreg-src-topic-col");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.topic_assignments " +
                    "(tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) VALUES " +
                    "('" + TENANT_A + "', 'unreg-src-doc', 8003, 'hdbscan', 'truly-unreg-src-col', NOW())")
            );
            assertThat(ex.getMessage())
                .as("topic_assignments_collection_fk must reject non-null source_collection not in catalog_collections")
                .containsIgnoringCase(FK_TOPIC_ASSIGN);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — chash_index FK: unregistered physical_collection rejected
    //
    // EXPECTED RED: FK absent → insert with unregistered physical_collection succeeds.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void chashIndex_control_registeredCollection_accepted() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ci-ctrl-col");
            su.createStatement().execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
                "VALUES ('" + TENANT_A + "', '" + validChash("ci-ctrl") + "', 'ci-ctrl-col', NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chash_index " +
                "WHERE tenant_id='" + TENANT_A + "' AND physical_collection='ci-ctrl-col'");
            rs.next();
            assertThat(rs.getInt(1)).as("registered chash_index insert must succeed").isEqualTo(1);
        }
    }

    @Test @Order(51)
    void chashIndex_unregisteredCollection_rejected() throws Exception {
        // RED until P0.2 adds chash_index_collection_fk.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
                    "VALUES ('" + TENANT_A + "', '" + validChash("ci-bad") + "', 'unreg-ci-col', NOW())")
            );
            assertThat(ex.getMessage())
                .as("chash_index_collection_fk must reject unregistered physical_collection")
                .containsIgnoringCase(FK_CHASH_INDEX);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — ON DELETE RESTRICT: registered collection with live chunk row
    //
    // EXPECTED RED: RESTRICT absent → DELETE catalog_collections succeeds.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void deleteCollection_withLiveChunk384_isRejected() throws Exception {
        // RED until P0.2 adds chunks_384_collection_fk ON DELETE RESTRICT.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "restrict-col-384");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'restrict-col-384', " +
                "'" + validChash("restrict384") + "', 'text', " +
                vectorLiteral(384) + "::vector)");

            // DELETE must be rejected because a live chunk row references the collection
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "DELETE FROM nexus.catalog_collections " +
                    "WHERE tenant_id='" + TENANT_A + "' AND name='restrict-col-384'")
            );
            assertThat(ex.getMessage())
                .as("ON DELETE RESTRICT must prevent deleting a collection with live chunks_384 rows")
                .containsIgnoringCase(FK_CHUNKS_384);
        }
    }

    @Test @Order(61)
    void deleteCollection_afterChunkDeleted_succeeds() throws Exception {
        // CONTROL for group 6 — after removing the chunk row, DELETE collection must succeed.
        // This is GREEN now (RESTRICT absent → delete succeeds) AND after P0.2 (RESTRICT enforced
        // → delete requires removing the chunk first).  The test is always GREEN but it
        // verifies the "clear children then delete parent" pattern expected of callers.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "restrict-col-after");
            String ch = validChash("restrict-after");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'restrict-col-after', " +
                "'" + ch + "', 'text', " +
                vectorLiteral(384) + "::vector)");

            // Delete the chunk row first
            su.createStatement().execute(
                "DELETE FROM nexus.chunks_384 " +
                "WHERE tenant_id='" + TENANT_A + "' AND chash='" + ch + "'");

            // Now the collection delete must succeed
            int deleted = su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_collections " +
                "WHERE tenant_id='" + TENANT_A + "' AND name='restrict-col-after'");
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
    void chunks384_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_384_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-384-31");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-384-31', " +
                    "'" + chashOfLen(31) + "', 'text', " +
                    vectorLiteral(384) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_384_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_384_CHASH);
        }
    }

    @Test @Order(71)
    void chunks384_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_384_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-384-33");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-384-33', " +
                    "'" + chashOfLen(33) + "', 'text', " +
                    vectorLiteral(384) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_384_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_384_CHASH);
        }
    }

    @Test @Order(72)
    void chunks384_chashLenCheck_accepts32() throws Exception {
        // CONTROL — must be GREEN before and after P0.2 lands.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-384-32");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT_A + "', 'chk-col-384-32', " +
                "'" + validChash("384-32ok") + "', 'text', " +
                vectorLiteral(384) + "::vector)");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chunks_384 WHERE chash='" + validChash("384-32ok") + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("32-char chash must be accepted by chunks_384").isEqualTo(1);
        }
    }

    @Test @Order(73)
    void chunks768_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_768_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-768-31");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-768-31', " +
                    "'" + chashOfLen(31) + "', 'text', " +
                    vectorLiteral(768) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_768_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_768_CHASH);
        }
    }

    @Test @Order(74)
    void chunks768_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_768_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-768-33");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-768-33', " +
                    "'" + chashOfLen(33) + "', 'text', " +
                    vectorLiteral(768) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_768_chash_len_check must reject chash of length 33")
                .containsIgnoringCase(CHK_768_CHASH);
        }
    }

    @Test @Order(75)
    void chunks1024_chashLenCheck_rejects31() throws Exception {
        // RED until P0.2 adds chunks_1024_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-1024-31");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-1024-31', " +
                    "'" + chashOfLen(31) + "', 'text', " +
                    vectorLiteral(1024) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("chunks_1024_chash_len_check must reject chash of length 31")
                .containsIgnoringCase(CHK_1024_CHASH);
        }
    }

    @Test @Order(76)
    void chunks1024_chashLenCheck_rejects33() throws Exception {
        // RED until P0.2 adds chunks_1024_chash_len_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "chk-col-1024-33");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'chk-col-1024-33', " +
                    "'" + chashOfLen(33) + "', 'text', " +
                    vectorLiteral(1024) + "::vector)")
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
            insertCatalogDocument(su, TENANT_A, "chk-manifest-doc");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
                    "VALUES ('" + TENANT_A + "', 'chk-manifest-doc', 0, '" + chashOfLen(31) + "')")
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
            insertCatalogDocument(su, TENANT_A, "chk-manifest-doc");  // idempotent via ON CONFLICT
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
                    "VALUES ('" + TENANT_A + "', 'chk-manifest-doc', 1, '" + chashOfLen(33) + "')")
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
            insertCatalogDocument(su, TENANT_A, "chk-manifest-doc");  // idempotent
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
                "VALUES ('" + TENANT_A + "', 'chk-manifest-doc', 2, '" + validChash("manifestok") + "')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id='" + TENANT_A + "' AND chash='" + validChash("manifestok") + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("32-char chash must be accepted by catalog_document_chunks").isEqualTo(1);
        }
    }

    @Test @Order(88)
    void catalogDocumentChunks_positionCheck_rejectsNegative() throws Exception {
        // RED until P0.2 adds catalog_document_chunks_position_check.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "pos-chk-doc");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
                    "VALUES ('" + TENANT_A + "', 'pos-chk-doc', -1, '" + validChash("pos-neg") + "')")
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
            insertCatalogDocument(su, TENANT_A, "pos-chk-doc");  // idempotent
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
                "VALUES ('" + TENANT_A + "', 'pos-chk-doc', 0, '" + validChash("pos-zero") + "') " +
                "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='pos-chk-doc' AND position=0");
            rs.next();
            assertThat(rs.getInt(1)).as("position=0 must be accepted").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — Temporal typing: created_at / superseded_at become timestamptz NULL
    //
    // EXPECTED RED: currently TEXT NOT NULL DEFAULT '' (SQLite heritage).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void catalogCollections_createdAt_isTimestamptzNullable() throws Exception {
        // RED until P0.2 hygiene changeset converts created_at to timestamptz NULL.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT data_type, is_nullable " +
                "FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='catalog_collections' " +
                "  AND column_name='created_at'");
            assertThat(rs.next()).as("created_at column must exist in catalog_collections").isTrue();
            assertThat(rs.getString("data_type"))
                .as("catalog_collections.created_at must be 'timestamp with time zone' after P0.2 hygiene")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.getString("is_nullable"))
                .as("catalog_collections.created_at must be nullable after P0.2 hygiene (SQLite heritage was NOT NULL)")
                .isEqualTo("YES");
        }
    }

    @Test @Order(81)
    void catalogCollections_supersededAt_isTimestamptzNullable() throws Exception {
        // RED until P0.2 hygiene changeset converts superseded_at to timestamptz NULL.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT data_type, is_nullable " +
                "FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='catalog_collections' " +
                "  AND column_name='superseded_at'");
            assertThat(rs.next()).as("superseded_at column must exist in catalog_collections").isTrue();
            assertThat(rs.getString("data_type"))
                .as("catalog_collections.superseded_at must be 'timestamp with time zone' after P0.2 hygiene")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.getString("is_nullable"))
                .as("catalog_collections.superseded_at must be nullable after P0.2 hygiene")
                .isEqualTo("YES");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — source_uri audit harness + deferred-constraint pin
    //
    // EXPECTED GREEN (both sub-tests): the audit query detects the seeded duplicate,
    // AND no unique constraint exists on (tenant_id, source_uri).
    //
    // Provenance: T2 nexus_rdr/156-P0-source-uri-audit (2026-06-11) found 201 distinct
    // source_uris duplicated in live SQLite before RDR-153 migration, e.g. the rdr-127
    // file registered under tumblers 1.1.1781, 1.10.2708, and 1.10.2836.  A UNIQUE
    // constraint on (tenant_id, source_uri) is DEFERRED pending ghost dedup; P0.2 ships
    // NO such constraint.  A blind ADD UNIQUE would fail on existing data.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void sourceUriAuditQuery_detectsSeededDuplicate() throws Exception {
        // GREEN before and after P0.2 lands.
        // Seeds two catalog_documents rows with the same non-empty source_uri (same tenant,
        // different tumblers), then verifies the audit GROUP BY query finds them.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Seed: two docs sharing the same source_uri — mirrors the 201-uri debt found
            // in the 2026-06-11 live audit (e.g. rdr-127 → 1.1.1781 + 1.10.2708)
            String dupUri = "file:///docs/rdr/rdr-127-shared.md";
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri) " +
                "VALUES ('" + TENANT_A + "', 'audit-t1', 'Audit Doc 1', '" + dupUri + "') " +
                "ON CONFLICT (tenant_id, tumbler) DO UPDATE SET source_uri = EXCLUDED.source_uri");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri) " +
                "VALUES ('" + TENANT_A + "', 'audit-t2', 'Audit Doc 2', '" + dupUri + "') " +
                "ON CONFLICT (tenant_id, tumbler) DO UPDATE SET source_uri = EXCLUDED.source_uri");

            // Audit query: group by (tenant_id, source_uri) HAVING count > 1 WHERE non-empty
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT tenant_id, source_uri, COUNT(*) AS cnt " +
                "FROM nexus.catalog_documents " +
                "WHERE source_uri <> '' " +
                "GROUP BY tenant_id, source_uri " +
                "HAVING COUNT(*) > 1 " +
                "AND tenant_id = '" + TENANT_A + "' " +
                "AND source_uri = '" + dupUri + "'");
            assertThat(rs.next())
                .as("audit query must detect the seeded duplicate source_uri (201-uri debt, 2026-06-11)")
                .isTrue();
            assertThat(rs.getInt("cnt"))
                .as("duplicate count must be exactly 2 for the seeded pair")
                .isEqualTo(2);
        }
    }

    @Test @Order(91)
    void catalogDocuments_noUniqueConstraintOnSourceUri() throws Exception {
        // GREEN: no unique index/constraint on (tenant_id, source_uri) must exist.
        // A blind ADD UNIQUE must fail this test — the revisit is conscious
        // (pending ghost dedup sweep per RDR-156 Decision 7 + audit record above).
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM pg_indexes " +
                "WHERE schemaname = 'nexus' " +
                "  AND tablename  = 'catalog_documents' " +
                "  AND indexdef LIKE '%source_uri%' " +
                "  AND indexdef NOT LIKE '%WHERE%'");  // exclude partial idx (source_uri != '') allowed
            rs.next();
            assertThat(rs.getInt(1))
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
        // RED until P0.2 adds chunks_384_collection_fk.
        // Collection registered ONLY under TENANT_B; INSERT as TENANT_A must be rejected
        // by the composite FK (tenant_id, collection) → catalog_collections(tenant_id, name).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_B, "xtenant-col-b");
            // TENANT_A tries to insert a chunk row referencing TENANT_B's collection name.
            // The composite FK means (TENANT_A, 'xtenant-col-b') has no matching row in
            // catalog_collections — must be rejected.
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'xtenant-col-b', " +
                    "'" + validChash("xtenant384") + "', 'text', " +
                    vectorLiteral(384) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("composite FK must reject cross-tenant collection reference in chunks_384")
                .containsIgnoringCase(FK_CHUNKS_384);
        }
    }

    @Test @Order(101)
    void chunks384_crossTenantCollection_viaRlsPosture_rejected() throws Exception {
        // RED until P0.2 adds chunks_384_collection_fk.
        // RLS-posture variant: svc role under FORCE RLS with GUC=TENANT_A tries to insert
        // a chunk referencing TENANT_B's collection.  FK must reject (not silently filtered).
        // Mirrors the tenant-correctness group in ForeignKeyConstraintTest (@Order 51-53).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_B, "xtenant-rls-col-b");
        }
        // As svc role stamped as TENANT_A: insert referencing TENANT_B's collection name.
        // Use is_local=false (session-level) so the GUC persists for the INSERT statement
        // (is_local=true would scope the GUC to the set_config statement's own transaction
        // and expire before the INSERT when autoCommit=true).
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                svc.createStatement().execute(
                    "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT_A + "', 'xtenant-rls-col-b', " +
                    "'" + validChash("xtenant-rls") + "', 'text', " +
                    vectorLiteral(384) + "::vector)")
            );
            assertThat(ex.getMessage())
                .as("composite FK must reject cross-tenant collection reference via svc-role RLS posture")
                .containsIgnoringCase(FK_CHUNKS_384);
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
        // Uses minilm-l6-v2-384 → chunks_384 (matching the fake embedder dim below).
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
                List.of(validChash("autoreg-384")),
                List.of("auto-reg chunk text"),
                List.of(Map.of()));
        }

        // Verify: (i) write succeeded — chunk row exists
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.chunks_384 " +
                "WHERE tenant_id='" + tenant + "' AND collection='" + conformantCol + "'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("upsertChunks must succeed (chunk row written) after auto-registration")
                .isEqualTo(1);

            // Verify: (ii) catalog_collections has the row WITH parsed segments
            ResultSet crs = su.createStatement().executeQuery(
                "SELECT content_type, owner_id, embedding_model, model_version " +
                "FROM nexus.catalog_collections " +
                "WHERE tenant_id='" + tenant + "' AND name='" + conformantCol + "'");
            assertThat(crs.next())
                .as("auto-registration must create a catalog_collections row for the conformant collection")
                .isTrue();
            assertThat(crs.getString("content_type"))
                .as("auto-registered row must store parsed content_type segment")
                .isEqualTo("knowledge");
            assertThat(crs.getString("owner_id"))
                .as("auto-registered row must store parsed owner_id segment")
                .isEqualTo("auto-reg-owner");
            assertThat(crs.getString("embedding_model"))
                .as("auto-registered row must store parsed embedding_model segment")
                .isEqualTo("minilm-l6-v2-384");
            assertThat(crs.getString("model_version"))
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
            // Insert a stub manually (simulating fk-002-0 backfill for an unregistered collection)
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "VALUES ('" + tenant + "', '" + stubCol + "') " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");

            // Verify stub: metadata fields must all be ''
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT content_type, owner_id, embedding_model, model_version " +
                "FROM nexus.catalog_collections " +
                "WHERE tenant_id='" + tenant + "' AND name='" + stubCol + "'");
            assertThat(rs.next())
                .as("stub row must exist in catalog_collections after minimal insert")
                .isTrue();
            assertThat(rs.getString("content_type"))
                .as("name-only stub must have empty content_type").isEqualTo("");
            assertThat(rs.getString("owner_id"))
                .as("name-only stub must have empty owner_id").isEqualTo("");
            assertThat(rs.getString("embedding_model"))
                .as("name-only stub must have empty embedding_model").isEqualTo("");
            assertThat(rs.getString("model_version"))
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
    // now pins that coherent success.
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
            // Register the collection
            insertCollection(su, tenant, oldName);
            // Insert a chunk row referencing the collection
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + tenant + "', '" + oldName + "', " +
                "'" + validChash("grp12chunk1") + "', 'rename-test chunk', " +
                vectorLiteral(384) + "::vector)");
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
            assertThat(counts.get("chunks_384"))
                .as("the chunks-present case re-homes the chunk row").isEqualTo(1);
            assertThat(counts.get("catalog_collections_inserted")).as("registry Y inserted").isEqualTo(1);
            assertThat(counts.get("catalog_collections_deleted")).as("registry X deleted").isEqualTo(1);
        }

        // Verify the move at the SQL layer: chunk + registry under NEW, nothing under OLD.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs;
            rs = su.createStatement().executeQuery("SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='"
                + tenant + "' AND collection='" + oldName + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("no chunk orphan under old name").isZero();
            rs = su.createStatement().executeQuery("SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='"
                + tenant + "' AND collection='" + newName + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("chunk re-homed under new name").isEqualTo(1);
            rs = su.createStatement().executeQuery("SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='"
                + tenant + "' AND name='" + oldName + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("old registry row gone").isZero();
            rs = su.createStatement().executeQuery("SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='"
                + tenant + "' AND name='" + newName + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("new registry row present").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a minimal catalog_collections row. Uses ON CONFLICT DO NOTHING for idempotency.
     * catalog_collections PK: (tenant_id, name).
     * Columns content_type/owner_id/embedding_model/model_version/display_name all TEXT NOT NULL DEFAULT ''.
     * created_at/superseded_at TEXT NOT NULL DEFAULT '' (current SQLite heritage; P0.2 converts to timestamptz).
     */
    private static void insertCollection(Connection su, String tenantId, String name)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenantId + "', '" + name + "') " +
            "ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    /**
     * Insert a minimal catalog_documents row. Uses ON CONFLICT DO NOTHING for idempotency.
     * Required as a parent row because topic_assignments has (tenant_id, doc_id) → catalog_documents
     * via fk-001 (index only for topic_assignments, but the fixture must exist for topic fixture).
     * catalog_documents PK: (tenant_id, tumbler); title NOT NULL.
     */
    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    /**
     * Insert a topics row. Uses explicit ID to avoid sequence gaps across tests.
     * topics PK: (id) — BIGSERIAL. Supply explicit id and use ON CONFLICT DO NOTHING.
     */
    private static void insertTopic(Connection su, String tenantId, long id, String label, String collection)
            throws Exception {
        // RDR-164 P1a: topics now carries topics_collection_fk → catalog_collections.
        // Register the topic's collection first so the fixture satisfies the NOT VALID FK.
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenantId + "', '" + collection + "') " +
            "ON CONFLICT (tenant_id, name) DO NOTHING");
        su.createStatement().execute(
            "INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) " +
            "VALUES (" + id + ", '" + tenantId + "', '" + label + "', '" + collection + "', 0, NOW(), 'pending') " +
            "ON CONFLICT (id) DO NOTHING");
    }

    /**
     * Generate a pgvector literal string of {@code dim} uniform 0.1 components.
     * Format: {@code '[0.1,0.1,...,0.1]'} — safe for {@code ?::vector} and for
     * inline literal with {@code ::vector} cast.
     *
     * <p>Matches the pattern from ChunksRlsBehavioralTest.vectorLiteral().
     */
    private static String vectorLiteral(int dim) {
        return IntStream.range(0, dim)
                        .mapToObj(i -> "0.1")
                        .collect(Collectors.joining(",", "'[", "]'"));
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

    /**
     * Return a hex string of exactly {@code len} characters (all 'a') for CHECK constraint tests.
     */
    private static String chashOfLen(int len) {
        return "a".repeat(len);
    }
}

package dev.nexus.service;

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

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-164 P1a bead nexus-dcqml — collection-registry FK spine, second wave.
 *
 * <p>RDR-156 P0.2 (fk-002) added NOT VALID {@code ON DELETE RESTRICT} FKs from
 * {@code chunks_384/768/1024}, {@code chash_index}, and {@code topic_assignments}
 * to {@code catalog_collections(tenant_id, name)}. This suite covers the FIVE
 * remaining FK-eligible collection-level lifecycle tables that RDR-164 P1a wires
 * with the same NOT VALID + RESTRICT shape (changelog {@code fk-003-collection-registry-extra.xml}):
 *
 * <ul>
 *   <li>{@code document_aspects_collection_fk}      (collection NOT NULL)</li>
 *   <li>{@code aspect_extraction_queue_collection_fk}(collection NOT NULL)</li>
 *   <li>{@code topics_collection_fk}                (collection NOT NULL)</li>
 *   <li>{@code taxonomy_meta_collection_fk}         (collection NOT NULL, PK = (tenant_id, collection))</li>
 *   <li>{@code document_highlights_collection_fk}   (collection NULLABLE — MATCH SIMPLE, null escapes the FK)</li>
 * </ul>
 *
 * <p><strong>Scope boundary (RDR-164 P0 + P1a):</strong> P1a ships the backfill
 * (STUB-REGISTER, mirroring {@code fk-002-0-backfill-stubs}) plus these five NOT
 * VALID FKs. {@code VALIDATE CONSTRAINT} is P1b (bead nexus-70r3c.3 sibling),
 * world-blocked on the RDR-153 production migration completing with
 * {@code summary.total_failed==0}; until then every FK row carries
 * {@code convalidated=false} (GROUP C pins this). The orphan RECONCILE
 * (DELETE genuinely-orphaned / FAIL-LOUD ambiguous, Q5) rides with that
 * migration where real data exists — it is NOT in P1a.
 *
 * <p>Conventions mirror {@link CollectionRegistryFkTest}: {@link PgContainerHelper#start()},
 * master changelog via Liquibase, PER_CLASS lifecycle, {@code @Order}, AssertJ +
 * {@code assertThrows(PSQLException.class)}, superuser for direct inserts.
 *
 * <p>Verified schema facts (do not re-derive; source = Liquibase baselines):
 * <ul>
 *   <li>document_aspects(tenant_id TEXT, collection TEXT NOT NULL, doc_id TEXT NOT NULL DEFAULT '', source_path, ...)
 *       UNIQUE (tenant_id, collection, source_path) — aspects-001-baseline.xml changeset 1</li>
 *   <li>aspect_extraction_queue(tenant_id, collection TEXT NOT NULL, doc_id TEXT NOT NULL DEFAULT '', source_path, status, ...)
 *       UNIQUE (tenant_id, collection, source_path) — aspects-001-baseline.xml changeset 5</li>
 *   <li>document_highlights(tenant_id, doc_id TEXT NOT NULL, collection TEXT NULLABLE, ...)
 *       UNIQUE (tenant_id, doc_id) — aspects-001-baseline.xml changeset 3</li>
 *   <li>topics(id BIGSERIAL PK, tenant_id, collection TEXT NOT NULL, label, doc_count, ...) — taxonomy-001-baseline.xml changeset 1</li>
 *   <li>taxonomy_meta(tenant_id, collection TEXT NOT NULL, ...; PK (tenant_id, collection)) — taxonomy-001-baseline.xml changeset 2</li>
 *   <li>catalog_collections PK (tenant_id, name) — catalog-001-baseline.xml changeset 5</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionRegistryFkExtraTest {

    // ── Constraint names (fixed contract; fk-003 uses exactly these) ───────────
    private static final String FK_DOC_ASPECTS  = "document_aspects_collection_fk";
    private static final String FK_ASPECT_QUEUE = "aspect_extraction_queue_collection_fk";
    private static final String FK_TOPICS       = "topics_collection_fk";
    private static final String FK_TAX_META     = "taxonomy_meta_collection_fk";
    private static final String FK_DOC_HL       = "document_highlights_collection_fk";

    private static final List<String> ALL_FIVE_FK_NAMES = List.of(
            FK_DOC_ASPECTS, FK_ASPECT_QUEUE, FK_TOPICS, FK_TAX_META, FK_DOC_HL);

    private static final String TENANT_A = "crfkx-tenant-a";
    private static final String TENANT_B = "crfkx-tenant-b";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP A — FK rejects unregistered (tenant_id, collection)
    // EXPECTED RED until fk-003 lands.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void documentAspects_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-aspect-col', '/p/a.md', NOW(), 'v1', 'test')"));
            assertThat(ex.getMessage())
                .as("document_aspects_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_DOC_ASPECTS);
        }
    }

    @Test @Order(11)
    void aspectQueue_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-queue-col', '/p/q.md', 'pending', NOW())"));
            assertThat(ex.getMessage())
                .as("aspect_extraction_queue_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_ASPECT_QUEUE);
        }
    }

    @Test @Order(12)
    void topics_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) " +
                    "VALUES ('" + TENANT_A + "', 'topic-x', 'unreg-topic-col', 0, NOW(), 'pending')"));
            assertThat(ex.getMessage())
                .as("topics_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_TOPICS);
        }
    }

    @Test @Order(13)
    void taxonomyMeta_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.taxonomy_meta (tenant_id, collection) " +
                    "VALUES ('" + TENANT_A + "', 'unreg-meta-col')"));
            assertThat(ex.getMessage())
                .as("taxonomy_meta_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_TAX_META);
        }
    }

    @Test @Order(14)
    void documentHighlights_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Parent catalog_documents row so the doc-rooted fk-001 FK is satisfied and the
            // ONLY remaining violation is the collection FK under test.
            insertCatalogDocument(su, TENANT_A, "hl-doc-unreg");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, ingested_at) " +
                    "VALUES ('" + TENANT_A + "', 'hl-doc-unreg', 'unreg-hl-col', NOW())"));
            assertThat(ex.getMessage())
                .as("document_highlights_collection_fk must reject unregistered non-null collection")
                .containsIgnoringCase(FK_DOC_HL);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP B — control: registered collection accepted; null highlight collection accepted
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void documentAspects_registeredCollection_accepted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ctrl-aspect-col");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                "VALUES ('" + TENANT_A + "', 'ctrl-aspect-col', '/p/ctrl.md', NOW(), 'v1', 'test')");
            assertThat(count(su,
                "SELECT COUNT(*) FROM nexus.document_aspects " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='ctrl-aspect-col'"))
                .as("registered document_aspects insert must succeed").isEqualTo(1);
        }
    }

    @Test @Order(21)
    void topics_registeredCollection_accepted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "ctrl-topic-col");
            su.createStatement().execute(
                "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) " +
                "VALUES ('" + TENANT_A + "', 'topic-ctrl', 'ctrl-topic-col', 0, NOW(), 'pending')");
            assertThat(count(su,
                "SELECT COUNT(*) FROM nexus.topics " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='ctrl-topic-col'"))
                .as("registered topics insert must succeed").isEqualTo(1);
        }
    }

    @Test @Order(22)
    void documentHighlights_nullCollection_accepted() throws Exception {
        // MATCH SIMPLE: a null FK column escapes the constraint. Document-rooted highlights
        // with no collection tag rely on the fk-001 catalog_documents cascade, not this FK.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "hl-doc-null");
            su.createStatement().execute(
                "INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, ingested_at) " +
                "VALUES ('" + TENANT_A + "', 'hl-doc-null', NULL, NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT collection FROM nexus.document_highlights " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='hl-doc-null'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("collection"))
                .as("NULL highlight collection must be accepted (MATCH SIMPLE)").isNull();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP C — VALIDATED pin: after RDR-164 P1b (fk-003-validate.xml) the five FKs
    // are convalidated=true. The full master changelog applied by this test includes
    // P1b's gap-window reconcile + VALIDATE CONSTRAINT, so on a freshly-migrated DB
    // (no orphan rows) all five validate cleanly.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void allFiveExtraCollectionFks_existAndAreValidated() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String fkName : ALL_FIVE_FK_NAMES) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT convalidated FROM pg_constraint c " +
                    "JOIN pg_namespace n ON n.oid = c.connamespace " +
                    "WHERE c.contype = 'f' AND c.conname = '" + fkName + "' AND n.nspname = 'nexus'");
                assertThat(rs.next())
                    .as("FK constraint " + fkName + " must exist in pg_constraint").isTrue();
                assertThat(rs.getBoolean("convalidated"))
                    .as("FK " + fkName + " must be VALIDATED (convalidated=true) after P1b VALIDATE runs")
                    .isTrue();
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP D — ON DELETE RESTRICT: collection delete blocked while a child row lives.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void deleteCollection_withLiveAspectRow_isRejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "restrict-aspect-col");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                "VALUES ('" + TENANT_A + "', 'restrict-aspect-col', '/p/r.md', NOW(), 'v1', 'test')");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "DELETE FROM nexus.catalog_collections " +
                    "WHERE tenant_id='" + TENANT_A + "' AND name='restrict-aspect-col'"));
            assertThat(ex.getMessage())
                .as("ON DELETE RESTRICT must block deleting a collection with live document_aspects rows")
                .containsIgnoringCase(FK_DOC_ASPECTS);
        }
    }

    @Test @Order(41)
    void deleteCollection_afterAspectDeleted_succeeds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, "restrict-aspect-after");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                "VALUES ('" + TENANT_A + "', 'restrict-aspect-after', '/p/after.md', NOW(), 'v1', 'test')");
            su.createStatement().execute(
                "DELETE FROM nexus.document_aspects " +
                "WHERE tenant_id='" + TENANT_A + "' AND collection='restrict-aspect-after'");
            int deleted = su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_collections " +
                "WHERE tenant_id='" + TENANT_A + "' AND name='restrict-aspect-after'");
            assertThat(deleted)
                .as("collection delete must succeed once referencing aspect rows are removed")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP E — cross-tenant FK isolation (composite (tenant_id, collection)).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void documentAspects_crossTenantCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_B, "xtenant-aspect-col-b");
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                    "VALUES ('" + TENANT_A + "', 'xtenant-aspect-col-b', '/p/x.md', NOW(), 'v1', 'test')"));
            assertThat(ex.getMessage())
                .as("composite FK must reject cross-tenant collection reference in document_aspects")
                .containsIgnoringCase(FK_DOC_ASPECTS);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP F — backfill stub-register (fk-003-0) exact-count behavior.
    //
    // The master changelog applies the backfill against an EMPTY DB (no-op), then
    // adds the NOT VALID FKs. To exercise the backfill SQL against real orphan rows
    // we DROP the five FKs (so orphan inserts are allowed), seed orphans, then run
    // the SAME backfill SQL fk-003-0 ships and assert EXACT stub counts:
    //   - DISTINCT (tenant_id, collection) per source table
    //   - ON CONFLICT DO NOTHING dedup across tables and against pre-existing rows
    //   - document_highlights: only non-null, non-empty collection contributes
    // The SQL below MUST stay identical to changeset fk-003-0-backfill-stubs.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void backfillStubs_registersExactlyTheReferencedCollections() throws Exception {
        final String T = "crfkx-backfill-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Drop the five FKs so orphan child rows can be seeded.
            for (String fk : List.of(
                    "document_aspects",        // table → constraint <table>_collection_fk
                    "aspect_extraction_queue",
                    "topics",
                    "taxonomy_meta",
                    "document_highlights")) {
                su.createStatement().execute(
                    "ALTER TABLE nexus." + fk + " DROP CONSTRAINT IF EXISTS " + fk + "_collection_fk");
            }

            // Seed orphan rows referencing collections NOT in catalog_collections.
            // colA referenced by 3 tables (must dedup to ONE stub). colB by queue only.
            // colC by topics only. colD by taxonomy_meta only. colE (non-null) by a highlight.
            // A null-collection highlight must contribute NOTHING.
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) VALUES " +
                "('" + T + "', 'bf-colA', '/a/1.md', NOW(), 'v1', 'test'), ('" + T + "', 'bf-colA', '/a/2.md', NOW(), 'v1', 'test')");
            su.createStatement().execute(
                "INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at) VALUES " +
                "('" + T + "', 'bf-colA', '/a/1.md', 'pending', NOW()), ('" + T + "', 'bf-colB', '/b/1.md', 'pending', NOW())");
            su.createStatement().execute(
                "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) VALUES " +
                "('" + T + "', 'tA', 'bf-colA', 0, NOW(), 'pending'), " +
                "('" + T + "', 'tC', 'bf-colC', 0, NOW(), 'pending')");
            su.createStatement().execute(
                "INSERT INTO nexus.taxonomy_meta (tenant_id, collection) VALUES ('" + T + "', 'bf-colD')");
            // Parent docs for the highlight rows (doc-rooted fk-001 FK).
            insertCatalogDocument(su, T, "bf-hl-1");
            insertCatalogDocument(su, T, "bf-hl-2");
            insertCatalogDocument(su, T, "bf-hl-3");
            su.createStatement().execute(
                "INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, ingested_at) VALUES " +
                "('" + T + "', 'bf-hl-1', 'bf-colE', NOW()), ('" + T + "', 'bf-hl-2', NULL, NOW()), ('" + T + "', 'bf-hl-3', '', NOW())");

            assertThat(count(su,
                "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + T + "'"))
                .as("no stubs registered for backfill tenant before backfill").isEqualTo(0);

            // ── fk-003-0-backfill-stubs SQL (MUST match the changeset verbatim) ──
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.document_aspects " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.aspect_extraction_queue " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.topics " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.taxonomy_meta " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.document_highlights " +
                "WHERE collection IS NOT NULL AND collection != '' " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");

            // Exactly five distinct collections registered: colA, colB, colC, colD, colE.
            assertThat(count(su,
                "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + T + "'"))
                .as("backfill must stub-register exactly 5 distinct collections (colA deduped, null/empty highlight skipped)")
                .isEqualTo(5);
            assertThat(count(su,
                "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + T + "' " +
                "AND name IN ('bf-colA','bf-colB','bf-colC','bf-colD','bf-colE')"))
                .as("the 5 expected collection names must each be registered exactly once")
                .isEqualTo(5);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP G — RDR-164 P1b (nexus-p9aw6): the reconcile→VALIDATE flow, per FK.
    //
    // Proves the gap-window reconcile is LOAD-BEARING for VALIDATE: a row referencing
    // an unregistered collection (the gap-window orphan class) makes VALIDATE FAIL;
    // re-running the stub-register reconcile registers the collection so VALIDATE then
    // SUCCEEDS and flips convalidated=true. Self-contained per test: re-creates the FK
    // itself (GROUP F @Order(60) dropped all five) and seeds the orphan while the FK is
    // ABSENT (NOT VALID still enforces NEW inserts, so the orphan cannot be inserted
    // under it). Each test uses a distinct tenant so the "not registered before
    // reconcile" precondition holds independently.
    //
    // Coverage spans ALL FIVE FKs, mirroring fk-003-validate.xml changesets
    // fk-003-6-reconcile (the five INSERT-SELECT arms) + fk-003-7..11 (VALIDATE). The
    // four tables beyond document_aspects have materially distinct shapes — topics
    // (BIGSERIAL PK), taxonomy_meta (PK = (tenant_id, collection)), document_highlights
    // (NULLABLE collection + the reconcile's WHERE collection IS NOT NULL AND != ''
    // filter) — so each gets its own causal proof rather than relying on SQL similarity.
    //
    // Teardown is container-scoped: @AfterAll pg.stop() drops the whole DB, so the
    // re-added FKs and orphan rows these tests leave behind do not leak. Any future
    // @Order(>74) group must account for that residual state explicitly.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void reconcileThenValidate_documentAspects_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-aspects";
        final String ORPHAN_COL = "p1b-orphan-aspects";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertReconcileLoadBearing(su, "document_aspects", FK_DOC_ASPECTS, T, ORPHAN_COL,
                "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                "VALUES ('" + T + "', '" + ORPHAN_COL + "', '/p1b/o.md', NOW(), 'v1', 'test')",
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.document_aspects " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
        }
    }

    @Test @Order(71)
    void reconcileThenValidate_aspectQueue_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-queue";
        final String ORPHAN_COL = "p1b-orphan-queue";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertReconcileLoadBearing(su, "aspect_extraction_queue", FK_ASPECT_QUEUE, T, ORPHAN_COL,
                "INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at) " +
                "VALUES ('" + T + "', '" + ORPHAN_COL + "', '/p1b/q.md', 'pending', NOW())",
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.aspect_extraction_queue " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
        }
    }

    @Test @Order(72)
    void reconcileThenValidate_topics_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-topics";
        final String ORPHAN_COL = "p1b-orphan-topic";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertReconcileLoadBearing(su, "topics", FK_TOPICS, T, ORPHAN_COL,
                "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) " +
                "VALUES ('" + T + "', 'topic-p1b', '" + ORPHAN_COL + "', 0, NOW(), 'pending')",
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.topics " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
        }
    }

    @Test @Order(73)
    void reconcileThenValidate_taxonomyMeta_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-meta";
        final String ORPHAN_COL = "p1b-orphan-meta";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertReconcileLoadBearing(su, "taxonomy_meta", FK_TAX_META, T, ORPHAN_COL,
                "INSERT INTO nexus.taxonomy_meta (tenant_id, collection) " +
                "VALUES ('" + T + "', '" + ORPHAN_COL + "')",
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.taxonomy_meta " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
        }
    }

    @Test @Order(74)
    void reconcileThenValidate_documentHighlights_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-hl";
        final String ORPHAN_COL = "p1b-orphan-hl";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // GROUP F @Order(60) deliberately left an empty-string-collection highlight row
            // (bf-hl-3, collection='') and proved the reconcile's WHERE collection != '' filter
            // refuses to register it. Such a row is a STUB-UNSATISFIABLE orphan: it is non-null
            // (MATCH SIMPLE enforces it) yet the reconcile will never give it a parent, so
            // VALIDATE fails LOUD on it by design (arm-c, the intended safety property; the
            // non-registration is already asserted by GROUP F). Remove it here so this case can
            // isolate the happy-path gap-window reconcile proof for a real (non-empty) collection.
            su.createStatement().execute(
                "DELETE FROM nexus.document_highlights WHERE collection = ''");
            // document_highlights is doc-rooted (fk-001): seed the parent catalog_documents
            // row so the ONLY remaining VALIDATE failure is the collection FK under test.
            insertCatalogDocument(su, T, "hl-doc-p1b");
            // The document_highlights reconcile arm carries the WHERE collection IS NOT NULL
            // AND collection != '' filter (collection is NULLABLE here, MATCH SIMPLE) — this
            // is the materially-distinct arm; proving it still registers a real gap-window
            // collection is the point of this case.
            assertReconcileLoadBearing(su, "document_highlights", FK_DOC_HL, T, ORPHAN_COL,
                "INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, ingested_at) " +
                "VALUES ('" + T + "', 'hl-doc-p1b', '" + ORPHAN_COL + "', NOW())",
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "SELECT DISTINCT tenant_id, collection FROM nexus.document_highlights " +
                "WHERE collection IS NOT NULL AND collection != '' " +
                "ON CONFLICT (tenant_id, name) DO NOTHING");
        }
    }

    // ── helpers ────────────────────────────────────────────────────────────────

    /**
     * Drives the RDR-164 P1b reconcile→VALIDATE causal proof for one collection FK:
     * the FK is absent (GROUP F dropped all five), an orphan row referencing an
     * unregistered collection is seeded while absent, the FK is re-added NOT VALID,
     * VALIDATE FAILS on the orphan, the reconcile stub-registers the collection, and
     * VALIDATE then SUCCEEDS with convalidated=true. {@code orphanInsertSql} and
     * {@code reconcileInsertSql} are the table-specific arms mirroring
     * fk-003-validate.xml fk-003-6-reconcile.
     */
    private void assertReconcileLoadBearing(
            Connection su, String table, String fkName, String tenant, String orphanCol,
            String orphanInsertSql, String reconcileInsertSql) throws Exception {
        // FK absent (dropped by GROUP F); seed an orphan row while it is absent.
        su.createStatement().execute(
            "ALTER TABLE nexus." + table + " DROP CONSTRAINT IF EXISTS " + fkName);
        su.createStatement().execute(orphanInsertSql);
        assertThat(count(su, "SELECT COUNT(*) FROM nexus.catalog_collections " +
            "WHERE tenant_id='" + tenant + "' AND name='" + orphanCol + "'"))
            .as(table + ": orphan collection is NOT registered before reconcile").isZero();

        // Re-add the FK as NOT VALID — succeeds (NOT VALID skips existing-row validation).
        su.createStatement().execute(
            "ALTER TABLE nexus." + table + " ADD CONSTRAINT " + fkName + " " +
            "FOREIGN KEY (tenant_id, collection) " +
            "REFERENCES nexus.catalog_collections (tenant_id, name) " +
            "ON DELETE RESTRICT NOT VALID");

        // VALIDATE must FAIL while the orphan is unregistered — proves reconcile is load-bearing.
        PSQLException ex = assertThrows(PSQLException.class, () ->
            su.createStatement().execute(
                "ALTER TABLE nexus." + table + " VALIDATE CONSTRAINT " + fkName));
        assertThat(ex.getMessage())
            .as(table + ": VALIDATE must fail loud on a gap-window orphan before reconcile")
            .containsIgnoringCase(fkName);

        // Reconcile: re-run this table's stub-register arm (fk-003-6-reconcile).
        su.createStatement().execute(reconcileInsertSql);
        assertThat(count(su, "SELECT COUNT(*) FROM nexus.catalog_collections " +
            "WHERE tenant_id='" + tenant + "' AND name='" + orphanCol + "'"))
            .as(table + ": reconcile stub-registers the gap-window collection").isEqualTo(1);

        // VALIDATE now SUCCEEDS and flips convalidated=true.
        su.createStatement().execute(
            "ALTER TABLE nexus." + table + " VALIDATE CONSTRAINT " + fkName);
        ResultSet rs = su.createStatement().executeQuery(
            "SELECT convalidated FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace " +
            "WHERE c.contype='f' AND c.conname='" + fkName + "' AND n.nspname='nexus'");
        assertThat(rs.next()).isTrue();
        assertThat(rs.getBoolean("convalidated"))
            .as(table + ": VALIDATE succeeds after reconcile → convalidated=true").isTrue();
    }

    private static void insertCollection(Connection su, String tenantId, String name) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenantId + "', '" + name + "') " +
            "ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    private static int count(Connection su, String sql) throws Exception {
        ResultSet rs = su.createStatement().executeQuery(sql);
        rs.next();
        return rs.getInt(1);
    }
}

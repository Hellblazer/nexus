package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-164 P1a bead nexus-dcqml — collection-registry FK spine, second wave.
 *
 * <p>RDR-156 P0.2 (fk-002) added NOT VALID {@code ON DELETE RESTRICT} FKs from
 * {@code chunks_384/768/1024} (RDR-191 Phase 4: unified into {@code nexus.chunks};
 * the collection FK itself is Phase 5, {@link CollectionRegistryFkTest}'s territory,
 * not yet landed as of this comment), {@code chash_index}, and
 * {@code topic_assignments} to {@code catalog_collections(tenant_id, name)}. This
 * suite covers the FIVE
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
 * {@code assertThrows(DataAccessException.class)} (jOOQ's unchecked wrapper around the
 * underlying {@code PSQLException} — nexus-cbo4a batch 10 converted every insert/select/
 * delete here off {@code Connection}+string-literal SQL onto typed jOOQ DSL, so
 * {@code jOOQ}'s own exception type is what a rejected {@code .execute()} now throws),
 * superuser for direct inserts.
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

    /** The five FK-eligible tables in {@code ALL_FIVE_FK_NAMES} order — used to drive
     *  the GROUP F/G loops over typed jOOQ {@link Table} references instead of raw
     *  table-name strings, mirroring {@code CollectionRegistryFkTest}'s per-table FK
     *  bookkeeping. */
    private static final List<Table<?>> ALL_FIVE_FK_TABLES = List.of(
            DOCUMENT_ASPECTS, ASPECT_EXTRACTION_QUEUE, TOPICS, TAXONOMY_META, DOCUMENT_HIGHLIGHTS);

    private static final String TENANT_A = "crfkx-tenant-a";
    private static final String TENANT_B = "crfkx-tenant-b";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        // role-001 (the master changelog's first include) creates nexus_svc.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now -- seed a
            // real catalog-document parent so the ONLY violation exercised here
            // is the (still-unregistered) collection FK.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "asp-unreg-doc");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "unreg-aspect-col", "/p/a.md", OffsetDateTime.now(), "v1", "test",
                        "asp-unreg-doc", "file:///p/a.md")
                    .execute());
            assertThat(ex.getMessage())
                .as("document_aspects_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_DOC_ASPECTS);
        }
    }

    @Test @Order(11)
    void aspectQueue_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 2: doc_id is NOT NULL now -- seed a real
            // catalog-document parent so the collection FK is the sole violation.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "queue-unreg-doc");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                        ASPECT_EXTRACTION_QUEUE.DOC_ID)
                    .values(TENANT_A, "unreg-queue-col", "/p/q.md", "pending", OffsetDateTime.now(),
                        "queue-unreg-doc")
                    .execute());
            assertThat(ex.getMessage())
                .as("aspect_extraction_queue_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_ASPECT_QUEUE);
        }
    }

    @Test @Order(12)
    void topics_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                        TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                    .values(TENANT_A, "topic-x", "unreg-topic-col", 0, OffsetDateTime.now(), "pending")
                    .execute());
            assertThat(ex.getMessage())
                .as("topics_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_TOPICS);
        }
    }

    @Test @Order(13)
    void taxonomyMeta_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
                    .values(TENANT_A, "unreg-meta-col")
                    .execute());
            assertThat(ex.getMessage())
                .as("taxonomy_meta_collection_fk must reject unregistered collection")
                .containsIgnoringCase(FK_TAX_META);
        }
    }

    @Test @Order(14)
    void documentHighlights_unregisteredCollection_rejected() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Parent catalog_documents row so the doc-rooted fk-001 FK is satisfied and the
            // ONLY remaining violation is the collection FK under test. hygiene-001 step 3:
            // source_uri is NOT NULL now too.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "hl-doc-unreg");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                    .values(TENANT_A, "hl-doc-unreg", "file:///hl-doc-unreg", "unreg-hl-col", OffsetDateTime.now())
                    .execute());
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "ctrl-aspect-col");
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "ctrl-aspect-doc");
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "ctrl-aspect-col", "/p/ctrl.md", OffsetDateTime.now(), "v1", "test",
                    "ctrl-aspect-doc", "file:///p/ctrl.md")
                .execute();
            int count = ctx.selectCount().from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT_A))
                .and(DOCUMENT_ASPECTS.COLLECTION.eq("ctrl-aspect-col"))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("registered document_aspects insert must succeed").isEqualTo(1);
        }
    }

    @Test @Order(21)
    void topics_registeredCollection_accepted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "ctrl-topic-col");
            ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                    TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                .values(TENANT_A, "topic-ctrl", "ctrl-topic-col", 0, OffsetDateTime.now(), "pending")
                .execute();
            int count = ctx.selectCount().from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(TENANT_A))
                .and(TOPICS.COLLECTION.eq("ctrl-topic-col"))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("registered topics insert must succeed").isEqualTo(1);
        }
    }

    @Test @Order(22)
    void documentHighlights_nullCollection_isRejected() throws Exception {
        // hygiene-001 step 3 (nexus-tk070.p6a follow-on): document_highlights.collection
        // is NOT NULL now -- the MATCH SIMPLE null-escapes-the-FK scenario this test used
        // to cover no longer exists; a fresh insert with a NULL collection is rejected
        // outright by the NOT NULL constraint, ahead of this FK.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "hl-doc-null");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_HIGHLIGHTS)
                    .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, TENANT_A)
                    .set(DOCUMENT_HIGHLIGHTS.DOC_ID, "hl-doc-null")
                    .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, "file:///hl-doc-null")
                    .set(DOCUMENT_HIGHLIGHTS.COLLECTION, (String) null)
                    .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, OffsetDateTime.now())
                    .execute());
            assertThat(ex.getMessage())
                .as("document_highlights.collection must be NOT NULL (hygiene-001 step 3)")
                .containsIgnoringCase("null value in column \"collection\"");
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
                PgCatalogProbes.Constraint rs = PgCatalogProbes.foreignKey(
                    DSL.using(su, SQLDialect.POSTGRES), "nexus", fkName);
                assertThat(rs)
                    .as("FK constraint " + fkName + " must exist in pg_constraint").isNotNull();
                assertThat(rs.convalidated())
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "restrict-aspect-col");
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "restrict-aspect-doc");
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "restrict-aspect-col", "/p/r.md", OffsetDateTime.now(), "v1", "test",
                    "restrict-aspect-doc", "file:///p/r.md")
                .execute();
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.deleteFrom(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A))
                    .and(CATALOG_COLLECTIONS.NAME.eq("restrict-aspect-col"))
                    .execute());
            assertThat(ex.getMessage())
                .as("ON DELETE RESTRICT must block deleting a collection with live document_aspects rows")
                .containsIgnoringCase(FK_DOC_ASPECTS);
        }
    }

    @Test @Order(41)
    void deleteCollection_afterAspectDeleted_succeeds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "restrict-aspect-after");
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "restrict-aspect-after-doc");
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "restrict-aspect-after", "/p/after.md", OffsetDateTime.now(), "v1", "test",
                    "restrict-aspect-after-doc", "file:///p/after.md")
                .execute();
            ctx.deleteFrom(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT_A))
                .and(DOCUMENT_ASPECTS.COLLECTION.eq("restrict-aspect-after"))
                .execute();
            int deleted = ctx.deleteFrom(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A))
                .and(CATALOG_COLLECTIONS.NAME.eq("restrict-aspect-after"))
                .execute();
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_B, "xtenant-aspect-col-b");
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now -- seed a real
            // catalog-document parent for TENANT_A so the collection FK is the sole violation.
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "xtenant-aspect-doc");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "xtenant-aspect-col-b", "/p/x.md", OffsetDateTime.now(), "v1", "test",
                        "xtenant-aspect-doc", "file:///p/x.md")
                    .execute());
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
    // the SAME backfill shape fk-003-0 ships (jOOQ typed INSERT ... SELECT DISTINCT
    // ... ON CONFLICT DO NOTHING, rendering the identical statement the changeset's
    // literal SQL specifies) and assert EXACT stub counts:
    //   - DISTINCT (tenant_id, collection) per source table
    //   - ON CONFLICT DO NOTHING dedup across tables and against pre-existing rows
    //   - document_highlights: only non-null, non-empty collection contributes
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void backfillStubs_registersExactlyTheReferencedCollections() throws Exception {
        final String T = "crfkx-backfill-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            // Drop the five FKs so orphan child rows can be seeded. ALTER TABLE ..
            // DROP CONSTRAINT IF EXISTS has a typed jOOQ form (unlike ADD CONSTRAINT
            // .. NOT VALID / VALIDATE CONSTRAINT below, both Postgres-only DDL
            // extensions jOOQ's DSL does not model -- verified against jOOQ 3.21's
            // manual, nexus-cbo4a batch 10).
            for (int i = 0; i < ALL_FIVE_FK_TABLES.size(); i++) {
                ctx.alterTable(ALL_FIVE_FK_TABLES.get(i)).dropConstraintIfExists(ALL_FIVE_FK_NAMES.get(i)).execute();
            }

            // Seed orphan rows referencing collections NOT in catalog_collections.
            // colA referenced by 3 tables (must dedup to ONE stub). colB by queue only.
            // colC by topics only. colD by taxonomy_meta only. colE (non-null) by a highlight.
            // An empty-string-collection highlight must contribute NOTHING.
            //
            // hygiene-001 steps 1/2/3 (nexus-tk070.p6a follow-on): document_aspects.doc_id/
            // source_uri, aspect_extraction_queue.doc_id, and document_highlights.source_uri
            // are all NOT NULL now -- seed real catalog-document parents and supply the
            // columns so these rows insert at all; the backfill-stub SQL under test is
            // orthogonal to doc_id attribution.
            PgContainerHelper.insertCatalogDocument(ctx, T, "bf-aspect-doc");
            PgContainerHelper.insertCatalogDocument(ctx, T, "bf-queue-doc");
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(T, "bf-colA", "/a/1.md", OffsetDateTime.now(), "v1", "test", "bf-aspect-doc", "file:///a/1.md")
                .values(T, "bf-colA", "/a/2.md", OffsetDateTime.now(), "v1", "test", "bf-aspect-doc", "file:///a/2.md")
                .execute();
            ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .values(T, "bf-colA", "/a/1.md", "pending", OffsetDateTime.now(), "bf-queue-doc")
                .values(T, "bf-colB", "/b/1.md", "pending", OffsetDateTime.now(), "bf-queue-doc")
                .execute();
            ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                    TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                .values(T, "tA", "bf-colA", 0, OffsetDateTime.now(), "pending")
                .values(T, "tC", "bf-colC", 0, OffsetDateTime.now(), "pending")
                .execute();
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
                .values(T, "bf-colD")
                .execute();
            // Parent docs for the highlight rows (doc-rooted fk-001 FK). bf-hl-2 (the prior
            // NULL-collection seed row) is GONE: hygiene-001 step 3 makes collection NOT
            // NULL, so that case can no longer be represented by a live insert -- the
            // empty-string arm (bf-hl-3) remains as the fixture's sole coverage of the
            // reconcile SQL's exclusion filter.
            PgContainerHelper.insertCatalogDocument(ctx, T, "bf-hl-1");
            PgContainerHelper.insertCatalogDocument(ctx, T, "bf-hl-3");
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(T, "bf-hl-1", "file:///bf-hl-1", "bf-colE", OffsetDateTime.now())
                .values(T, "bf-hl-3", "file:///bf-hl-3", "", OffsetDateTime.now())
                .execute();

            assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).fetchOne(0, int.class))
                .as("no stubs registered for backfill tenant before backfill").isEqualTo(0);

            // ── fk-003-0-backfill-stubs shape (MUST render the changeset's SQL verbatim) ──
            runBackfillStub(ctx, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, null);
            runBackfillStub(ctx, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION, null);
            runBackfillStub(ctx, TOPICS.TENANT_ID, TOPICS.COLLECTION, null);
            runBackfillStub(ctx, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION, null);
            runBackfillStub(ctx, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                DOCUMENT_HIGHLIGHTS.COLLECTION.isNotNull().and(DOCUMENT_HIGHLIGHTS.COLLECTION.ne("")));

            // Exactly five distinct collections registered: colA, colB, colC, colD, colE.
            assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).fetchOne(0, int.class))
                .as("backfill must stub-register exactly 5 distinct collections (colA deduped, empty-string highlight skipped)")
                .isEqualTo(5);
            assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T))
                    .and(CATALOG_COLLECTIONS.NAME.in("bf-colA", "bf-colB", "bf-colC", "bf-colD", "bf-colE"))
                    .fetchOne(0, int.class))
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 1: doc_id/source_uri are NOT NULL now -- seed a real
            // catalog-document parent so the orphan insert succeeds at all (it is
            // seeded while the collection FK is absent, so the doc-id FK stays
            // the only one exercised at insert time).
            PgContainerHelper.insertCatalogDocument(ctx, T, "p1b-aspects-doc");
            assertReconcileLoadBearing(su, ctx, DOCUMENT_ASPECTS, FK_DOC_ASPECTS, T, ORPHAN_COL,
                () -> ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(T, ORPHAN_COL, "/p1b/o.md", OffsetDateTime.now(), "v1", "test",
                        "p1b-aspects-doc", "file:///p1b/o.md")
                    .execute(),
                () -> runBackfillStub(ctx, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, null));
        }
    }

    @Test @Order(71)
    void reconcileThenValidate_aspectQueue_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-queue";
        final String ORPHAN_COL = "p1b-orphan-queue";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 2: doc_id is NOT NULL now -- seed a real
            // catalog-document parent so the orphan insert succeeds at all.
            PgContainerHelper.insertCatalogDocument(ctx, T, "p1b-queue-doc");
            assertReconcileLoadBearing(su, ctx, ASPECT_EXTRACTION_QUEUE, FK_ASPECT_QUEUE, T, ORPHAN_COL,
                () -> ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT,
                        ASPECT_EXTRACTION_QUEUE.DOC_ID)
                    .values(T, ORPHAN_COL, "/p1b/q.md", "pending", OffsetDateTime.now(), "p1b-queue-doc")
                    .execute(),
                () -> runBackfillStub(ctx, ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION, null));
        }
    }

    @Test @Order(72)
    void reconcileThenValidate_topics_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-topics";
        final String ORPHAN_COL = "p1b-orphan-topic";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertReconcileLoadBearing(su, ctx, TOPICS, FK_TOPICS, T, ORPHAN_COL,
                () -> ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                        TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                    .values(T, "topic-p1b", ORPHAN_COL, 0, OffsetDateTime.now(), "pending")
                    .execute(),
                () -> runBackfillStub(ctx, TOPICS.TENANT_ID, TOPICS.COLLECTION, null));
        }
    }

    @Test @Order(73)
    void reconcileThenValidate_taxonomyMeta_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-meta";
        final String ORPHAN_COL = "p1b-orphan-meta";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertReconcileLoadBearing(su, ctx, TAXONOMY_META, FK_TAX_META, T, ORPHAN_COL,
                () -> ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
                    .values(T, ORPHAN_COL)
                    .execute(),
                () -> runBackfillStub(ctx, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION, null));
        }
    }

    @Test @Order(74)
    void reconcileThenValidate_documentHighlights_gapWindowOrphan() throws Exception {
        final String T = "crfkx-p1b-hl";
        final String ORPHAN_COL = "p1b-orphan-hl";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // GROUP F @Order(60) deliberately left an empty-string-collection highlight row
            // (bf-hl-3, collection='') and proved the reconcile's WHERE collection != '' filter
            // refuses to register it. Such a row is a STUB-UNSATISFIABLE orphan: it is non-null
            // (MATCH SIMPLE enforces it) yet the reconcile will never give it a parent, so
            // VALIDATE fails LOUD on it by design (arm-c, the intended safety property; the
            // non-registration is already asserted by GROUP F). Remove it here so this case can
            // isolate the happy-path gap-window reconcile proof for a real (non-empty) collection.
            ctx.deleteFrom(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.COLLECTION.eq("")).execute();
            // document_highlights is doc-rooted (fk-001): seed the parent catalog_documents
            // row so the ONLY remaining VALIDATE failure is the collection FK under test.
            PgContainerHelper.insertCatalogDocument(ctx, T, "hl-doc-p1b");
            // The document_highlights reconcile arm carries the WHERE collection IS NOT NULL
            // AND collection != '' filter (collection is NULLABLE here, MATCH SIMPLE) — this
            // is the materially-distinct arm; proving it still registers a real gap-window
            // collection is the point of this case.
            // hygiene-001 step 3: source_uri is NOT NULL now too.
            assertReconcileLoadBearing(su, ctx, DOCUMENT_HIGHLIGHTS, FK_DOC_HL, T, ORPHAN_COL,
                () -> ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                    .values(T, "hl-doc-p1b", "file:///hl-doc-p1b", ORPHAN_COL, OffsetDateTime.now())
                    .execute(),
                () -> runBackfillStub(ctx, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.COLLECTION.isNotNull().and(DOCUMENT_HIGHLIGHTS.COLLECTION.ne(""))));
        }
    }

    // ── helpers ────────────────────────────────────────────────────────────────

    /**
     * Runs the fk-003-0-backfill-stubs shape for one source table via typed jOOQ:
     * {@code INSERT INTO nexus.catalog_collections (tenant_id, name, content_type,
     * owner_id, embedding_model, lifecycle_state) SELECT DISTINCT tenant_id,
     * <collectionField>, 'unknown', tenant_id, 'bge-base-en-v15-768', 'live' FROM
     * <source table> [WHERE <filter>] ON CONFLICT (tenant_id, name) DO NOTHING} —
     * renders the exact statement shape the changeset's raw SQL specifies (DISTINCT
     * projection, optional WHERE, ON CONFLICT DO NOTHING), just built through jOOQ's
     * typed DSL instead of string concatenation.
     *
     * <p>nexus-ft04v.4/.5 fix (critic T2 critique-nexus-ft04v-4-walk-changeset-b42549f03
     * [24941]): hygiene-002-collection-attributes-walk.xml's non-empty CHECKs /
     * embedding_model FK / lifecycle_state NOT NULL reject the bare (tenant_id, name)
     * insert this used to do. The four backfilled columns here mirror this bead's own
     * walk's catch-all branch D (no separator/unparseable-shape) EXACTLY, since a
     * name derived from an arbitrary source table's own {@code collection} column has
     * no guaranteed RDR-103 shape either: content_type 'unknown', owner_id the row's
     * own tenant_id (the same value the walk uses), the seeded local fallback model,
     * lifecycle_state 'live' (this stub always represents a collection reference that
     * genuinely exists, never a disputed one). Literal projected columns via
     * {@code DSL.val(...)}, not string concatenation.
     */
    private static void runBackfillStub(
            DSLContext ctx, org.jooq.TableField<?, String> tenantField,
            org.jooq.TableField<?, String> collectionField, org.jooq.Condition filter) {
        var contentTypeField = DSL.val("unknown").as(CATALOG_COLLECTIONS.CONTENT_TYPE);
        var ownerIdField = tenantField.as(CATALOG_COLLECTIONS.OWNER_ID.getName());
        var embeddingModelField = DSL.val("bge-base-en-v15-768").as(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
        var lifecycleStateField = DSL.val("live").as(CATALOG_COLLECTIONS.LIFECYCLE_STATE);
        var select = filter == null
            ? ctx.selectDistinct(tenantField, collectionField, contentTypeField, ownerIdField,
                    embeddingModelField, lifecycleStateField).from(tenantField.getTable())
            : ctx.selectDistinct(tenantField, collectionField, contentTypeField, ownerIdField,
                    embeddingModelField, lifecycleStateField).from(tenantField.getTable()).where(filter);
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .select(select)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Drives the RDR-164 P1b reconcile→VALIDATE causal proof for one collection FK:
     * the FK is absent (GROUP F dropped all five), an orphan row referencing an
     * unregistered collection is seeded while absent, the FK is re-added NOT VALID,
     * VALIDATE FAILS on the orphan, the reconcile stub-registers the collection, and
     * VALIDATE then SUCCEEDS with convalidated=true. {@code orphanInsert} and
     * {@code reconcileInsert} are the table-specific arms mirroring
     * fk-003-validate.xml fk-003-6-reconcile, now typed jOOQ closures instead of raw
     * SQL text.
     *
     * <p>{@code ADD CONSTRAINT .. NOT VALID} and {@code VALIDATE CONSTRAINT} run
     * through {@code nexus_test.add_fk_not_valid}/{@code nexus_test.validate_constraint}
     * (db/changelog-test/db.changelog-test-objects.xml) via {@link
     * PgContainerHelper#addFkNotValid}/{@link PgContainerHelper#validateConstraint}
     * (nexus-cbo4a batch 10 review fold-in) — both Postgres-specific {@code ALTER
     * TABLE} extensions with no jOOQ typed-DSL form (verified against jOOQ 3.21's
     * manual: {@code alterConstraint().enforced()/notEnforced()} renders MySQL-style
     * {@code [NOT] ENFORCED}, not Postgres's {@code NOT VALID}/{@code VALIDATE
     * CONSTRAINT}), so the raw statement now lives server-side inside a plpgsql
     * wrapper function rather than being assembled client-side.
     */
    private void assertReconcileLoadBearing(
            Connection su, DSLContext ctx, Table<?> table, String fkName, String tenant, String orphanCol,
            Runnable orphanInsert, Runnable reconcileInsert) throws Exception {
        // FK absent (dropped by GROUP F); seed an orphan row while it is absent.
        ctx.alterTable(table).dropConstraintIfExists(fkName).execute();
        orphanInsert.run();
        assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(orphanCol))
                .fetchOne(0, int.class))
            .as(table.getName() + ": orphan collection is NOT registered before reconcile").isZero();

        // Re-add the FK as NOT VALID — succeeds (NOT VALID skips existing-row validation).
        PgContainerHelper.addFkNotValid(su, table, fkName, "collection", CATALOG_COLLECTIONS, "name",
            "ON DELETE RESTRICT");

        // VALIDATE must FAIL while the orphan is unregistered — proves reconcile is load-bearing.
        // jOOQ wraps the underlying PSQLException in its own unchecked DataAccessException.
        DataAccessException ex = assertThrows(DataAccessException.class, () ->
            PgContainerHelper.validateConstraint(su, table, fkName));
        assertThat(ex.getMessage())
            .as(table.getName() + ": VALIDATE must fail loud on a gap-window orphan before reconcile")
            .containsIgnoringCase(fkName);

        // Reconcile: re-run this table's stub-register arm (fk-003-6-reconcile).
        reconcileInsert.run();
        assertThat(ctx.selectCount().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(orphanCol))
                .fetchOne(0, int.class))
            .as(table.getName() + ": reconcile stub-registers the gap-window collection").isEqualTo(1);

        // VALIDATE now SUCCEEDS and flips convalidated=true.
        PgContainerHelper.validateConstraint(su, table, fkName);
        PgCatalogProbes.Constraint rs = PgCatalogProbes.foreignKey(ctx, "nexus", fkName);
        assertThat(rs).isNotNull();
        assertThat(rs.convalidated())
            .as(table.getName() + ": VALIDATE succeeds after reconcile → convalidated=true").isTrue();
    }
}

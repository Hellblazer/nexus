package dev.nexus.service;

import java.util.HexFormat;
import java.time.OffsetDateTime;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-187 bead nexus-piwya.3 — ChashRepository integration tests for the
 * chunks-backed reroute.
 *
 * <p>The write surface (upsert/upsertMany/doImport/deleteCollection/
 * deleteStale) is GONE — the router table was the only thing those wrote; the
 * chunks table is written by the vector ingest paths. What remains, all
 * served from the unified {@code nexus.chunks} table (RDR-191 Phase 4,
 * repoint-batch lane D5 — formerly {@code chunks_384/768/1024}) under RLS
 * via the real {@code nexus_svc} role:
 * <ol>
 *   <li>lookup: all (collection, created_at) rows for a chash, across dim
 *       tables, router-era key names and second-precision UTC format</li>
 *   <li>lookup: unknown chash yields empty</li>
 *   <li>distinctCollections: chunk-bearing collections only (a zero-chunk
 *       registry stub does not appear)</li>
 *   <li>renameCollection: re-homes rows across all dim tables, registers the
 *       new collection, collision-defends (NEW-side row wins), and is
 *       idempotent when the RDR-164 cascade already re-homed (0 rows)</li>
 *   <li>isEmpty / countForCollection: chunk-backed truth, per tenant</li>
 *   <li>registeredChashesForCollection: distinct 64-hex digests</li>
 *   <li>RLS isolation: tenant A chunks invisible to tenant B</li>
 * </ol>
 *
 * <p>The superset-conformance contract against the router table's contents is
 * pinned separately by {@code ChashRerouteConformanceTest}; at-scale plan
 * shape by {@code ChashProbePlanShapeTest}.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashRepositoryTest {

    private static final String TENANT_A = "chash-tenant-a";
    private static final String TENANT_B = "chash-tenant-b";
    private static final String MODEL_384 = "minilm-l6-v2-384";
    private static final String MODEL_1024 = "voyage-context-3";
    /** Never seeded — the fresh-install isEmpty guard. */
    private static final String TENANT_EMPTY = "chash-tenant-empty";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    ChashRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    private static Chash ch(String seed) {
        return Chash.ofText(seed);
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(PgContainerHelper.SVC_USERNAME);
        config.setPassword(PgContainerHelper.SVC_PASSWORD);
        // nexus-11gh6: bumped from 3 -- the new gate tests hold one external
        // connection open while a repo call blocks on the same pool.
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);

        tenantScope = new TenantScope(svcDs);
        repo = new ChashRepository(tenantScope);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String[] tc : new String[][] {
                    // RDR-225: a collection has ONE embedding model and a chunk's vector is that
                    // model's dimension, so the non-conformant fixture names register under the
                    // model their vectors are sized for (third element); the default is the
                    // 768-d bge fallback.
                    {TENANT_A, "coll-a-384", MODEL_384}, {TENANT_A, "coll-a-768"},
                    {TENANT_A, "coll-a-1024", MODEL_1024}, {TENANT_A, "stub-no-chunks"},
                    {TENANT_A, "ren-src", MODEL_384}, {TENANT_A, "ren-collide-src", MODEL_384},
                    {TENANT_A, "ren-collide-dst", MODEL_384},
                    {TENANT_B, "coll-b-384", MODEL_384},
                    // RDR-204 Phase 1 (bead nexus-ft04v.7): renameCollection's
                    // NEW-side name now requires a real catalog_collections row
                    // (ChashRepository's stub-insert is retired) — pre-register
                    // every rename DESTINATION these tests target, plus the
                    // real-ingest-path fixture below.
                    {TENANT_A, "ren-dst", MODEL_384},
                    {TENANT_A, "gate-rename-old-1-dst"},
                    {TENANT_A, "gate-rename-new-1-dst"},
                    {TENANT_A, "gate-deadlock-a"}, {TENANT_A, "gate-deadlock-b"},
                    {TENANT_A, "code__cr__minilm-l6-v2-384__v1"}}) {
                if (tc.length > 2) {
                    PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tc[0], tc[1], tc[2]);
                } else {
                    PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tc[0], tc[1]);
                }
            }

            // Multi-collection chash: 384 + 1024.
            chunk(su, TENANT_A, 384,  "coll-a-384",  ch("multi"), OffsetDateTime.parse("2026-07-01T00:00:01Z"));
            chunk(su, TENANT_A, 1024, "coll-a-1024", ch("multi"), OffsetDateTime.parse("2026-07-01T00:00:02Z"));
            // Singles per dim table.
            chunk(su, TENANT_A, 384,  "coll-a-384",  ch("only-384"),  OffsetDateTime.parse("2026-07-01T00:00:03Z"));
            chunk(su, TENANT_A, 768,  "coll-a-768",  ch("only-768"),  OffsetDateTime.parse("2026-07-01T00:00:04Z"));
            chunk(su, TENANT_A, 1024, "coll-a-1024", ch("only-1024"), OffsetDateTime.parse("2026-07-01T00:00:05Z"));
            // Rename source: two rows under one collection name, plus a
            // manifest row — the rename must re-home the combined-query join
            // key too (nexus-x6kdz class; .3 critique S2). RDR-225: both rows are 384-d,
            // since a collection has exactly one model (the pre-RDR-225 fixture mixed a 384
            // and a 768 row under one name, the cross-model re-embed history that layout
            // can no longer hold).
            chunk(su, TENANT_A, 384, "ren-src", ch("ren-1"), OffsetDateTime.parse("2026-07-01T00:00:06Z"));
            chunk(su, TENANT_A, 384, "ren-src", ch("ren-2"), OffsetDateTime.parse("2026-07-01T00:00:07Z"));
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT_A)
                .set(CATALOG_DOCUMENTS.TUMBLER, "ren-doc-1")
                .set(CATALOG_DOCUMENTS.TITLE, "Ren Doc")
                .set(CATALOG_DOCUMENTS.AUTHOR, "a")
                .set(CATALOG_DOCUMENTS.YEAR, 2026)
                .set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.CORPUS, "research")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "ren-src")
                .execute();
            seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT_A)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "ren-doc-1")
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, ch("ren-1").toBytes())
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, "ren-src")
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                    PgContainerHelper.collectionModel(seed, TENANT_A, "ren-src"))
                .execute();
            // Rename collision fixture: same chash on both sides.
            chunk(su, TENANT_A, 384, "ren-collide-src", ch("collide"),   OffsetDateTime.parse("2026-07-01T00:00:08Z"));
            chunk(su, TENANT_A, 384, "ren-collide-src", ch("collide-2"), OffsetDateTime.parse("2026-07-01T00:00:09Z"));
            chunk(su, TENANT_A, 384, "ren-collide-dst", ch("collide"),   OffsetDateTime.parse("2026-07-01T00:00:10Z"));
            // Tenant B: same chash value as tenant A's multi — isolation.
            chunk(su, TENANT_B, 384, "coll-b-384", ch("multi"), OffsetDateTime.parse("2026-07-01T00:00:11Z"));
        }
    }

    // ── lookup ───────────────────────────────────────────────────────────────

    @Test
    void lookup_returnsAllCollections_acrossDimTables() {
        List<Map<String, String>> rows = repo.lookup(TENANT_A, ch("multi"));
        assertThat(rows).hasSize(2);
        assertThat(rows).extracting(r -> r.get("collection"))
            .containsExactlyInAnyOrder("coll-a-384", "coll-a-1024");
        assertThat(rows).extracting(r -> r.get("created_at"))
            .containsExactlyInAnyOrder("2026-07-01T00:00:01Z", "2026-07-01T00:00:02Z");
    }

    @Test
    void lookup_unknownChash_returnsEmpty() {
        assertThat(repo.lookup(TENANT_A, ch("never-seeded"))).isEmpty();
    }

    // ── distinct_collections ─────────────────────────────────────────────────

    @Test
    void distinctCollections_returnsChunkBearingOnly() {
        // Only the coll-a-* fixtures are asserted here: the ren-* fixtures are
        // owned (and re-homed) by the rename tests, and JUnit method order is
        // not fixed in this class.
        Set<String> collections = repo.distinctCollections(TENANT_A);
        assertThat(collections).contains(
            "coll-a-384", "coll-a-768", "coll-a-1024");
        assertThat(collections)
            .as("a zero-chunk registry stub is not a chash-bearing collection")
            .doesNotContain("stub-no-chunks");
        assertThat(collections)
            .as("tenant isolation")
            .doesNotContain("coll-b-384");
    }

    // ── rename_collection ────────────────────────────────────────────────────

    @Test
    void renameCollection_rehomesEveryRow_andIsIdempotent() {
        int updated = repo.renameCollection(TENANT_A, "ren-src", "ren-dst");
        assertThat(updated).as("both rows of the source collection re-homed").isEqualTo(2);

        assertThat(repo.lookup(TENANT_A, ch("ren-1")))
            .extracting(r -> r.get("collection")).containsExactly("ren-dst");
        assertThat(repo.lookup(TENANT_A, ch("ren-2")))
            .extracting(r -> r.get("collection")).containsExactly("ren-dst");
        assertThat(repo.countForCollection(TENANT_A, "ren-src")).isZero();

        // Second call = the cascade-already-ran topology (RDR-187 Q3): the
        // source is empty, the rename is a clean 0-row no-op, no error.
        assertThat(repo.renameCollection(TENANT_A, "ren-src", "ren-dst")).isZero();

        // The new collection was registered in-transaction (fk-002 RESTRICT
        // would have rejected the re-home otherwise) — visible to the registry.
        assertThat(repo.distinctCollections(TENANT_A)).contains("ren-dst");

        // The manifest's denormalized collection re-homed too — renaming
        // chunks without it strands the combined-query join key on the old
        // name (nexus-x6kdz class; .3 critique S2).
        assertThat(manifestRowsIn("ren-dst")).isEqualTo(1);
        assertThat(manifestRowsIn("ren-src")).isZero();
    }

    @Test
    void renameCollection_collisionDefense_newSideRowWins() {
        int updated = repo.renameCollection(TENANT_A, "ren-collide-src", "ren-collide-dst");
        // 'collide' collided (dst already holds it — src copy dropped, dst
        // row survives with ITS created_at); only 'collide-2' re-homed.
        assertThat(updated).isEqualTo(1);

        List<Map<String, String>> collide = repo.lookup(TENANT_A, ch("collide"));
        assertThat(collide).hasSize(1);
        assertThat(collide.get(0).get("collection")).isEqualTo("ren-collide-dst");
        assertThat(collide.get(0).get("created_at"))
            .as("the surviving row is the destination's (its created_at kept)")
            .isEqualTo("2026-07-01T00:00:10Z");
        assertThat(repo.lookup(TENANT_A, ch("collide-2")))
            .extracting(r -> r.get("collection")).containsExactly("ren-collide-dst");
        assertThat(repo.countForCollection(TENANT_A, "ren-collide-src")).isZero();
    }

    @Test
    void renameCollection_blankArguments_rejected() {
        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, "", "x"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, "x", " "))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void renameCollection_unregisteredNewSide_throwsAndWritesNoRow() {
        // RDR-204 Phase 1 (nexus-ft04v.7 gap 3, closed by nexus-ft04v.8's test
        // addendum): renameCollection's NEW-side ensureCollectionRegistered
        // guard runs before any row is re-homed -- a never-registered
        // destination must fail loud (UnregisteredCollectionException) and
        // create no catalog_collections row for it, whether or not the
        // source collection carries any chunks.
        String oldCollection = "chr-unreg-src";
        String newCollection = "chr-unreg-dst";

        assertThatThrownBy(() -> repo.renameCollection(TENANT_A, oldCollection, newCollection))
            .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class)
            .hasMessageContaining(newCollection)
            .hasMessageContaining("POST /v1/catalog/collections/upsert");

        boolean stubRowExists = tenantScope.withTenant(TENANT_A, ctx -> ctx.fetchExists(
                ctx.selectOne().from(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                   .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A))
                   .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(newCollection))));
        assertThat(stubRowExists)
            .as("the rejected rename must not have created a catalog_collections stub row for the new side")
            .isFalse();
    }

    // ── is_empty / count_for_collection ──────────────────────────────────────

    @Test
    void isEmpty_trueForFreshTenant_falseForSeeded() {
        assertThat(repo.isEmpty(TENANT_EMPTY)).isTrue();
        assertThat(repo.isEmpty(TENANT_A)).isFalse();
    }

    @Test
    void countForCollection_exactAndZeroForUnknown() {
        assertThat(repo.countForCollection(TENANT_A, "coll-a-384")).isEqualTo(2);
        assertThat(repo.countForCollection(TENANT_A, "coll-a-768")).isEqualTo(1);
        assertThat(repo.countForCollection(TENANT_A, "no-such-collection")).isZero();
    }

    // ── registered_chashes_for_collection ────────────────────────────────────

    @Test
    void registeredChashes_distinct64HexDigests() {
        Set<String> chashes = repo.registeredChashesForCollection(TENANT_A, "coll-a-384");
        assertThat(chashes).containsExactlyInAnyOrder(
            ch("multi").toHex(), ch("only-384").toHex());
        assertThat(chashes).allSatisfy(h -> assertThat(h).hasSize(64));
        assertThat(repo.registeredChashesForCollection(TENANT_A, "no-such-collection")).isEmpty();
        assertThatThrownBy(() -> repo.registeredChashesForCollection(TENANT_A, " "))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── created_at first-insert invariant (RDR-187 research finding 1) ───────

    @Test
    void createdAt_isFirstInsertPerKey_stableUnderReupsert() {
        // The lookup's created_at contract ("when this chash entered this
        // collection") holds ONLY because both chunk upsert ON CONFLICT
        // set-lists exclude created_at. This is the survivor regression for
        // RDR-187 design Q1 (.3 critique S3: the property previously had no
        // test anywhere — a future "fix" refreshing created_at on re-index
        // would silently invalidate the closed design question). Drives the
        // REAL ingest path (PgVectorRepository.upsertChunksWithVectors), not
        // raw SQL.
        String collection = "code__cr__minilm-l6-v2-384__v1"; // 4-segment conformant, 384-dim
        var vectorRepo = new dev.nexus.service.vectors.PgVectorRepository(
            tenantScope,
            (dev.nexus.service.vectors.Embedder) null,
            (dev.nexus.service.vectors.Embedder) null);
        Chash chash = ch("created-at-stability");
        float[] vec = new float[384];
        vec[0] = 1.0f;

        vectorRepo.upsertChunksWithVectors(TENANT_A, collection,
            List.of(chash.toHex()), List.of("created-at probe text"),
            List.of(vec), List.of(Map.of()));
        assertThat(repo.lookup(TENANT_A, chash)).hasSize(1);
        // Compare at RAW microsecond precision, superuser-side — the lookup's
        // second-precision format would be vacuously equal for two upserts
        // inside the same wall-clock second.
        String firstCreatedAt = rawCreatedAt(chash, collection);
        assertThat(firstCreatedAt).isNotBlank();

        // Re-upsert the same (tenant, collection, chash) with different text.
        vectorRepo.upsertChunksWithVectors(TENANT_A, collection,
            List.of(chash.toHex()), List.of("created-at probe text v2"),
            List.of(vec), List.of(Map.of()));
        assertThat(rawCreatedAt(chash, collection))
            .as("created_at must be first-insert-per-key: re-upsert may not refresh it")
            .isEqualTo(firstCreatedAt);
    }

    /** Raw microsecond-precision created_at for one (chash, collection), superuser-side. */
    private String rawCreatedAt(Chash chash, String collection) {
        try (Connection su = pg.createConnection("")) {
            var createdAtText = CHUNKS.CREATED_AT.cast(org.jooq.impl.SQLDataType.CLOB);
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(createdAtText).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A), CHUNKS.COLLECTION.eq(collection),
                       CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash.toHex())))
                .fetch(createdAtText);
            if (rows.isEmpty()) throw new IllegalStateException("row not found");
            return rows.get(0);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ── RLS isolation ────────────────────────────────────────────────────────

    @Test
    void rls_tenantRowsInvisibleAcrossTenants() {
        assertThat(repo.lookup(TENANT_B, ch("multi")))
            .extracting(r -> r.get("collection"))
            .containsExactly("coll-b-384");
        assertThat(repo.countForCollection(TENANT_B, "coll-a-384")).isZero();
        assertThat(repo.registeredChashesForCollection(TENANT_B, "coll-a-384")).isEmpty();
    }

    // ── nexus-11gh6 post-review (T2 nexus/review-11gh6-gate-2026-08-08
    //    [21797] Important finding): renameCollection is a SECOND,
    //    independently-reachable (via /v1/chash/*) re-home implementation
    //    doing the same mutation class as CatalogRepository.renameCollectionTxn
    //    -- it must take the SAME sweep gate, on BOTH endpoints, sorted. ──

    /** Raw connection to the service role's own pool (test-controlled transaction). */
    private Connection dsConnection() throws SQLException {
        return svcDs.getConnection();
    }

    /** Hand-drives {@code CatalogRepository.acquireSweepGateExclusive}'s exact
     *  SQL shape (through jOOQ on that connection), for tests needing manual transaction control. */
    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs)
            throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", String.class, DSL.inline("lock_timeout"),
                DSL.val(String.valueOf(lockTimeoutMs)), DSL.inline(true))).fetch();
        ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection)))).fetch();
    }

    private void assertRenameBlocksOnExternalExclusiveGate(String gatedCollection,
            String oldCollection, String newCollection) throws Exception {
        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            PgContainerHelper.setTenant(external, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            acquireGateExclusive(external, TENANT_A, gatedCollection, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Integer> future = executor.submit(
                    () -> repo.renameCollection(TENANT_A, oldCollection, newCollection));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("renameCollection must BLOCK while '" + gatedCollection
                        + "' is held EXCLUSIVE externally")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                future.get(15, TimeUnit.SECONDS);
            } finally {
                executor.shutdownNow();
            }
        }
    }

    @Test
    void renameCollection_blocksOnExternalExclusiveGate_onOldCollection_thenProceeds() throws Exception {
        assertRenameBlocksOnExternalExclusiveGate(
            "gate-rename-old-1", "gate-rename-old-1", "gate-rename-old-1-dst");
    }

    @Test
    void renameCollection_blocksOnExternalExclusiveGate_onNewCollection_thenProceeds() throws Exception {
        assertRenameBlocksOnExternalExclusiveGate(
            "gate-rename-new-1-dst", "gate-rename-new-1", "gate-rename-new-1-dst");
    }

    /**
     * SANITY CHECK, NOT A DISCRIMINATING REGRESSION TEST (renamed post-review
     * — T2 nexus/critique-11gh6-gate-impl-2026-08-08 [21798] REWORK DELTA
     * item (d)): both endpoints are taken SHARED, never upgraded, and shared
     * locks never conflict with EACH OTHER regardless of acquisition order —
     * there is, structurally, NO deadlock possible here today whether or not
     * the two collection names are sorted before acquisition. The critic
     * confirmed this test would pass IDENTICALLY whether or not the
     * {@code sorted().distinct()} call in {@link
     * dev.nexus.service.db.ChashRepository#renameCollection} were removed —
     * it cannot falsify a hypothetical "sorting regression" given the
     * current all-SHARED lock set (mirrors {@code
     * CatalogRepository.renameCollectionTxn}'s own §5.1 deadlock analysis,
     * T2 nexus/design-11gh6-sweep-write-skew-closure [21768]). What it DOES
     * legitimately prove: two opposite-direction renames sharing both
     * endpoints, run concurrently, complete without a PostgreSQL deadlock
     * abort (which WOULD surface as a thrown exception from one of the
     * futures) — a real, if non-discriminating, sanity check. The sort call
     * remains pure future-proofing for a hypothetical later EXCLUSIVE
     * acquisition on this path, not a defense this test can verify.
     */
    @Test
    void renameCollection_concurrentOppositeDirectionRenames_completeWithoutIncident() throws Exception {
        String a = "gate-deadlock-a";
        String b = "gate-deadlock-b";
        ExecutorService executor = Executors.newFixedThreadPool(2);
        try {
            Future<Integer> f1 = executor.submit(() -> repo.renameCollection(TENANT_A, a, b));
            Future<Integer> f2 = executor.submit(() -> repo.renameCollection(TENANT_A, b, a));
            assertThat(f1.get(15, TimeUnit.SECONDS)).isGreaterThanOrEqualTo(0);
            assertThat(f2.get(15, TimeUnit.SECONDS)).isGreaterThanOrEqualTo(0);
        } finally {
            executor.shutdownNow();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    // RDR-191 Phase 4 (lane D5): nexus.chunks_384/768/1024 collapsed into
    // ONE unified nexus.chunks table — dim now selects the embedding_<dim>
    // COLUMN, not the target table.
    private void chunk(Connection su, String tenant, int dim, String collection,
                       Chash chash, OffsetDateTime createdAt) throws Exception {
        var ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash.toHex()))
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
            .set(CHUNKS.CHUNK_TEXT, "chunk " + chash.toHex().substring(0, 8))
            .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vecXY(dim, 1f, 0f))
            .set(CHUNKS.CREATED_AT, createdAt)
            .execute();
    }

    /** catalog_document_chunks rows TENANT_A holds for {@code collection}, superuser-side. */
    private int manifestRowsIn(String collection) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A), CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}

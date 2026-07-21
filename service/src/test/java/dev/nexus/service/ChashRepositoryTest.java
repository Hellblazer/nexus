package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-187 bead nexus-piwya.3 — ChashRepository integration tests for the
 * chunks-backed reroute.
 *
 * <p>The write surface (upsert/upsertMany/doImport/deleteCollection/
 * deleteStale) is GONE — the router table was the only thing those wrote; the
 * chunks tables are written by the vector ingest paths. What remains, all
 * served from {@code chunks_384/768/1024} under RLS via the real
 * {@code nexus_svc} role:
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
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

        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(PgContainerHelper.SVC_USERNAME);
        config.setPassword(PgContainerHelper.SVC_PASSWORD);
        config.setMaximumPoolSize(3);
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
                    {TENANT_A, "coll-a-384"}, {TENANT_A, "coll-a-768"},
                    {TENANT_A, "coll-a-1024"}, {TENANT_A, "stub-no-chunks"},
                    {TENANT_A, "ren-src"}, {TENANT_A, "ren-collide-src"},
                    {TENANT_A, "ren-collide-dst"},
                    {TENANT_B, "coll-b-384"}}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + tc[0] + "', '" + tc[1] + "') " +
                    "ON CONFLICT (tenant_id, name) DO NOTHING");
            }

            // Multi-collection chash: 384 + 1024.
            chunk(su, TENANT_A, 384,  "coll-a-384",  ch("multi"), "2026-07-01 00:00:01+00");
            chunk(su, TENANT_A, 1024, "coll-a-1024", ch("multi"), "2026-07-01 00:00:02+00");
            // Singles per dim table.
            chunk(su, TENANT_A, 384,  "coll-a-384",  ch("only-384"),  "2026-07-01 00:00:03+00");
            chunk(su, TENANT_A, 768,  "coll-a-768",  ch("only-768"),  "2026-07-01 00:00:04+00");
            chunk(su, TENANT_A, 1024, "coll-a-1024", ch("only-1024"), "2026-07-01 00:00:05+00");
            // Rename source: rows in two dim tables under one collection name
            // (cross-model re-embed history makes this shape real), plus a
            // manifest row — the rename must re-home the combined-query join
            // key too (nexus-x6kdz class; .3 critique S2).
            chunk(su, TENANT_A, 384, "ren-src", ch("ren-1"), "2026-07-01 00:00:06+00");
            chunk(su, TENANT_A, 768, "ren-src", ch("ren-2"), "2026-07-01 00:00:07+00");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents " +
                "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
                "VALUES ('" + TENANT_A + "', 'ren-doc-1', 'Ren Doc', 'a', 2026, " +
                "'paper', 'research', 'ren-src')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) " +
                "VALUES ('" + TENANT_A + "', 'ren-doc-1', 0, decode('" + ch("ren-1").toHex() + "', 'hex'), 'ren-src')");
            // Rename collision fixture: same chash on both sides.
            chunk(su, TENANT_A, 384, "ren-collide-src", ch("collide"),   "2026-07-01 00:00:08+00");
            chunk(su, TENANT_A, 384, "ren-collide-src", ch("collide-2"), "2026-07-01 00:00:09+00");
            chunk(su, TENANT_A, 384, "ren-collide-dst", ch("collide"),   "2026-07-01 00:00:10+00");
            // Tenant B: same chash value as tenant A's multi — isolation.
            chunk(su, TENANT_B, 384, "coll-b-384", ch("multi"), "2026-07-01 00:00:11+00");
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
    void renameCollection_rehomesAcrossDimTables_andIsIdempotent() {
        int updated = repo.renameCollection(TENANT_A, "ren-src", "ren-dst");
        assertThat(updated).as("one 384 row + one 768 row re-homed").isEqualTo(2);

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
        assertThat(countRows(
            "SELECT count(*) FROM nexus.catalog_document_chunks " +
            "WHERE tenant_id = '" + TENANT_A + "' AND collection = 'ren-dst'"))
            .isEqualTo(1);
        assertThat(countRows(
            "SELECT count(*) FROM nexus.catalog_document_chunks " +
            "WHERE tenant_id = '" + TENANT_A + "' AND collection = 'ren-src'"))
            .isZero();
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
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(
                "SELECT created_at::text FROM nexus.chunks_384 " +
                "WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + collection + "' " +
                "  AND chash = decode('" + chash.toHex() + "', 'hex')")) {
            if (!rs.next()) throw new IllegalStateException("row not found");
            return rs.getString(1);
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

    // ── helpers ──────────────────────────────────────────────────────────────

    private void chunk(Connection su, String tenant, int dim, String collection,
                       Chash chash, String createdAt) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding, created_at) VALUES " +
            "('" + tenant + "', '" + collection + "', decode('" + chash.toHex() + "', 'hex'), " +
            "'chunk " + chash.toHex().substring(0, 8) + "', " + unitVec(dim) + "::vector, " +
            "TIMESTAMPTZ '" + createdAt + "')");
    }

    private static String unitVec(int dim) {
        StringBuilder sb = new StringBuilder("'[1");
        for (int i = 1; i < dim; i++) sb.append(",0");
        return sb.append("]'").toString();
    }

    private int countRows(String sql) {
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}

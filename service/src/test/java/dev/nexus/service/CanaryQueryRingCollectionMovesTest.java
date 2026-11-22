// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4lnn1.1.2 (RDR-230 Phase 1 P1.2a): a collection's {@code nexus.canary_query_ring} rows follow the
 * collection through the two operations that move rows between collection names, through
 * {@link CatalogRepository#renameCollection} and {@link CatalogRepository#rehomeCollection}. The ring is an
 * entry of {@code CatalogRepository.COLLECTION_SCOPED_TABLES}, so both operations move it with the rest, and the
 * tests drive the real operations; no row is moved or deleted by the test itself.
 *
 * <p>The ring has a foreign key to the registry on (tenant_id, collection, embedding_model). A rename satisfies it
 * (the new registry row is written first and copies the model). A re-home between two collections of one model
 * satisfies it. A re-home between two collections of different models cannot move a ring row, and the ring is the
 * only registered table that can be the whole of such a source (every other model-keyed table is reached through
 * {@code chunks}, which that guard already refuses), so the re-home drops the source's ring there instead of failing
 * on the foreign key. The ghost sweep is covered by {@code GhostSweepDormantMarkingTest}, which seeds every
 * registered table and now includes this one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CanaryQueryRingCollectionMovesTest {

    private static final String SVC_ROLE = "svc_ring_moves_test";
    private static final String SVC_PASS = "svc_ring_moves_test_pass";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
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
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── rename ──────────────────────────────────────────────────────────

    @Test
    void rename_movesTheRingRowsToTheNewName_andReportsThemUnderTheirKey() throws Exception {
        String tenant = "ring-move-rename";
        String oldName = "knowledge__ringren__minilm-l6-v2-384__v1";
        String newName = "knowledge__ringren__minilm-l6-v2-384__v2";
        seed(tenant, oldName, "ren", 3);

        Map<String, Integer> counts = repo.renameCollection(tenant, oldName, newName);

        assertThat(counts).as("the per-table counts of the rename carry the ring").containsEntry("canary_query_ring", 3);
        assertThat(digests(tenant, newName)).as("all three rows now name the new collection")
            .isEqualTo(digestSet("ren", 3));
        assertThat(digests(tenant, oldName)).as("none is left under the old name (a tombstone row stays in the registry)")
            .isEmpty();
    }

    // ── re-home, same model ─────────────────────────────────────────────

    /**
     * Gate S5. The ring's key is (tenant_id, collection, vector_digest), so a digest present in the ring of the
     * source AND the ring of the target is a collision: the move leaves the source's row where it is and counts it
     * in {@code leftBehindByTable}, as it does for any collection-keyed table. Everything else in the source's ring
     * moves.
     *
     * <p>THE WINDOW THIS LEAVES, written down on purpose: the target's ring can hold more than 32 rows after the
     * move (here its 33 plus the 5 that moved, 38), because a re-home never runs the function that trims. It
     * exceeds 32 rows only until the target's next write, which trims it back to the 32 newest (asserted below). No
     * reader depends on the bound: the canary reads whatever the ring holds, and the next write restores it.
     */
    @Test
    void rehome_aDigestOnBothSides_staysAtTheSourceAndIsCounted_andTheTargetMayExceed32UntilItsNextWrite()
            throws Exception {
        String tenant = "ring-move-rehome";
        String src = "knowledge__ringre-src__minilm-l6-v2-384__v1";
        String dst = "knowledge__ringre-dst__minilm-l6-v2-384__v1";
        // The source holds "shared" (the digest also at the target) and five rows of its own.
        seed(tenant, src, "src", 5);
        insert(tenant, src, "shared");
        // The target holds "shared" (the collision) and 32 rows of its own: 33 in all, already over the bound, as
        // a ring is after an earlier re-home.
        seed(tenant, dst, "dst", 32);
        insert(tenant, dst, "shared");

        CatalogRepository.RehomeResult r = repo.rehomeCollection(tenant, src, dst);

        assertThat(r.movedByTable()).as("the five source rows with no twin at the target moved")
            .containsEntry("canary_query_ring", 5);
        assertThat(r.leftBehindByTable()).as("REPORTED, not dropped: the digest present on both sides stayed")
            .containsEntry("canary_query_ring", 1);
        assertThat(digests(tenant, src)).as("the colliding row is still at the source")
            .isEqualTo(Set.of(hex(digest("shared"))));
        assertThat(digests(tenant, dst)).as("the target holds its 33 and the 5 that moved, 38 in all")
            .hasSize(38);
        assertThat(r.status().done()).as("the move is finished: nothing movable is left").isTrue();
        assertThat(r.status().remainingByTable()).containsEntry("canary_query_ring", 1);

        // The window closes at the target's next write: the function trims the ring to the 32 newest.
        int written = scope.withTenant(tenant, ctx -> Routines.canaryQueryRingUpsert(ctx.configuration(), tenant,
            new String[] {dst}, new String[] {model(tenant, dst)}, new String[] {vector(384).toString()},
            new byte[][] {digest("after-rehome")}));
        assertThat(written).isEqualTo(1);
        assertThat(digests(tenant, dst)).as("back to the bound after one write").hasSize(32).contains(hex(digest("after-rehome")));
    }

    // ── re-home, different models ───────────────────────────────────────

    @Test
    void rehome_acrossModels_dropsTheSourceRing_insteadOfFailingOnTheForeignKey() throws Exception {
        String tenant = "ring-move-xmodel";
        String src = "knowledge__ringxm-src__minilm-l6-v2-384__v1";
        String dst = "knowledge__ringxm-dst__bge-base-en-v15-768__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, src, "minilm-l6-v2-384");
            PgContainerHelper.insertCollection(ctx, tenant, dst, "bge-base-en-v15-768");
        }
        insert(tenant, src, "xm");
        insert(tenant, dst, "dst-own", 768);

        // The source has no chunks, so the model-mismatch refusal does not apply; its ring rows cannot move to a
        // collection of another model (the foreign key names the model), and they are vectors of the wrong space.
        CatalogRepository.RehomeResult r = repo.rehomeCollection(tenant, src, dst);

        assertThat(r.movedByTable()).containsEntry("canary_query_ring", 0);
        assertThat(digests(tenant, src)).as("the source's ring is dropped, a derived cache of another model").isEmpty();
        assertThat(digests(tenant, dst)).as("the target's own ring is untouched").isEqualTo(Set.of(hex(digest("dst-own"))));
        assertThat(r.status().done()).isTrue();
    }

    // ── fixtures ────────────────────────────────────────────────────────

    /** Registers {@code collection} and writes {@code n} ring rows {@code <prefix>-<i>} to it, 384 wide. */
    private void seed(String tenant, String collection, String prefix, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            String model = PgContainerHelper.collectionModel(ctx, tenant, collection);
            for (int i = 0; i < n; i++) {
                insertRow(ctx, tenant, collection, model, prefix + "-" + i, 384);
            }
        }
    }

    /** One ring row {@code seed} (384 wide) in an already registered collection. */
    private void insert(String tenant, String collection, String seed) throws Exception {
        insert(tenant, collection, seed, 384);
    }

    private void insert(String tenant, String collection, String seed, int dims) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(ctx, tenant, collection);
            insertRow(ctx, tenant, collection, model, seed, dims);
        }
    }

    private static void insertRow(DSLContext ctx, String tenant, String collection, String model, String seed,
                                  int dims) {
        var step = ctx.insertInto(CANARY_QUERY_RING)
            .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, collection)
            .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
            .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest(seed))
            .set(CANARY_QUERY_RING.ADDED_AT, OffsetDateTime.now());
        if (dims == 768) {
            step.set(CANARY_QUERY_RING.EMBEDDING_768, vector(dims)).execute();
        } else {
            step.set(CANARY_QUERY_RING.EMBEDDING_384, vector(dims)).execute();
        }
    }

    private String model(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> PgContainerHelper.collectionModel(ctx, tenant, collection));
    }

    private Set<String> digests(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> ctx.select(CANARY_QUERY_RING.VECTOR_DIGEST).from(CANARY_QUERY_RING)
            .where(CANARY_QUERY_RING.COLLECTION.eq(collection))
            .fetch(r -> hex(r.value1())).stream().collect(Collectors.toSet()));
    }

    private static Set<String> digestSet(String prefix, int n) {
        return java.util.stream.IntStream.range(0, n).mapToObj(i -> hex(digest(prefix + "-" + i)))
            .collect(Collectors.toSet());
    }

    private static Vector vector(int dims) {
        float[] v = new float[dims];
        for (int i = 0; i < dims; i++) v[i] = ((i * 7) % 89) / 100f + 0.01f;
        return Vector.of(v);
    }

    private static byte[] digest(String seed) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(seed.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hex(byte[] bytes) {
        return HexFormat.of().formatHex(bytes);
    }
}

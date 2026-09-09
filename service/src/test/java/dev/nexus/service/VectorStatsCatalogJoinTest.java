// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 2, bead nexus-ft04v.24 — {@code PgVectorRepository.collectionStats}
 * gains four catalog attributes ({@code content_type}, {@code owner_id}, {@code
 * embedding_model}, {@code lifecycle_state}) via a LEFT JOIN against {@code
 * nexus.catalog_collections}, additive to the existing {@code name}/{@code dim}/
 * {@code count}/{@code last_write} keys {@code GET /v1/vectors/stats} already serves.
 *
 * <p>Two scenarios pinned here:
 * <ul>
 *   <li>a collection WITH a {@code catalog_collections} row: the four keys are
 *       present and carry the row's values;</li>
 *   <li>a collection with live vector stats but NO {@code catalog_collections}
 *       row: the four keys are ABSENT (omitted, not JSON-{@code null} — the same
 *       "absent means absent" convention {@code last_write} already uses for a
 *       collection with no writes) and the pre-existing keys are unaffected. This
 *       is the LEFT-JOIN (not INNER) contract: the client's collection cache reads
 *       this exact route and its population must not shrink.</li>
 * </ul>
 *
 * <p>The second scenario cannot arise through the normal write path today —
 * {@code chunks_collection_fk} (fk-004, {@code ON DELETE RESTRICT}) requires a
 * collection to be registered in {@code catalog_collections} before any chunk
 * write, and blocks deleting that row while chunks remain — so the fixture below
 * constructs it directly, mirroring the dangling-manifest-row technique {@code
 * PgContainerHelper#dropConstraint}/{@code #addFkNotValid} already exist for
 * (their own javadoc names this exact drop-insert-readd shape): register, write
 * the chunk (satisfying the FK), momentarily {@link
 * PgContainerHelper#dropConstraint drop} {@code chunks_collection_fk}, delete the
 * {@code catalog_collections} row via typed DSL, then {@link
 * PgContainerHelper#addFkNotValid re-add} the constraint — leaving the {@code
 * collection_vector_stats} row an orphan on purpose. This proves the LEFT JOIN's
 * defensive behavior even though the orphan state is not reachable via any code
 * path in this repository today.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle, plain
 * NOSUPERUSER NOBYPASSRLS svc role for every {@link PgVectorRepository} call
 * (RLS-subject, matching {@code PgVectorRepositoryContractTest}'s own posture).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorStatsCatalogJoinTest {

    private static final String SVC_ROLE = "svc_vscj_test";
    private static final String SVC_PASS = "svc_vscj_test_pass";
    private static final String TENANT   = "vscj-tenant";

    // Conformant name -> PgContainerHelper.insertCollection derives content_type=code,
    // owner_id=vscj-owner, embedding_model=voyage-code-3 (a real embedding_models row),
    // lifecycle_state=live.
    private static final String COL_REGISTERED = "code__vscj-owner__voyage-code-3__v1";
    // Non-conformant name -> insertCollection's "unknown" branch: content_type=unknown,
    // owner_id=TENANT, embedding_model=bge-base-en-v15-768 (fallback), lifecycle_state=live.
    // Its catalog_collections row is deleted after the chunk write (see fixture below),
    // leaving an orphan stats row on purpose.
    private static final String COL_ORPHAN = "vscj-orphan-coll";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    PgVectorRepository repo;

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
        tenantScope = new TenantScope(svcDs);
        // collectionStats() never touches the embedder — a plain, non-router
        // constructor with null embedders is sufficient for this suite.
        repo = new PgVectorRepository(tenantScope, null, null);

        // ── Fixture: COL_REGISTERED — real catalog row, one chunk ───────────────
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COL_REGISTERED);
            insertChunk1024(ctx, TENANT, COL_REGISTERED, chashBytes("vscj-reg-c1"), vector(1024));
        }

        // ── Fixture: COL_ORPHAN — register, write chunk (FK requires the row),
        //    then remove the catalog_collections row underneath the stats row,
        //    momentarily dropping chunks_collection_fk (re-added after) — the
        //    same drop-insert-readd shape PgContainerHelper's dangling-row
        //    helpers already document. ──────────────────────────────────────
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COL_ORPHAN);
            insertChunk384(ctx, TENANT, COL_ORPHAN, chashBytes("vscj-orphan-c1"), vector(384));
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_fk");
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(COL_ORPHAN)))
               .execute();
            PgContainerHelper.addFkNotValid(su, CHUNKS, "chunks_collection_fk", "collection",
                CATALOG_COLLECTIONS, "name", "ON DELETE RESTRICT");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    @Test
    void statsCarriesCatalogAttributes_forCollectionWithCatalogRow() {
        List<Map<String, Object>> stats = repo.collectionStats(TENANT);
        Map<String, Object> row = stats.stream()
            .filter(s -> COL_REGISTERED.equals(s.get("name")))
            .findFirst()
            .orElseThrow(() -> new AssertionError("stats must contain " + COL_REGISTERED + " (got: " + stats + ")"));

        // Pre-existing keys unaffected.
        assertThat(((Number) row.get("dim")).intValue()).isEqualTo(1024);
        assertThat(((Number) row.get("count")).longValue()).isEqualTo(1L);
        assertThat((String) row.get("last_write")).isNotEmpty();

        // New, joined keys.
        assertThat(row.get("content_type")).as("conformant name's content_type token").isEqualTo("code");
        assertThat(row.get("owner_id")).as("conformant name's owner token").isEqualTo("vscj-owner");
        assertThat(row.get("embedding_model")).as("conformant name's model token, a real embedding_models row")
            .isEqualTo("voyage-code-3");
        assertThat(row.get("lifecycle_state")).as("a freshly-registered row is live").isEqualTo("live");
    }

    @Test
    void statsOmitsCatalogAttributes_forCollectionWithNoCatalogRow() {
        List<Map<String, Object>> stats = repo.collectionStats(TENANT);
        Map<String, Object> row = stats.stream()
            .filter(s -> COL_ORPHAN.equals(s.get("name")))
            .findFirst()
            .orElseThrow(() -> new AssertionError("stats must still contain " + COL_ORPHAN +
                " (LEFT JOIN, not INNER — the client's cache population must not shrink) (got: " + stats + ")"));

        // Pre-existing keys STILL present — the LEFT JOIN must not suppress the row.
        assertThat(((Number) row.get("dim")).intValue()).isEqualTo(384);
        assertThat(((Number) row.get("count")).longValue()).isEqualTo(1L);
        assertThat((String) row.get("last_write")).isNotEmpty();

        // The four joined keys are ABSENT (omitted), not null-valued.
        assertThat(row)
            .as("no catalog_collections row exists for " + COL_ORPHAN +
                " — the four joined keys must be absent, not present-with-null")
            .doesNotContainKeys("content_type", "owner_id", "embedding_model", "lifecycle_state");
    }

    // ── HELPERS (typed jOOQ DSL — mirrors PgContainerHelper.insertChunk384/1024
    //    and CatalogDeleteCollectionCascadeTest's own chashBytes/vector pair) ───

    private static void insertChunk384(org.jooq.DSLContext ctx, String tenant, String collection,
                                        byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    private static void insertChunk1024(org.jooq.DSLContext ctx, String tenant, String collection,
                                         byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_1024)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    /** A pgvector value with every one of {@code dim} components equal to {@code 0.1}. */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /** 32-char hex-alphabet label, stored as its own ASCII bytes (no real hash semantics —
     *  matches {@code CatalogDeleteCollectionCascadeTest#chashBytes}). */
    private static byte[] chashBytes(String seed) {
        String label = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return label.getBytes(StandardCharsets.US_ASCII);
    }
}

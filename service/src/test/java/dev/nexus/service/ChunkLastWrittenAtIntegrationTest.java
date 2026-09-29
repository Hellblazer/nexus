// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.DimTables;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.43: {@code nexus.chunks.last_written_at}, the key RDR-192's
 * {@code reapable(c)} grace window reads. It is refreshed ONLY when a client
 * write re-writes a chunk that already exists, and never by maintenance or
 * stamping updates (which would keep dead chunks alive), while {@code
 * created_at} stays write-once.
 *
 * <p>Every refresh test follows one shape: write the chunk through the real
 * repository method, push {@code created_at} and {@code last_written_at}
 * into the past with a direct superuser UPDATE, re-write the same chash
 * through the same method, and read both timestamps back. The maintenance
 * test runs the same shape through the metadata-only stamping path and
 * asserts the timestamp did NOT move.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLastWrittenAtIntegrationTest {

    private static final String SVC_ROLE = "svc_lwa_test";
    private static final String SVC_PASS = "svc_lwa_test_pass";
    private static final String TENANT = "lwa-tenant";

    private static final String COL_UPSERT_FORCE = "code__lwa-force__minilm-l6-v2-384__v1";
    private static final String COL_UPSERT_DIVERGENT = "code__lwa-divergent__minilm-l6-v2-384__v1";
    private static final String COL_UPSERT_HAVE_VECTOR = "code__lwa-havevec__minilm-l6-v2-384__v1";
    private static final String COL_REF_ONLY = "code__lwa-refonly__minilm-l6-v2-384__v1";
    private static final String COL_CW_IDENTICAL = "code__lwa-cwident__minilm-l6-v2-384__v1";
    private static final String COL_CW_DIVERGENT = "code__lwa-cwdiverge__minilm-l6-v2-384__v1";
    private static final String COL_MAINTENANCE = "code__lwa-maint__minilm-l6-v2-384__v1";
    private static final String COL_FRESH = "code__lwa-fresh__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository catalog;
    private CombinedWriteService combined;
    private PgVectorRepository vectors;

    /** One row's two timestamps, read with the database's own now() for the recency test. */
    private record Stamps(OffsetDateTime createdAt, OffsetDateTime lastWrittenAt, OffsetDateTime dbNow) {
        /** last_written_at within five minutes of the database clock. */
        boolean recent() {
            return lastWrittenAt.isAfter(dbNow.minusMinutes(5));
        }
    }

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
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalog = new CatalogRepository(tenantScope);
        var embedder = new CombinedWriteRepositoryTest.CountingFakeEmbedder();
        combined = new CombinedWriteService(tenantScope, catalog, new EmbedderRouter(embedder, "document"));
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);

        for (String col : List.of(COL_UPSERT_FORCE, COL_UPSERT_DIVERGENT, COL_UPSERT_HAVE_VECTOR,
                COL_REF_ONLY, COL_CW_IDENTICAL, COL_CW_DIVERGENT, COL_MAINTENANCE, COL_FRESH)) {
            tenantScope.withTenant(TENANT, ctx -> {
                PgContainerHelper.insertCollection(ctx, TENANT, col);
                return null;
            });
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── schema shape ─────────────────────────────────────────────────────────

    @Test
    void column_isTimestamptzNotNullDefaultNow() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var info = PgCatalogProbes.columnInfo(ctx, "nexus", "chunks", "last_written_at");
            assertThat(info).as("nexus.chunks.last_written_at must exist after Liquibase").isNotNull();
            assertThat(info.dataType()).isEqualTo("timestamp with time zone");
            assertThat(info.nullable()).as("NOT NULL: every row carries a grace anchor").isFalse();
            assertThat(info.columnDefault()).as("DEFAULT now(): an insert is fresh by construction")
                .contains("now()");
        }
    }

    @Test
    void freshInsert_takesTheDefault_andBothTimestampsStartTogether() throws Exception {
        String chash = ch("fresh");
        vectors.upsertChunks(TENANT, COL_FRESH, List.of(chash), List.of("fresh text"), List.of(Map.of()));
        Stamps s = stamps(COL_FRESH, chash);
        assertThat(s.recent()).as("a new row's last_written_at is now()").isTrue();
        assertThat(s.lastWrittenAt()).isEqualTo(s.createdAt());
    }

    // ── refreshing paths: client re-writes of an existing chunk ──────────────

    @Test
    void upsertChunks_onConflictContentPath_refreshesLastWrittenAt_notCreatedAt() throws Exception {
        String chash = ch("force");
        vectors.upsertChunks(TENANT, COL_UPSERT_FORCE, List.of(chash), List.of("same text"), List.of(Map.of()));
        Stamps aged = age(COL_UPSERT_FORCE, chash);

        // forceReEmbed=true skips the have-vector shortcut, so the row goes through
        // the INSERT ... ON CONFLICT DO UPDATE of upsertChunksInternal.
        vectors.upsertChunks(TENANT, COL_UPSERT_FORCE, List.of(chash), List.of("same text"),
            List.of(Map.of()), true);

        assertRefreshed(aged, stamps(COL_UPSERT_FORCE, chash));
    }

    @Test
    void upsertChunks_contentDivergentRewrite_refreshesLastWrittenAt() throws Exception {
        String chash = ch("divergent");
        vectors.upsertChunks(TENANT, COL_UPSERT_DIVERGENT, List.of(chash), List.of("first text"),
            List.of(Map.of()));
        Stamps aged = age(COL_UPSERT_DIVERGENT, chash);

        vectors.upsertChunks(TENANT, COL_UPSERT_DIVERGENT, List.of(chash), List.of("second text"),
            List.of(Map.of()));

        assertRefreshed(aged, stamps(COL_UPSERT_DIVERGENT, chash));
    }

    @Test
    void upsertChunks_haveVectorMetadataOnlyBranch_refreshesLastWrittenAt() throws Exception {
        String chash = ch("havevec");
        vectors.upsertChunks(TENANT, COL_UPSERT_HAVE_VECTOR, List.of(chash), List.of("stable text"),
            List.of(Map.of("v", "1")));
        Stamps aged = age(COL_UPSERT_HAVE_VECTOR, chash);

        // Identical text, no force: resolveNeedEmbedIdx's have-vector branch writes
        // metadata only through batchUpdateMetadata. A re-index of an unchanged file
        // is exactly this call, and it is the write the reaper's grace must see.
        vectors.upsertChunks(TENANT, COL_UPSERT_HAVE_VECTOR, List.of(chash), List.of("stable text"),
            List.of(Map.of("v", "2")));

        assertRefreshed(aged, stamps(COL_UPSERT_HAVE_VECTOR, chash));
    }

    @Test
    void upsertReferenceOnlyChunk_onConflict_refreshesLastWrittenAt() throws Exception {
        String chash = ch("refonly");
        float[] vec = new float[384];
        vec[3] = 1.0f;
        vectors.upsertReferenceOnlyChunk(TENANT, COL_REF_ONLY, chash, vec, Map.of("v", "1"));
        Stamps aged = age(COL_REF_ONLY, chash);

        vectors.upsertReferenceOnlyChunk(TENANT, COL_REF_ONLY, chash, vec, Map.of("v", "2"));

        assertRefreshed(aged, stamps(COL_REF_ONLY, chash));
    }

    @Test
    void combinedWrite_identicalTextMetadataRefreshBranch_refreshesLastWrittenAt() throws Exception {
        String chash = ch("cw-identical");
        registerDoc("lwa.1a", COL_CW_IDENTICAL);
        registerDoc("lwa.1b", COL_CW_IDENTICAL);
        combined.writeManyCombined(TENANT, COL_CW_IDENTICAL,
            List.of(chunk(chash, "cw stable text", Map.of("section_type", ""))),
            List.of(doc("lwa.1a", List.of(row(0, chash)))), null, false, false);
        Stamps aged = age(COL_CW_IDENTICAL, chash);

        // The nexus-4jj40 branch: identical stored text, different metadata, no
        // embed. The chash never reaches the INSERT; batchUpdateMetadata is the
        // only statement that touches the row.
        var r2 = combined.writeManyCombined(TENANT, COL_CW_IDENTICAL,
            List.of(chunk(chash, "cw stable text", Map.of("section_type", "imports"))),
            List.of(doc("lwa.1b", List.of(row(0, chash)))), null, false, false);
        assertThat(r2.response().get("embed_embedded")).as("precondition: took the no-embed branch")
            .isEqualTo(0);

        assertRefreshed(aged, stamps(COL_CW_IDENTICAL, chash));
    }

    @Test
    void combinedWrite_chunkUpsertOnConflict_refreshesLastWrittenAt() throws Exception {
        String chash = ch("cw-divergent");
        registerDoc("lwa.2a", COL_CW_DIVERGENT);
        registerDoc("lwa.2b", COL_CW_DIVERGENT);
        combined.writeManyCombined(TENANT, COL_CW_DIVERGENT,
            List.of(chunk(chash, "cw first text", Map.of())),
            List.of(doc("lwa.2a", List.of(row(0, chash)))), null, false, false);
        Stamps aged = age(COL_CW_DIVERGENT, chash);

        // Different text under the same chash: needs an embed, so the per-doc
        // transaction runs upsertManifestChunkVectors' INSERT ... ON CONFLICT.
        var r2 = combined.writeManyCombined(TENANT, COL_CW_DIVERGENT,
            List.of(chunk(chash, "cw second text", Map.of())),
            List.of(doc("lwa.2b", List.of(row(0, chash)))), null, false, false);
        assertThat(r2.response().get("embed_embedded")).as("precondition: took the embed branch")
            .isEqualTo(1);

        assertRefreshed(aged, stamps(COL_CW_DIVERGENT, chash));
    }

    // ── deliberately untouched: maintenance and stamping ─────────────────────

    @Test
    void updateMetadata_frecencyAndEnrichmentStamping_doesNotMoveLastWrittenAt() throws Exception {
        String chash = ch("maint");
        vectors.upsertChunks(TENANT, COL_MAINTENANCE, List.of(chash), List.of("maintained text"),
            List.of(Map.of("frecency_score", "0.1")));
        Stamps aged = age(COL_MAINTENANCE, chash);

        int affected = vectors.updateMetadata(TENANT, COL_MAINTENANCE, List.of(chash),
            List.of(Map.of("frecency_score", "0.9")));
        assertThat(affected).as("precondition: the stamp really hit the row").isEqualTo(1);

        Stamps after = stamps(COL_MAINTENANCE, chash);
        assertThat(after.lastWrittenAt())
            .as("a metadata stamp is not a client re-write: it must not extend a dead chunk's grace")
            .isEqualTo(aged.lastWrittenAt());
        assertThat(after.createdAt()).isEqualTo(aged.createdAt());
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static void assertRefreshed(Stamps aged, Stamps after) {
        assertThat(after.recent())
            .as("last_written_at must jump from a day ago to now(): the re-write refreshed it")
            .isTrue();
        assertThat(after.lastWrittenAt()).isAfter(aged.lastWrittenAt());
        assertThat(after.createdAt())
            .as("created_at is write-once: a re-write must not touch it")
            .isEqualTo(aged.createdAt());
    }

    private static String ch(String seed) {
        return Chash.ofText("lwa-" + seed).toHex();
    }

    /** Pushes created_at two days and last_written_at one day into the past, returns what it stored. */
    private Stamps age(String collection, String chash) throws SQLException {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(384);
        Field<OffsetDateTime> createdAt = ch.table().field("created_at", OffsetDateTime.class);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            // The database's own clock, so a skewed container clock cannot fake a pass.
            OffsetDateTime dbNow = ctx.select(DimTables.lastWrittenNow()).fetchOne(0, OffsetDateTime.class);
            int hit = ctx.update(ch.table())
                .set(createdAt, dbNow.minusDays(2))
                .set(ch.lastWrittenAt(), dbNow.minusDays(1))
                .where(ch.collection().eq(collection).and(ch.chash().eq(chash)))
                .execute();
            assertThat(hit).as("aging %s/%s hit one row", collection, chash).isEqualTo(1);
        }
        Stamps s = stamps(collection, chash);
        assertThat(s.recent()).as("precondition: the aged row is no longer recent").isFalse();
        return s;
    }

    private Stamps stamps(String collection, String chash) throws SQLException {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(384);
        Field<OffsetDateTime> createdAt = ch.table().field("created_at", OffsetDateTime.class);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var r = ctx.select(createdAt, ch.lastWrittenAt(), DimTables.lastWrittenNow())
                .from(ch.table())
                .where(ch.collection().eq(collection).and(ch.chash().eq(chash)))
                .fetchOne();
            assertThat(r).as("row %s/%s must exist", collection, chash).isNotNull();
            return new Stamps(r.value1(), r.value2(), r.value3());
        }
    }

    private void registerDoc(String tumbler, String collection) {
        catalog.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "lwa-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
    }

    private static Map<String, Object> chunk(String chash, String text, Map<String, Object> metadata) {
        return Map.of("chash", chash, "text", text, "metadata", metadata);
    }

    private static Map<String, Object> row(int position, String chash) {
        return Map.of("position", position, "chash", chash, "chunk_index", position);
    }

    private static Map<String, Object> doc(String docId, List<Map<String, Object>> rows) {
        return Map.of("doc_id", docId, "rows", rows);
    }
}

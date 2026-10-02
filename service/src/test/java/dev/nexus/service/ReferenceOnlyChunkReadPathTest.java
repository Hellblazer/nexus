// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;

/**
 * Read path of REFERENCE-ONLY rows (RDR-169 G4) against a REAL, fully-migrated schema.
 * The engine has no writer for such a row (RDR-223 Phase 3, nexus-z0o2p.36), so every row
 * here is built by {@link PgContainerHelper#insertReferenceOnlyChunk}; what is pinned is how
 * the engine READS and PROMOTES one.
 *
 * <ul>
 *   <li>A reference-only row round-trips through {@link PgVectorRepository#search} with
 *       {@code content=null} and {@code retention="reference-only"}, and is EXCLUDED from
 *       {@link PgVectorRepository#hybridSearch} (chunk_tsv is NULL for a NULL chunk_text, so
 *       the hybrid_search_&lt;dim&gt; predicate is false on both arms, vectors-007).</li>
 *   <li>A reference-only row re-seeded with new vector and metadata keeps {@code chunk_text}
 *       NULL.</li>
 *   <li>A reference-only row re-submitted through the ORDINARY {@link
 *       PgVectorRepository#upsertChunks} with real text ends up full, {@code retention='full'}.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReferenceOnlyChunkReadPathTest {

    static final String TENANT     = "t-refonly-test";
    static final String COL        = "knowledge__refonly-owner__voyage-context-3__v1";
    // Full 64-hex chash for the search round-trip test
    static final String REFONLY_CHASH = dev.nexus.service.db.Chash.ofText("rrefonly").toHex();
    // Full 64-hex chash for the reference-only re-seed test
    static final String REWRITE_CHASH = dev.nexus.service.db.Chash.ofText("rrewrite").toHex();

    static final String REFONLY_QUERY = "docuverse reference only search probe";
    static final float[] REFONLY_VEC  = FakeEmbedder.unitVector(1024, 0.6f, 0.8f);

    PostgreSQLContainer<?> pg;
    HikariDataSource       ds;
    PgVectorRepository     repo;

    @BeforeAll
    void startDb() throws Exception {
        pg = PgContainerHelper.start();

        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        ds = new HikariDataSource(cfg);

        try (Connection conn = ds.getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase("db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                lb.update(new Contexts());
            }
        }

        // Register TENANT so TenantScope can resolve RLS. Literal (fake) hash, not a
        // real token's sha256 -- typed jOOQ DSL directly, not
        // PgContainerHelper.seedServiceToken (which always hashes its token argument).
        try (Connection conn = ds.getConnection()) {
            DSL.using(conn, SQLDialect.POSTGRES).insertInto(SERVICE_TOKENS)
                .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
                .values("fakehash-refonly", TENANT, "test-refonly")
                .onConflictDoNothing()
                .execute();
        }

        TenantScope scope = new TenantScope(ds);
        FakeEmbedder embedder = new FakeEmbedder(1024);
        embedder.register(REFONLY_QUERY, 0.6f, 0.8f);
        repo = new PgVectorRepository(scope, embedder, embedder);

        // RDR-204 Phase 1 (bead nexus-ft04v.7): PgVectorRepository's stub-insert is
        // retired — the upsertChunks promote test now requires COL to already be
        // registered (CollectionRegistry.requireRegistered), or it fails loud.
        // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection,
        // which derives the constraint-satisfying attributes hygiene-002-1 now
        // requires (the bare two-column insert this used to run 23502s on
        // lifecycle_state NOT NULL).
        try (Connection conn = ds.getConnection()) {
            PgContainerHelper.insertCollection(DSL.using(conn, SQLDialect.POSTGRES), TENANT, COL);
        }
    }

    @AfterAll
    void stopDb() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** Direct typed-jOOQ read of {@code chunk_text} for one chash — bypasses the repo. */
    private Optional<String> chunkTextOf(String chash) throws Exception {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(1024);
            return Optional.ofNullable(ctx.select(ch.chunkText()).from(ch.table())
                .where(ch.tenantId().eq(TENANT).and(ch.chash().eq(chash)))
                .fetchOne(ch.chunkText()));
        }
    }

    /** Seed (or re-seed) one reference-only row through the test fixture, as the superuser. */
    private void seedReferenceOnly(String chash, float[] vec, Map<String, Object> metadata) throws Exception {
        try (Connection conn = ds.getConnection()) {
            PgContainerHelper.insertReferenceOnlyChunk(
                DSL.using(conn, SQLDialect.POSTGRES), TENANT, COL, chash, vec, metadata);
        }
    }

    /** Direct typed-jOOQ read of metadata key {@code v} for one chash — bypasses the repo. */
    private String metadataVOf(String chash) throws Exception {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(1024);
            var meta = ctx.select(ch.metadata()).from(ch.table())
                .where(ch.tenantId().eq(TENANT).and(ch.chash().eq(chash)))
                .fetchOne(ch.metadata());
            try {
                return new com.fasterxml.jackson.databind.ObjectMapper()
                    .readTree(meta.data()).path("v").asText();
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                throw new IllegalStateException(e);
            }
        }
    }

    /** Direct typed-jOOQ read of {@code retention} for one chash — bypasses the repo. */
    private String retentionOf(String chash) throws Exception {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(1024);
            return ctx.select(ch.retention()).from(ch.table())
                .where(ch.tenantId().eq(TENANT).and(ch.chash().eq(chash)))
                .fetchOne(ch.retention());
        }
    }

    // -------------------------------------------------------------------------
    // Live write: a brand-new reference-only chash succeeds and round-trips
    // -------------------------------------------------------------------------

    /**
     * Gives the reference-only row a live owner in {@link #COL}, idempotently. Since
     * RDR-192 Step 5 (nexus-wbfpw.10) search returns only chunks with a live
     * own-collection owner, so an unowned row would be hidden for that reason alone and
     * the hybrid-exclusion test below would pass without exercising the NULL-text gate.
     */
    private void ownReferenceOnlyChunk() {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_DOCUMENTS)
               .columns(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                        CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT, "refonly-owner", "reference-only owner", COL)
               .onConflictDoNothing()
               .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
               .columns(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(TENANT, "refonly-owner", 0,
                       dev.nexus.service.db.Chash.fromHex(REFONLY_CHASH).toBytes(), COL)
               .onConflictDoNothing()
               .execute();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * {@link PgVectorRepository#search} (pure vector ranking, vectors-009
     * {@code plain_search_<dim>}, no FTS gate) must return the row with {@code content=null}
     * AND {@code retention="reference-only"} (RDR-169 Phase B fix round 1, Gap 2,
     * vectors-015-retention-search-return.xml) — the additive field a reference-aware
     * consumer reads to distinguish this row from an ordinary full-content hit.
     */
    @Test
    void referenceOnlyRow_searchReturnsNullContentAndRetention() throws Exception {
        seedReferenceOnly(REFONLY_CHASH, REFONLY_VEC, Map.of("k", "v"));
        ownReferenceOnlyChunk();

        List<Map<String, Object>> rows = repo.search(TENANT, REFONLY_QUERY, List.of(COL), 10, null);
        Map<String, Object> row = rows.stream()
            .filter(r -> REFONLY_CHASH.equals(r.get("id")))
            .findFirst()
            .orElse(null);
        assertThat(row)
            .as("plain vector search must return the reference-only row (it has a real "
                + "embedding, RDR-169 Technical Design: 'a reference-only chunk is still a "
                + "vector')")
            .isNotNull();
        assertThat(row.get("content"))
            .as("a reference-only hit's content must be null on the wire")
            .isNull();
        assertThat(row.get("retention"))
            .as("a reference-only hit's retention must read back 'reference-only'")
            .isEqualTo("reference-only");
    }

    /**
     * The SAME row {@link #referenceOnlyRow_searchReturnsNullContentAndRetention}
     * reads back must be EXCLUDED from {@link PgVectorRepository#hybridSearch} — {@code
     * chunk_tsv} is NULL (generated from a NULL {@code chunk_text}), so
     * {@code hybrid_search_<dim>}'s {@code chunk_tsv @@ plainto_tsquery(...) OR
     * p_query_text <% chunk_text} predicate (vectors-007) evaluates false on both arms and
     * the row never enters the candidate set — proven live, not just at the SQL-shape
     * level (CA-2).
     */
    @Test
    void referenceOnlyRow_isExcludedFromHybridSearch() throws Exception {
        // Ensure the row exists regardless of JUnit method ordering within the class
        // (PER_CLASS lifecycle does not guarantee declaration order across test runners).
        seedReferenceOnly(REFONLY_CHASH, REFONLY_VEC, Map.of("k", "v"));
        ownReferenceOnlyChunk();

        List<Map<String, Object>> hybridRows =
            repo.hybridSearch(TENANT, REFONLY_QUERY, List.of(COL), 10, null);
        assertThat(hybridRows)
            .as("hybrid_search_<dim> gates on chunk_tsv / trigram similarity against "
                + "chunk_text -- both NULL for a reference-only row, so it must never appear")
            .noneMatch(r -> REFONLY_CHASH.equals(r.get("id")));
    }

    // -------------------------------------------------------------------------
    // reference-only → reference-only rewrite (embedding/metadata refresh)
    // -------------------------------------------------------------------------

    /**
     * A second seed of the SAME chash (no intervening full-content write) refreshes
     * embedding/metadata; {@code chunk_text} stays NULL and {@code retention} stays
     * {@code reference-only} (RDR-169 §Re-index: "reference-only → reference-only rewrites
     * ... are fine"). This pins the fixture's conflict contract and the schema's biconditional
     * CHECK, which a rewrite must satisfy; the engine itself has no writer to rewrite with.
     */
    @Test
    void referenceOnlyToReferenceOnly_rewriteSucceeds_chunkTextStaysNull() throws Exception {
        seedReferenceOnly(REWRITE_CHASH, FakeEmbedder.unitVector(1024, 1.0f, 0.0f), Map.of("v", "1"));
        assertThat(chunkTextOf(REWRITE_CHASH)).as("first write: no content").isEmpty();
        assertThat(metadataVOf(REWRITE_CHASH)).isEqualTo("1");

        // Rewrite: different embedding + metadata, same chash.
        seedReferenceOnly(REWRITE_CHASH, FakeEmbedder.unitVector(1024, 0.0f, 1.0f), Map.of("v", "2"));
        assertThat(metadataVOf(REWRITE_CHASH)).as("the rewrite landed").isEqualTo("2");
        assertThat(chunkTextOf(REWRITE_CHASH))
            .as("rewrite must not have materialized any content")
            .isEmpty();
        assertThat(retentionOf(REWRITE_CHASH)).isEqualTo("reference-only");
    }

    // -------------------------------------------------------------------------
    // reference-only → full promotion via the ORDINARY content-write path
    // -------------------------------------------------------------------------

    /**
     * A chash first written reference-only, then re-submitted through the ORDINARY
     * {@link PgVectorRepository#upsertChunks} path with real text, must end up with
     * {@code chunk_text} present AND {@code retention='full'} — closing the "reference-only
     * row silently promoted to full content while retention stays stale" gap
     * (T2 review-nexus-zw2em-rdr169-phase-b-2026-09-11 Important finding): the ordinary
     * path's {@code ON CONFLICT DO UPDATE} now sets {@code retention='full'} explicitly
     * (PgVectorRepository#upsertChunksInternal), not just {@code chunk_text}.
     */
    @Test
    void referenceOnlyPromotedToFull_viaOrdinaryUpsert_retentionReadsFull() throws Exception {
        String promoteChash = dev.nexus.service.db.Chash.ofText("rpromote").toHex();
        seedReferenceOnly(promoteChash, FakeEmbedder.unitVector(1024, 0.6f, 0.8f), Map.of());
        assertThat(chunkTextOf(promoteChash)).as("reference-only: no content yet").isEmpty();
        assertThat(retentionOf(promoteChash)).isEqualTo("reference-only");

        // Promote: the SAME chash, now with real content, through the ordinary path.
        repo.upsertChunks(TENANT, COL,
            List.of(promoteChash), List.of("promoted to full content"), List.of(Map.of()));

        assertThat(chunkTextOf(promoteChash))
            .as("the promoted row must carry real content")
            .contains("promoted to full content");
        assertThat(retentionOf(promoteChash))
            .as("retention must flip to 'full' -- it must not stay stale at "
                + "'reference-only' once real content has genuinely arrived")
            .isEqualTo("full");
    }
}

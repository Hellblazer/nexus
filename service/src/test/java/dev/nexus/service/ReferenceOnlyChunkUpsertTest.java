// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;

/**
 * RDR-169 Phase B (bead nexus-zw2em) live integration tests for
 * {@link PgVectorRepository#upsertReferenceOnlyChunk} (RDR-169 G4, nexus-xvb6b) against a
 * REAL, fully-migrated schema — {@code vectors-014-retention.xml} has landed, {@link
 * PgVectorRepository#REFERENCE_ONLY_WRITES_ENABLED} is {@code true}, and every path below
 * reaches the real INSERT.
 *
 * <ul>
 *   <li>Null / empty embedding fails loud (pre-SQL).</li>
 *   <li>Dim mismatch fails loud (pre-SQL).</li>
 *   <li>full → reference-only guard: rejected, and the pre-existing full content is
 *       UNTOUCHED afterward (non-vacuous: reads the row back).</li>
 *   <li>A brand-new reference-only chash succeeds, round-trips with {@code content=null}
 *       via {@link PgVectorRepository#search}, and is EXCLUDED from {@link
 *       PgVectorRepository#hybridSearch} (chunk_tsv is NULL for a NULL chunk_text — the
 *       hybrid_search_&lt;dim&gt; function's {@code chunk_tsv @@ ... OR ... &lt;% chunk_text}
 *       predicate is false on both sides, vectors-007).</li>
 *   <li>reference-only → reference-only rewrite (embedding/metadata refresh) succeeds;
 *       {@code chunk_text} stays NULL.</li>
 * </ul>
 *
 * <p>SQL shape (without live execution) is tested separately in
 * {@code dev.nexus.service.vectors.ReferenceOnlySqlShapeTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReferenceOnlyChunkUpsertTest {

    static final String TENANT     = "t-refonly-test";
    static final String COL        = "knowledge__refonly-owner__voyage-context-3__v1";
    static final String FULL_TEXT  = "full content text for guard test";
    // Full 64-hex chash with full-content chunk (used for full→ref guard test)
    static final String FULL_CHASH = dev.nexus.service.db.Chash.ofText("rfull").toHex();
    // Full 64-hex chash for the dim/null pre-SQL validation tests (never written)
    static final String NEW_CHASH  = dev.nexus.service.db.Chash.ofText("rnew").toHex();
    // Full 64-hex chash for the successful-write / search round-trip test
    static final String REFONLY_CHASH = dev.nexus.service.db.Chash.ofText("rrefonly").toHex();
    // Full 64-hex chash for the reference-only -> reference-only rewrite test
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
        // retired — the upsertChunks seed call below now requires COL to already be
        // registered (CollectionRegistry.requireRegistered), or it fails loud.
        // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection,
        // which derives the constraint-satisfying attributes hygiene-002-1 now
        // requires (the bare two-column insert this used to run 23502s on
        // lifecycle_state NOT NULL).
        try (Connection conn = ds.getConnection()) {
            PgContainerHelper.insertCollection(DSL.using(conn, SQLDialect.POSTGRES), TENANT, COL);
        }

        // Seed a full-content chunk for the full→reference-only guard test.
        repo.upsertChunks(TENANT, COL,
            List.of(FULL_CHASH),
            List.of(FULL_TEXT),
            List.of(Map.of("source", "test")));
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
    // Null / empty embedding — pre-SQL, no DB needed
    // -------------------------------------------------------------------------

    /** Null embedding must fail loud with {@link IllegalArgumentException}. */
    @Test
    void nullEmbedding_failsLoud() {
        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, NEW_CHASH, null, Map.of()))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("non-null");
    }

    /** Empty (zero-length) embedding must fail loud with {@link IllegalArgumentException}. */
    @Test
    void emptyEmbedding_failsLoud() {
        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, NEW_CHASH, new float[0], Map.of()))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("non-empty");
    }

    // -------------------------------------------------------------------------
    // Dim mismatch — pre-SQL, no DB needed
    // -------------------------------------------------------------------------

    /**
     * A vector whose dimension disagrees with the collection's model segment must fail
     * loud with {@link IllegalArgumentException} before any SQL is issued.
     */
    @Test
    void dimMismatch_failsLoud() {
        float[] wrongDimVec = new float[768]; // COL dispatches to embedding_1024

        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, NEW_CHASH, wrongDimVec, Map.of()))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("768")
            .hasMessageContaining("1024");
    }

    // -------------------------------------------------------------------------
    // full → reference-only guard — real data, non-vacuous
    // -------------------------------------------------------------------------

    /**
     * A chash that already has {@code chunk_text IS NOT NULL} must be rejected with
     * {@link IllegalStateException} citing "full→reference-only transition is prohibited",
     * AND the pre-existing full content must be genuinely UNTOUCHED afterward — the guard
     * fires BEFORE any INSERT reaches the database, so this is not just "the call throws",
     * it is "the stored row never changed" (RDR-169 §Re-index PROHIBITS a silent NULL).
     */
    @Test
    void fullToReferenceOnly_isRejected_andContentIsUntouched() throws Exception {
        float[] vec = FakeEmbedder.unitVector(1024, 1.0f, 0.0f);

        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, FULL_CHASH, vec, Map.of()))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("full content")
            .hasMessageContaining(FULL_CHASH)
            .hasMessageContaining("full→reference-only transition is prohibited");

        assertThat(chunkTextOf(FULL_CHASH))
            .as("the rejected reference-only attempt must not have touched the stored content")
            .contains(FULL_TEXT);
    }

    // -------------------------------------------------------------------------
    // Live write: a brand-new reference-only chash succeeds and round-trips
    // -------------------------------------------------------------------------

    /**
     * When no existing row occupies the chash, the write gate is open
     * ({@link PgVectorRepository#REFERENCE_ONLY_WRITES_ENABLED} == true, Phase B) and the
     * INSERT succeeds. {@link PgVectorRepository#search} (pure vector ranking, vectors-009
     * {@code plain_search_<dim>}, no FTS gate) must return the row with {@code content=null}
     * AND {@code retention="reference-only"} (RDR-169 Phase B fix round 1, Gap 2,
     * vectors-015-retention-search-return.xml) — the additive field a reference-aware
     * consumer reads to distinguish this row from an ordinary full-content hit.
     */
    @Test
    void referenceOnlyOnNewChash_succeeds_andSearchReturnsNullContentAndRetention() {
        repo.upsertReferenceOnlyChunk(TENANT, COL, REFONLY_CHASH, REFONLY_VEC, Map.of("k", "v"));

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
     * The SAME row {@link #referenceOnlyOnNewChash_succeeds_andSearchReturnsNullContent}
     * wrote must be EXCLUDED from {@link PgVectorRepository#hybridSearch} — {@code
     * chunk_tsv} is NULL (generated from a NULL {@code chunk_text}), so
     * {@code hybrid_search_<dim>}'s {@code chunk_tsv @@ plainto_tsquery(...) OR
     * p_query_text <% chunk_text} predicate (vectors-007) evaluates false on both arms and
     * the row never enters the candidate set — proven live, not just at the SQL-shape
     * level (CA-2).
     */
    @Test
    void referenceOnlyRow_isExcludedFromHybridSearch() {
        // Ensure the row exists regardless of JUnit method ordering within the class
        // (PER_CLASS lifecycle does not guarantee declaration order across test runners).
        repo.upsertReferenceOnlyChunk(TENANT, COL, REFONLY_CHASH, REFONLY_VEC, Map.of("k", "v"));

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
     * A second {@code upsertReferenceOnlyChunk} call against the SAME chash (no intervening
     * full-content write) must succeed and refresh embedding/metadata; {@code chunk_text}
     * stays NULL throughout (RDR-169 §Re-index: "reference-only → reference-only rewrites
     * ... are fine").
     */
    @Test
    void referenceOnlyToReferenceOnly_rewriteSucceeds_chunkTextStaysNull() throws Exception {
        repo.upsertReferenceOnlyChunk(TENANT, COL, REWRITE_CHASH,
            FakeEmbedder.unitVector(1024, 1.0f, 0.0f), Map.of("v", "1"));
        assertThat(chunkTextOf(REWRITE_CHASH)).as("first write: no content").isEmpty();

        // Rewrite: different embedding + metadata, same chash.
        repo.upsertReferenceOnlyChunk(TENANT, COL, REWRITE_CHASH,
            FakeEmbedder.unitVector(1024, 0.0f, 1.0f), Map.of("v", "2"));
        assertThat(chunkTextOf(REWRITE_CHASH))
            .as("rewrite must not have materialized any content")
            .isEmpty();
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
        repo.upsertReferenceOnlyChunk(TENANT, COL, promoteChash,
            FakeEmbedder.unitVector(1024, 0.6f, 0.8f), Map.of());
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

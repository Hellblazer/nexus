// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

/**
 * Phase-A guard + integration tests for {@link PgVectorRepository#upsertReferenceOnlyChunk}
 * (RDR-169 G4, nexus-xvb6b).
 *
 * <h3>Phase-A scope — what is tested here</h3>
 * The {@code retention} column does not exist until Phase B (nexus-dtnpu).  Tests cover
 * only the pre-SQL and pre-gate validation paths that are safe against the current schema:
 * <ul>
 *   <li>Null / empty embedding fails loud (pre-SQL).</li>
 *   <li>Dim mismatch fails loud (pre-SQL).</li>
 *   <li>full → reference-only guard: guard SELECT fires, finds non-NULL
 *       {@code chunk_text}, throws {@link IllegalStateException} BEFORE the gate.</li>
 *   <li>Phase-A write gate: guard passes on a new chash, gate fires with
 *       "disabled until RDR-169 Phase B" message — proving no Phase-A code
 *       path reaches the retention-binding INSERT.</li>
 * </ul>
 *
 * <p>SQL shape is tested separately in
 * {@code dev.nexus.service.vectors.ReferenceOnlySqlShapeTest} (package-private access
 * to {@code referenceOnlyInsertSql}).  On-real-data rejection, FTS-NULL-exclusion, and
 * the live /v1 route are deferred to nexus-dtnpu (Phase B).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReferenceOnlyChunkUpsertTest {

    static final String TENANT     = "t-refonly-test";
    static final String COL        = "knowledge__refonly-owner__voyage-context-3__v1";
    // 32-char chash with full-content chunk (used for full→ref guard test)
    static final String FULL_CHASH = "rfull000000000000000000000000001";
    // 32-char chash for the gate-short-circuit test (no existing row)
    static final String NEW_CHASH  = "rnew0000000000000000000000000001";

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

        // Register TENANT so TenantScope can resolve RLS.
        try (Connection conn = ds.getConnection()) {
            conn.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                + " VALUES ('fakehash-refonly', '" + TENANT + "', 'test-refonly')"
                + " ON CONFLICT (token_hash) DO NOTHING");
        }

        TenantScope scope = new TenantScope(ds);
        FakeEmbedder embedder = new FakeEmbedder(1024);
        repo = new PgVectorRepository(scope, embedder, embedder);

        // Seed a full-content chunk for the full→reference-only guard test.
        // upsertChunks is safe today (no retention column on the existing INSERT).
        repo.upsertChunks(TENANT, COL,
            List.of(FULL_CHASH),
            List.of("full content text for guard test"),
            List.of(Map.of("source", "test")));
    }

    @AfterAll
    void stopDb() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
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
        float[] wrongDimVec = new float[768]; // COL dispatches to chunks_1024

        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, NEW_CHASH, wrongDimVec, Map.of()))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("768")
            .hasMessageContaining("1024");
    }

    // -------------------------------------------------------------------------
    // full → reference-only guard (guard SELECT fires before the Phase-A gate)
    // -------------------------------------------------------------------------

    /**
     * A chash that already has {@code chunk_text IS NOT NULL} must be rejected with
     * {@link IllegalStateException} citing "full→reference-only transition is prohibited".
     * The guard SELECT reads only {@code chunk_text} — safe against Phase-A schema.
     * The gate never fires because the guard throws first.
     */
    @Test
    void fullToReferenceOnly_isRejected_beforeGate() {
        float[] vec = FakeEmbedder.unitVector(1024, 1.0f, 0.0f);

        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, FULL_CHASH, vec, Map.of()))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("full content")
            .hasMessageContaining(FULL_CHASH)
            .hasMessageContaining("full→reference-only transition is prohibited");
    }

    // -------------------------------------------------------------------------
    // Phase-A write gate: guard passes, gate fires with Phase-B message
    // -------------------------------------------------------------------------

    /**
     * When no existing row occupies the chash (guard passes), the Phase-A write gate
     * ({@link PgVectorRepository#REFERENCE_ONLY_WRITES_ENABLED} == false) must throw
     * {@link IllegalStateException} with "disabled until RDR-169 Phase B".
     *
     * <p>This proves the code path ordering: dim error → full→ref ISE → gate ISE →
     * (Phase B) INSERT.  No Phase-A execution reaches the retention-binding INSERT.
     */
    @Test
    void referenceOnlyOnNewChash_gateFiresWithPhaseBMessage() {
        float[] vec = FakeEmbedder.unitVector(1024, 0.6f, 0.8f);

        assertThatThrownBy(() ->
            repo.upsertReferenceOnlyChunk(TENANT, COL, NEW_CHASH, vec, Map.of("k", "v")))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("disabled until RDR-169 Phase B");
    }
}

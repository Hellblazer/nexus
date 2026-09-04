// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Substantive critique 2026-08-10 finding 3 (T2
 * nexus/chroma-residue-C1-T0.1-critique-2026-08-10), on top of 14bf9a0c
 * (T2 nexus/chroma-residue-plan-2026-08-10 §T0.1).
 *
 * <p>{@code Indexer._prune_deleted_files} (a DELETE path) now relies on
 * {@link PgVectorRepository#getAllMetadata} raising (never silently
 * truncating) once the result set exceeds {@link
 * PgVectorRepository#GET_ALL_METADATA_MAX_ROWS}. That behaviour was verified
 * by READING the Java source (14bf9a0c's commit message says so explicitly)
 * but had NO server-side test exercising the actual cap crossing —
 * {@code PgVectorRepositoryContractTest}'s {@code getAllMetadata_*} tests
 * cover small-N correctness/tenant-scoping/empty only, never the boundary.
 *
 * <p>The production cap is 200,000 rows — too large to cross cheaply by
 * inserting real rows into a Testcontainers Postgres inside a unit test.
 * Rather than brute-forcing 200,001 inserts, this suite uses the
 * package-private 4-arg {@code PgVectorRepository} constructor overload
 * (test-only; added alongside this test, same package) that takes the cap
 * explicitly, so the cap-crossing property can be exercised for real,
 * against a handful of rows, instead of skipped or faked. The production
 * constructors (2-arg / 3-arg, public) always fix the cap at {@code
 * GET_ALL_METADATA_MAX_ROWS} — see {@code productionConstructor_*} below,
 * which pins that behaviourally rather than by restating the constant
 * (substantive critique 2026-08-10 findings 1 + 2, T2
 * nexus/chroma-residue-C1-T0.1-critique-2026-08-10).
 *
 * <p>The property under test: at {@code cap + 1} rows the call RAISES
 * ({@link IllegalStateException}, mapped to HTTP 422 by {@code
 * VectorHandler}) — it does NOT return a silently short result. Companion
 * cases pin the two boundary neighbours ({@code cap} rows succeeds in full,
 * {@code cap - 1} rows succeeds in full) so the boundary is exercised on
 * both sides, not just crossed.
 *
 * <p>Real Postgres round trip throughout (Testcontainers pgvector/pgvector:pg17,
 * plain LOGIN NOSUPERUSER role, PER_CLASS) — house rule: prefer a real
 * substrate over a mock. Setup mirrors {@code PgVectorMetadataBatchParityTest}.
 * Uses embedding_384 (smallest vector width) on the unified nexus.chunks
 * table (RDR-191 Phase 4) and a
 * zero-vector fake embedder so inserting rows costs no real embedding work.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryGetAllMetadataCapBoundaryTest {

    private static final String SVC_ROLE = "svc_gam_cap_test";
    private static final String SVC_PASS = "svc_gam_cap_test_pass";
    private static final String TENANT   = "gam-cap-tenant";

    /** Small on purpose — the point is to cross a cap for real, not to prove PG can hold 200k rows. */
    private static final int TEST_CAP = 5;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private PgVectorRepository repo;

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
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder = new ZeroEmbedder(384);
        repo = new PgVectorRepository(tenantScope, embedder, embedder, TEST_CAP);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    private static List<String> chashesFor(String label, int n) {
        List<String> out = new ArrayList<>(n);
        for (int i = 0; i < n; i++) out.add(Chash.ofText(label + "-" + i).toHex());
        return out;
    }

    private void seed(String collection, int n) {
        List<String> ids = chashesFor(collection, n);
        List<String> texts = new ArrayList<>(n);
        List<Map<String, Object>> metas = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            texts.add("gam-cap text " + i);
            metas.add(Map.of("i", String.valueOf(i)));
        }
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
    }

    /**
     * The load-bearing case: {@code TEST_CAP + 1} rows must RAISE, not
     * return a truncated {@code TEST_CAP}-length result. This is the exact
     * property {@code _prune_deleted_files}'s completeness argument depends
     * on — a caller that silently got {@code TEST_CAP} rows back here would
     * (in production, at the real 200,000 cap) treat the missing rows as
     * "not alive" and delete their vectors, a real data-loss bug the raise
     * exists specifically to prevent.
     */
    @Test
    void overCap_raisesInsteadOfTruncating() {
        String col = "knowledge__gamcapover__minilm-l6-v2-384__v1";
        seed(col, TEST_CAP + 1);

        assertThatThrownBy(() -> repo.getAllMetadata(TENANT, col, null))
            .as("cap+1 rows must fail loud, never silently truncate to cap rows")
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("more than")
            .hasMessageContaining(String.valueOf(TEST_CAP));
    }

    /** Exactly at the cap: every row is returned, no exception. */
    @Test
    void atCap_returnsEveryRow_doesNotRaise() {
        String col = "knowledge__gamcapat__minilm-l6-v2-384__v1";
        seed(col, TEST_CAP);

        var result = repo.getAllMetadata(TENANT, col, null);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) result.get("ids");
        assertThat(ids).hasSize(TEST_CAP);
    }

    /** One below the cap: also a full, unexceptional read — brackets the boundary from below. */
    @Test
    void belowCap_returnsEveryRow_doesNotRaise() {
        String col = "knowledge__gamcapbelow__minilm-l6-v2-384__v1";
        seed(col, TEST_CAP - 1);

        var result = repo.getAllMetadata(TENANT, col, null);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) result.get("ids");
        assertThat(ids).hasSize(TEST_CAP - 1);
    }

    /**
     * Behavioural counterpart to the cap-crossing tests above (substantive
     * critique 2026-08-10 finding 2): an instance built via the PRODUCTION
     * constructor (the public 3-arg overload, no cap argument) must NOT be
     * using {@link #TEST_CAP} — i.e. the small-cap override the other tests
     * rely on is genuinely absent by default, not merely a restated literal.
     *
     * <p>Exercised for real: seed {@code TEST_CAP + 1} rows — a boundary
     * that RAISES on the {@code repo} field (constructed with the small
     * test-cap override) — into a repository built the production way and
     * assert it returns all rows without raising. A production constructor
     * that accidentally wired the small test cap in (or any cap {@code <=
     * TEST_CAP}) would make this test fail; proving the real 200,000-row
     * cap directly would require 200,001 inserts, which is impractical for
     * a unit test, so this pins the property one level removed: "not the
     * small cap" rather than "exactly 200,000" — a genuine behavioural
     * check replacing the original test, which only compared {@code
     * GET_ALL_METADATA_MAX_ROWS} to the literal {@code 200_000} and could
     * never fail.
     */
    @Test
    void productionConstructor_hasNoCapOverride_crossesTestCapBoundaryWithoutRaising() {
        var prodEmbedder = new ZeroEmbedder(384);
        var prodRepo = new PgVectorRepository(tenantScope, prodEmbedder, prodEmbedder);
        String col = "knowledge__gamcapprod__minilm-l6-v2-384__v1";
        List<String> ids = chashesFor(col, TEST_CAP + 1);
        List<String> texts = new ArrayList<>(ids.size());
        List<Map<String, Object>> metas = new ArrayList<>(ids.size());
        for (int i = 0; i < ids.size(); i++) {
            texts.add("gam-cap-prod text " + i);
            metas.add(Map.of("i", String.valueOf(i)));
        }
        prodRepo.upsertChunks(TENANT, col, ids, texts, metas);

        var result = prodRepo.getAllMetadata(TENANT, col, null);

        @SuppressWarnings("unchecked")
        List<String> outIds = (List<String>) result.get("ids");
        assertThat(outIds)
            .as("production constructor must not inherit the small test-cap override")
            .hasSize(TEST_CAP + 1);
    }

    private static final class ZeroEmbedder implements Embedder {
        private final int dim;

        ZeroEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) out.add(new float[dim]);
            return out;
        }
    }
}

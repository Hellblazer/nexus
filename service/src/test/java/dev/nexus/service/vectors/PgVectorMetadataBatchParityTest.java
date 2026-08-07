// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
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
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6yps0 — parity coverage for {@link PgVectorRepository}'s batched replacement
 * of the former per-row {@code updateMetadataOneRow} loop + unbounded
 * {@code chash().in(...)} inside {@code resolveNeedEmbedIdx}'s have-vector branch (T2
 * [21552] deep-analysis third finding: a 0.024s/chunk floor on an UNCHANGED file's
 * re-index pass).
 *
 * <p>Real Postgres round trip throughout (Testcontainers pgvector/pgvector:pg17,
 * {@code nexus_svc}-shaped plain LOGIN NOSUPERUSER role, PER_CLASS) — house rule:
 * prefer a real substrate over a mock. Setup mirrors {@code
 * PgVectorEmbedSkipIntegrationTest} / {@code PgVectorEmbedSkipGcRaceTest}.
 *
 * <p>The load-bearing case per the bead's own acceptance criterion: a batch whose
 * size EXCEEDS {@link PgVectorRepository#SOURCE_URI_JOIN_BATCH} (300) — the constant
 * both the bounded {@code selectExistingChashTextCtx} existence-SELECT and the
 * batched metadata UPDATE now chunk at — so the parity assertions below exercise
 * TWO+ pages of both, not just the single-page case a smaller fixture would leave
 * unverified.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorMetadataBatchParityTest {

    private static final String SVC_ROLE = "svc_metabatch_test";
    private static final String SVC_PASS = "svc_metabatch_test_pass";
    private static final String TENANT   = "metabatch-tenant";

    /** One more than SOURCE_URI_JOIN_BATCH — guarantees a partial second page. */
    private static final int OVER_PAGE_COUNT = PgVectorRepository.SOURCE_URI_JOIN_BATCH + 50;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CountingEmbedder embedder;
    private PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_1024 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_document_chunks, nexus.catalog_documents TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        embedder = new CountingEmbedder(1024);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * The bead's core case: a batch of {@link #OVER_PAGE_COUNT} chashes, all with
     * text unchanged from the seed — every one takes the have-vector metadata-only
     * path, spanning TWO pages of both the bounded existence-SELECT and the batched
     * UPDATE. Parity means: (a) zero embed calls (nothing needed re-embedding), (b)
     * EVERY row's metadata reflects its own NEW per-row-distinct value (a
     * mis-indexed batch — e.g. row i receiving row i+1's value — would fail this),
     * (c) row count is unchanged (no accidental duplicate INSERTs), (d) embeddings
     * are byte-for-byte untouched (proves no re-embed silently happened for anyone).
     */
    @Test
    void overPageBatch_allUnchangedText_everyRowGetsItsOwnRefreshedMetadata_noReEmbed() throws Exception {
        String col = "code__metabatch-refresh__voyage-code-3__v1";
        List<String> chashes = new ArrayList<>(OVER_PAGE_COUNT);
        List<String> texts = new ArrayList<>(OVER_PAGE_COUNT);
        List<Map<String, Object>> seedMetas = new ArrayList<>(OVER_PAGE_COUNT);
        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            chashes.add(Chash.ofText(TENANT + "-refresh-" + i).toHex());
            texts.add("stable text " + i);
            seedMetas.add(Map.of("v", "seed-" + i));
        }
        repo.upsertChunks(TENANT, col, chashes, texts, seedMetas);
        assertThat(superuserCount(col)).isEqualTo((long) OVER_PAGE_COUNT);

        Map<String, String> firstVectorByChash = new java.util.HashMap<>();
        for (String chash : chashes) firstVectorByChash.put(chash, superuserEmbedding(col, chash));

        int callsBeforeRefresh = embedder.callCount();

        // SAME text (every chash takes have-vector), DIFFERENT metadata per row —
        // exact SAME chashes/texts, refreshed metadata with a distinct value per index
        // so a cross-row mixup is detectable.
        List<Map<String, Object>> refreshedMetas = new ArrayList<>(OVER_PAGE_COUNT);
        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            refreshedMetas.add(Map.of("v", "refreshed-" + i));
        }
        repo.upsertChunks(TENANT, col, chashes, texts, refreshedMetas);

        assertThat(embedder.callCount())
            .as("every chash in this batch is have-vector/unchanged-text -- zero embed calls")
            .isEqualTo(callsBeforeRefresh);
        assertThat(superuserCount(col))
            .as("no duplicate rows -- exactly the original row count")
            .isEqualTo((long) OVER_PAGE_COUNT);

        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            String chash = chashes.get(i);
            assertThat(rawMetadataV(col, chash))
                .as("row %d (chash=%s) must carry ITS OWN refreshed value, not a neighbor's "
                    + "-- exercised across a %d-row batch, over 1 page (%d) of the batched UPDATE",
                    i, chash, OVER_PAGE_COUNT, PgVectorRepository.SOURCE_URI_JOIN_BATCH)
                .isEqualTo("refreshed-" + i);
            assertThat(superuserEmbedding(col, chash))
                .as("row %d's embedding must be byte-identical to before -- no re-embed happened", i)
                .isEqualTo(firstVectorByChash.get(chash));
        }
    }

    /**
     * Mixed batch spanning two existence-SELECT/batched-UPDATE pages: a handful of
     * chashes scattered across the {@link #OVER_PAGE_COUNT}-sized batch carry
     * DIFFERENT text (content-divergent) while the rest are unchanged. The divergent
     * ones must be re-embedded (need-embed path, no metadata-only UPDATE issued for
     * them); the unchanged ones must still get their batched metadata refresh — and
     * this partition must hold for EVERY index regardless of which of the two
     * {@link PgVectorRepository#SOURCE_URI_JOIN_BATCH}-sized pages it lands in.
     * {@code upsertChunksInternal} sorts the dedup batch by chash before this point
     * (nexus-ps9wb lock-ordering), so which literal page a given "divergent-by-
     * construction-index" chash lands in is a function of its hash value, not this
     * test's insertion order — the per-index assertion loop below checks every one
     * of the {@link #OVER_PAGE_COUNT} rows unconditionally, so both pages are
     * exhaustively covered either way.
     */
    @Test
    void overPageBatch_mixedDivergentAndUnchanged_acrossPageBoundary_correctlyPartitioned() throws Exception {
        String col = "code__metabatch-mixed__voyage-code-3__v1";
        int pageSize = PgVectorRepository.SOURCE_URI_JOIN_BATCH;
        List<String> chashes = new ArrayList<>(OVER_PAGE_COUNT);
        List<String> seedTexts = new ArrayList<>(OVER_PAGE_COUNT);
        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            chashes.add(Chash.ofText(TENANT + "-mixed-" + i).toHex());
            seedTexts.add("seed text " + i);
        }
        List<Map<String, Object>> seedMetas = new ArrayList<>(OVER_PAGE_COUNT);
        for (int i = 0; i < OVER_PAGE_COUNT; i++) seedMetas.add(Map.of("v", "seed"));
        repo.upsertChunks(TENANT, col, chashes, seedTexts, seedMetas);

        // Divergent indices straddling BOTH pages: first page (near start, near the
        // page-300 boundary from below) and second page (right after the boundary,
        // near the end).
        Set<Integer> divergentIdx = new LinkedHashSet<>(List.of(0, pageSize - 1, pageSize, OVER_PAGE_COUNT - 1));

        int callsBefore = embedder.callCount();

        List<String> secondPassTexts = new ArrayList<>(OVER_PAGE_COUNT);
        List<Map<String, Object>> secondPassMetas = new ArrayList<>(OVER_PAGE_COUNT);
        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            boolean divergent = divergentIdx.contains(i);
            secondPassTexts.add(divergent ? "CHANGED text " + i : seedTexts.get(i));
            secondPassMetas.add(Map.of("v", divergent ? "divergent" : "refreshed"));
        }
        repo.upsertChunks(TENANT, col, chashes, secondPassTexts, secondPassMetas);

        assertThat(embedder.callCount() - callsBefore)
            .as("exactly one embed CALL covering all 4 divergent chashes (batched embed of the "
                + "need-embed subset)")
            .isEqualTo(1);

        for (int i = 0; i < OVER_PAGE_COUNT; i++) {
            String chash = chashes.get(i);
            if (divergentIdx.contains(i)) {
                assertThat(rawStoredText(col, chash))
                    .as("divergent index %d must have been RE-embedded (text rewritten)", i)
                    .isEqualTo("CHANGED text " + i);
                assertThat(rawMetadataV(col, chash)).isEqualTo("divergent");
            } else {
                assertThat(rawStoredText(col, chash))
                    .as("unchanged index %d's text must be untouched", i)
                    .isEqualTo(seedTexts.get(i));
                assertThat(rawMetadataV(col, chash))
                    .as("unchanged index %d must have taken the batched metadata-only refresh", i)
                    .isEqualTo("refreshed");
            }
        }
        assertThat(superuserCount(col)).isEqualTo((long) OVER_PAGE_COUNT);
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    private long superuserCount(String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_1024 WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private String superuserEmbedding(String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT embedding::text FROM nexus.chunks_1024 WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist", collection, chash).isTrue();
                return rs.getString(1);
            }
        }
    }

    private String rawMetadataV(String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT metadata->>'v' FROM nexus.chunks_1024 WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist", collection, chash).isTrue();
                return rs.getString(1);
            }
        }
    }

    private String rawStoredText(String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT chunk_text FROM nexus.chunks_1024 WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist", collection, chash).isTrue();
                return rs.getString(1);
            }
        }
    }

    /** Embedder stub that counts invocations and tags each vector with the call's serial number. */
    private static final class CountingEmbedder implements Embedder {
        private final int dim;
        private final AtomicInteger calls = new AtomicInteger();

        CountingEmbedder(int dim) {
            this.dim = dim;
        }

        int callCount() {
            return calls.get();
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.incrementAndGet();
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) out.add(new float[dim]);
            return out;
        }
    }
}

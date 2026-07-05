// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
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
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-181 (bead nexus-f0r8p.2): Testcontainers-backed integration coverage for the
 * server-side embed-skip wiring in {@link PgVectorRepository#upsertChunksInternal}.
 *
 * <p>Complements the hermetic {@link PgVectorEmbedSkipTest} (pure {@code partitionByExistence}
 * + the SELECT-error fail-safe against a stub {@code DataSource}) with scenarios that need a
 * real pgvector table: the row-count-returning {@code updateMetadata}, metadata parity between
 * the have-vector UPDATE path and the insert path, the mixed-batch index-realignment seam
 * (embeddings must stay aligned to {@code insertIdx}, not to the full dedup batch), and the
 * SELECT+UPDATE-before-embed transaction-ordering invariant.
 *
 * <p>Testcontainers setup mirrors {@code PgVectorRepositoryContractTest} (pgvector/pgvector:pg17,
 * PER_CLASS lifecycle, plain-LOGIN NOSUPERUSER role) but is trimmed to only what these tests
 * touch: {@code chunks_1024} + {@code catalog_collections} (the auto-stub registration every
 * {@code upsertChunks} call performs).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorEmbedSkipIntegrationTest {

    private static final String SVC_ROLE = "svc_embedskip_test";
    private static final String SVC_PASS = "svc_embedskip_test_pass";
    private static final String TENANT_A = "tenant-a";

    private PostgreSQLContainer<?> pg;
    private TenantScope tenantScope;
    private HikariDataSource svcDs;

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
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_1024 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
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
        if (pg    != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // (a) updateMetadata returns the affected-row count.
    // ---------------------------------------------------------------------------

    @Test
    void updateMetadata_returnsAffectedRowCount_existingRowIsOne_missingRowIsZero() throws Exception {
        String col = "code__embedskip-updatemeta__voyage-code-3__v1";
        String chash = "ums10000000000000000000000000000";
        repo.upsertChunks(TENANT_A, col, List.of(chash), List.of("hello world"), List.of(Map.of("v", "1")));

        int affected = repo.updateMetadata(TENANT_A, col, List.of(chash), List.of(Map.of("v", "2")));
        assertThat(affected).as("metadata-only UPDATE on an existing row affects exactly 1 row").isEqualTo(1);

        // This is the RDR-181 race signal at the primitive level: a chash absent from
        // the table (e.g. hard-deleted between an existence SELECT and this UPDATE)
        // returns 0 rather than silently no-op'ing — the caller in upsertChunksInternal
        // reroutes a 0 result into the need-embed set instead of dropping the chash.
        int affectedMissing = repo.updateMetadata(TENANT_A, col,
                List.of("doesnotexist000000000000000000000"), List.of(Map.of("v", "x")));
        assertThat(affectedMissing).as("metadata-only UPDATE on a nonexistent row affects 0 rows")
                .isEqualTo(0);
    }

    // ---------------------------------------------------------------------------
    // (b) Metadata parity + embedding untouched on a repeat upsert of the same chash.
    // ---------------------------------------------------------------------------

    @Test
    void upsertChunks_sameChashTwice_secondCallSkipsEmbed_metadataRefreshed_vectorUnchanged() throws Exception {
        String col = "code__embedskip-parity__voyage-code-3__v1";
        String chash = "eskp1000000000000000000000000000";

        // CountingEmbedder is a shared @TestInstance(PER_CLASS) field, so its counter
        // is cumulative across every test method in this class — assert on the DELTA
        // this test's own calls produce, never on an absolute value.
        int callsBefore = embedder.callCount();
        repo.upsertChunks(TENANT_A, col, List.of(chash), List.of("stable text"),
                List.of(Map.of("frecency_score", "0.1")));
        int callsAfterFirst = embedder.callCount();
        assertThat(callsAfterFirst - callsBefore)
                .as("first upsert of a brand-new chash must embed").isEqualTo(1);
        String firstVector = superuserEmbedding(col, chash);

        repo.upsertChunks(TENANT_A, col, List.of(chash), List.of("stable text"),
                List.of(Map.of("frecency_score", "0.9")));

        assertThat(embedder.callCount())
                .as("re-upserting the SAME chash must skip the embedder entirely — the whole win")
                .isEqualTo(callsAfterFirst);

        String secondVector = superuserEmbedding(col, chash);
        assertThat(secondVector)
                .as("the stored embedding must be UNCHANGED — proves no re-embed happened")
                .isEqualTo(firstVector);

        Map<String, Object> got = repo.get(TENANT_A, col, List.of(chash), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(metas).hasSize(1);
        // Metadata-parity acceptance criterion: the have-vector UPDATE branch must
        // write metadata indistinguishable from what a fresh insert would have
        // written — same sanitizeNulDeep + toJson shape, reused verbatim via
        // updateMetadataOneRow, so the refreshed value must be exactly the second
        // call's value.
        assertThat(metas.get(0).get("frecency_score"))
                .as("metadata must be refreshed by the have-vector branch (parity with the insert path)")
                .isEqualTo("0.9");
    }

    // ---------------------------------------------------------------------------
    // Mixed batch: the index-realignment seam. A batch containing BOTH have-vector
    // and need-embed chashes must not cross-contaminate embeddings across indices —
    // embeddings is aligned to insertIdx (position k), never to the full dedup batch
    // (position idx). Getting this wrong silently pairs the wrong vector with a chash.
    // ---------------------------------------------------------------------------

    @Test
    void upsertChunks_mixedBatch_onlyNewChashEmbedded_haveVectorRowsUntouchedAndCorrectlyAligned() throws Exception {
        String col = "code__embedskip-mixed__voyage-code-3__v1";
        String chashOld1 = "mob10000000000000000000000000000";
        String chashOld2 = "mob20000000000000000000000000000";
        String chashNew  = "mob30000000000000000000000000000";

        repo.upsertChunks(TENANT_A, col, List.of(chashOld1, chashOld2),
                List.of("old text one", "old text two"),
                List.of(Map.of("v", "old1"), Map.of("v", "old2")));
        int callsAfterSeed = embedder.callCount();
        String oldVec1 = superuserEmbedding(col, chashOld1);
        String oldVec2 = superuserEmbedding(col, chashOld2);

        // Mixed batch: chashOld1/chashOld2 already have vectors (metadata-refresh
        // only); chashNew is genuinely new (needs embed). The dedup+chash-sort inside
        // upsertChunksInternal reorders the batch internally — these assertions must
        // hold regardless of that reorder.
        repo.upsertChunks(TENANT_A, col,
                List.of(chashOld1, chashOld2, chashNew),
                List.of("old text one", "old text two", "brand new text"),
                List.of(Map.of("v", "refreshed1"), Map.of("v", "refreshed2"), Map.of("v", "new")));

        assertThat(embedder.callCount())
                .as("exactly one embed CALL for the mixed batch (batched across the single new chash)")
                .isEqualTo(callsAfterSeed + 1);

        assertThat(superuserEmbedding(col, chashOld1))
                .as("chashOld1's vector must be untouched by the metadata-only refresh")
                .isEqualTo(oldVec1);
        assertThat(superuserEmbedding(col, chashOld2))
                .as("chashOld2's vector must be untouched by the metadata-only refresh")
                .isEqualTo(oldVec2);

        Map<String, Object> got = repo.get(TENANT_A, col,
                List.of(chashOld1, chashOld2, chashNew), 10, 0);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(ids).as("all three chashes visible after the mixed batch")
                .containsExactlyInAnyOrder(chashOld1, chashOld2, chashNew);
        Map<String, String> expectedByChash = Map.of(
                chashOld1, "refreshed1",
                chashOld2, "refreshed2",
                chashNew,  "new");
        for (int i = 0; i < ids.size(); i++) {
            assertThat(metas.get(i).get("v"))
                    .as("metadata for %s must reflect the mixed-batch refresh, not a misaligned neighbor's value",
                        ids.get(i))
                    .isEqualTo(expectedByChash.get(ids.get(i)));
        }

        assertThat(superuserCount(col)).as("exactly 3 rows total, the new chash actually landed")
                .isEqualTo(3L);
    }

    // ---------------------------------------------------------------------------
    // (c) 0-row-fallback self-heal: a chash whose row disappears must never be
    // silently dropped — it is recreated (re-embedded + re-inserted) on the next
    // upsert touching it. This reproduces the OUTCOME of the RDR-181 orphan-GC race
    // (a hard-deleted chash never stays permanently lost) at the black-box level;
    // it does not fabricate the exact within-transaction SELECT-then-UPDATE
    // interleaving (there is no test seam inside resolveNeedEmbedIdx's single
    // transaction to inject a concurrent delete mid-call, and adding one would be
    // scope creep onto production code for this bead). The precise 0-row RETURN
    // VALUE behavior — the actual signal upsertChunksInternal reroutes on — is
    // covered directly by updateMetadata_returnsAffectedRowCount_existingRowIsOne_missingRowIsZero
    // above (a metadata-only UPDATE against an absent chash returns 0).
    // ---------------------------------------------------------------------------

    @Test
    void upsertChunks_chashDeletedThenReupserted_recreatedNotSilentlyDropped() throws Exception {
        String col = "code__embedskip-selfheal__voyage-code-3__v1";
        String chash = "shl10000000000000000000000000000";

        repo.upsertChunks(TENANT_A, col, List.of(chash), List.of("will be deleted"), List.of(Map.of("v", "1")));
        assertThat(superuserCount(col)).isEqualTo(1L);

        int deleted = repo.delete(TENANT_A, col, List.of(chash));
        assertThat(deleted).isEqualTo(1);
        assertThat(superuserCount(col)).as("row is gone (simulates orphan-GC)").isEqualTo(0L);

        int callsBeforeReupsert = embedder.callCount();
        repo.upsertChunks(TENANT_A, col, List.of(chash), List.of("will be deleted"), List.of(Map.of("v", "2")));

        assertThat(embedder.callCount())
                .as("the deleted chash must be RE-embedded, not silently skipped as if it already had a vector")
                .isEqualTo(callsBeforeReupsert + 1);
        assertThat(superuserCount(col))
                .as("the chash ends up correctly re-inserted, never permanently lost")
                .isEqualTo(1L);
    }

    // ---------------------------------------------------------------------------
    // (d) Transaction-ordering invariant: the existence SELECT + have-vector UPDATE
    // must COMMIT before embed() is invoked. Mechanically verified (not just by code
    // reading) via a raw side-channel read from a SEPARATE connection during the
    // embed() callback: if ordering were violated (embed invoked before the
    // have-vector UPDATE's transaction commits), the raw read would observe the
    // PRE-call metadata value instead of the post-update value.
    // ---------------------------------------------------------------------------

    @Test
    void resolveNeedEmbedIdx_haveVectorUpdateCommitsBeforeEmbedIsInvoked() throws Exception {
        String col = "code__embedskip-order__voyage-code-3__v1";
        String chashHaveVector = "ordr1000000000000000000000000000"; // pre-existing, have-vector this call
        String chashNeedEmbed  = "ordr2000000000000000000000000000"; // new, needs embed this call

        repo.upsertChunks(TENANT_A, col, List.of(chashHaveVector), List.of("have vector text"),
                List.of(Map.of("v", "old")));

        AtomicReference<String> observedDuringEmbed = new AtomicReference<>();
        Embedder orderingProbe = new Embedder() {
            @Override
            public List<float[]> embed(List<String> texts) {
                // A SEPARATE connection (not the repo's own transaction) reading
                // chashHaveVector's metadata right now. If resolveNeedEmbedIdx's
                // existence-check transaction (SELECT + have-vector UPDATE) has
                // already committed — as the RDR-181 Technical Design requires
                // BEFORE embed() runs — this observes "new". Any bug that invoked
                // embed() before that transaction committed would observe "old".
                try {
                    observedDuringEmbed.set(rawMetadataV(col, chashHaveVector));
                } catch (SQLException e) {
                    throw new RuntimeException(e);
                }
                List<float[]> out = new ArrayList<>(texts.size());
                for (String ignored : texts) out.add(new float[1024]);
                return out;
            }
        };
        PgVectorRepository orderingRepo = new PgVectorRepository(tenantScope, orderingProbe, orderingProbe);

        orderingRepo.upsertChunks(TENANT_A, col,
                List.of(chashHaveVector, chashNeedEmbed),
                List.of("have vector text", "brand new text"),
                List.of(Map.of("v", "new"), Map.of("v", "irrelevant")));

        assertThat(observedDuringEmbed.get())
                .as("the have-vector UPDATE (chashHaveVector -> 'new') must be committed and " +
                    "externally visible BEFORE embed() is invoked for chashNeedEmbed — the single " +
                    "most load-bearing ordering invariant in RDR-181 (steps 1+3 commit before step 4)")
                .isEqualTo("new");
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
            ps.setString(2, chash);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist", collection, chash).isTrue();
                return rs.getString(1);
            }
        }
    }

    /** Raw read of the {@code v} metadata key via a fresh superuser connection (side-channel). */
    private String rawMetadataV(String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT metadata->>'v' FROM nexus.chunks_1024 WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setString(2, chash);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) return null;
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
            int call = calls.incrementAndGet();
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = call;
                out.add(v);
            }
            return out;
        }
    }
}

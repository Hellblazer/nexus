// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
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
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-f0r8p.4 — the Critical write-safety regression test for RDR-181's
 * shared-chash GC race (docs/rdr/rdr-181-server-side-embed-skip-on-reindex.md §Risks
 * and Mitigations / §Failure Modes).
 *
 * <p><strong>The race.</strong> A chash H shared by two catalog documents (normal per
 * RDR-108: identical chunk text collapses to one {@code chunks_<dim>} row; two manifest
 * positions can point at the same chash). A re-index of doc B sees H present via the
 * existence SELECT inside {@link PgVectorRepository#resolveNeedEmbedIdx}, intending to
 * take the metadata-only have-vector path — but a concurrent orphan-GC hard delete
 * (e.g. doc A no longer referencing H, {@link PgVectorRepository#delete}) removes H
 * between that SELECT and the have-vector UPDATE. The single-writer MVV
 * ({@code PgVectorEmbedSkipIntegrationTest}) cannot exercise this: it has no concurrent
 * second writer. This suite reproduces the exact interleaving deterministically.
 *
 * <p><strong>Deterministic interleaving.</strong> {@code resolveNeedEmbedIdx}'s
 * existence-SELECT-then-have-vector-UPDATE pair runs inside ONE transaction (by design,
 * for the atomicity {@code .2} depends on) — there is no natural place to inject a
 * concurrent delete between them from outside. Rather than a {@code Thread.sleep}-timed
 * approximation (flaky — the existing concurrency suites in this package,
 * {@link PgVectorUpsertDeadlockTest} and {@link ChashVectorConcurrencyTest}, both avoid
 * sleep-based timing for exactly this reason), this test installs the
 * {@code afterExistencePartitionHookForTests} seam added on {@link PgVectorRepository}
 * for this bead: a test-only callback invoked after the existence SELECT resolves and
 * before the have-vector UPDATE loop begins, letting the racing writer's transaction be
 * paused deterministically with a {@link CountDownLatch} while a second thread's delete
 * commits, then resumed.
 *
 * <p><strong>Expected outcome (the self-heal, not a crash).</strong> The paused
 * transaction's have-vector UPDATE now matches 0 rows (H was deleted and committed by
 * the other thread; READ COMMITTED gives the UPDATE statement a fresh snapshot). Per the
 * RDR-181 design, a 0-row UPDATE reroutes that chash into need-embed — H is re-embedded
 * and re-inserted, never silently dropped. {@code fetchDocumentChunks} for the surviving
 * document must succeed afterward with no {@link IllegalStateException}
 * ({@code PgVectorRepository#fetchDocumentChunks}'s "never a silently partial document"
 * contract).
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc}-shaped plain
 * LOGIN NOSUPERUSER role, PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorEmbedSkipGcRaceTest {

    private static final String SVC_ROLE = "svc_gcrace_test";
    private static final String SVC_PASS = "svc_gcrace_test_pass";
    private static final String TENANT   = "gcrace-tenant";

    private static final String COLLECTION = "code__gcrace__voyage-code-3__v1";
    // 32-hex-char chash (chunks_1024_chash_len_check: length(chash) = 32) — the
    // shared chash H referenced by both docs A and B.
    private static final String CHASH_H = String.format("%032x", 0xABCDEF01L);
    private static final String SHARED_TEXT = "shared chunk text referenced by docs A and B";

    private static final String TUMBLER_A = "9.2.1";
    private static final String TUMBLER_B = "9.2.2";

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
                "GRANT SELECT ON nexus.catalog_documents, nexus.catalog_document_chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        // >=2 so the paused racing writer's held connection and the concurrent
        // delete's own connection can be open at the same time without contending
        // for the same physical connection.
        cfg.setMaximumPoolSize(4);
        cfg.setConnectionTimeout(10_000);
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

    @Test
    void concurrentGcDeleteBetweenExistenceSelectAndHaveVectorUpdate_selfHealsNoDataLoss() throws Exception {
        // 1. Seed H once (normal insert path — it does not exist yet) and register two
        //    manifest positions (docs A and B) pointing at the SAME chash — the RDR-108
        //    shared-chash scenario the whole race depends on.
        repo.upsertChunks(TENANT, COLLECTION, List.of(CHASH_H), List.of(SHARED_TEXT),
                List.of(Map.of("v", "1")));
        assertThat(superuserCount()).as("H exists after the initial seed insert").isEqualTo(1L);

        seedCatalogDocument(TUMBLER_A, "Doc A");
        seedCatalogDocument(TUMBLER_B, "Doc B");
        seedManifestRow(TUMBLER_A, 0, CHASH_H);
        seedManifestRow(TUMBLER_B, 0, CHASH_H);

        // Sanity: both documents resolve BEFORE the race.
        assertThat(repo.fetchDocumentChunks(TENANT, TUMBLER_A)).hasSize(1);
        assertThat(repo.fetchDocumentChunks(TENANT, TUMBLER_B)).hasSize(1);

        int embedCallsBeforeRace = embedder.callCount();

        // 2. Install the interleaving seam: pause the racing writer's transaction right
        //    after its existence SELECT resolves (H confirmed present), before its
        //    have-vector UPDATE runs.
        CountDownLatch paused = new CountDownLatch(1);
        CountDownLatch resume = new CountDownLatch(1);
        AtomicBoolean hookFired = new AtomicBoolean(false);
        repo.setAfterExistencePartitionHookForTests(() -> {
            hookFired.set(true);
            paused.countDown();
            try {
                boolean released = resume.await(30, TimeUnit.SECONDS);
                if (!released) {
                    throw new IllegalStateException("test timed out waiting to be resumed");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            }
        });

        // 3. Kick off doc B's re-index of H on a worker thread — SAME text, so it takes
        //    the have-vector (metadata-only) branch and races the concurrent delete
        //    below. Different metadata value ("2") so a successful re-embed is
        //    distinguishable from the original seed.
        AtomicReference<Throwable> workerError = new AtomicReference<>();
        Thread worker = new Thread(() -> {
            try {
                repo.upsertChunks(TENANT, COLLECTION, List.of(CHASH_H), List.of(SHARED_TEXT),
                        List.of(Map.of("v", "2")));
            } catch (Throwable t) {
                workerError.set(t);
            }
        }, "gc-race-worker");
        worker.start();

        try {
            // 4. Wait for the worker to reach the paused interleaving point.
            assertThat(paused.await(30, TimeUnit.SECONDS))
                .as("the racing writer must reach the post-existence-SELECT pause point")
                .isTrue();

            // 5. While paused: simulate the concurrent orphan-GC hard delete of H (e.g.
            //    doc A's own re-index dropped its reference and GC swept the orphan).
            int deleted = repo.delete(TENANT, COLLECTION, List.of(CHASH_H));
            assertThat(deleted).as("the concurrent GC delete removes exactly the one row").isEqualTo(1);
            assertThat(superuserCount())
                .as("the delete has committed and is visible outside the paused transaction")
                .isEqualTo(0L);
        } finally {
            // 6. Release the paused writer so its have-vector UPDATE runs against the
            //    now-deleted row.
            resume.countDown();
        }

        worker.join(TimeUnit.SECONDS.toMillis(30));
        assertThat(worker.isAlive()).as("the racing writer must finish within the join budget").isFalse();
        assertThat(workerError.get())
            .as("the racing writer must complete without throwing — the self-heal must be silent to the caller")
            .isNull();
        assertThat(hookFired.get()).as("the interleaving seam must actually have fired").isTrue();

        // 7. H must exist afterward — re-created via the need-embed reroute, not lost.
        assertThat(superuserCount())
            .as("H exists again after the self-heal re-embed + re-insert")
            .isEqualTo(1L);
        assertThat(embedder.callCount())
            .as("the reroute must have actually re-embedded H (proves the reroute took the "
                + "need-embed path, not a silent no-op)")
            .isEqualTo(embedCallsBeforeRace + 1);

        Map<String, Object> got = repo.get(TENANT, COLLECTION, List.of(CHASH_H), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(metas).hasSize(1);
        assertThat(metas.get(0).get("v"))
            .as("the re-inserted row carries the racing writer's metadata, not the stale seed value")
            .isEqualTo("2");

        // 8. The write-safety proof: fetchDocumentChunks for the surviving document
        //    (B) must succeed — no IllegalStateException, no dangling manifest
        //    reference (PgVectorRepository.fetchDocumentChunks's "never a silently
        //    partial document" contract).
        List<Map<String, Object>> rowsB = repo.fetchDocumentChunks(TENANT, TUMBLER_B);
        assertThat(rowsB).hasSize(1);
        assertThat(rowsB.get(0).get("chash")).isEqualTo(CHASH_H);
        assertThat(rowsB.get(0).get("chunk_text")).isEqualTo(SHARED_TEXT);

        // Doc A too — both manifest positions pointed at the shared chash and both must
        // still resolve; the GC-race self-heal must not leave EITHER document broken.
        List<Map<String, Object>> rowsA = repo.fetchDocumentChunks(TENANT, TUMBLER_A);
        assertThat(rowsA).hasSize(1);
        assertThat(rowsA.get(0).get("chash")).isEqualTo(CHASH_H);
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    private long superuserCount() throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_1024 WHERE collection = ? AND chash = ?")) {
            ps.setString(1, COLLECTION);
            ps.setString(2, CHASH_H);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private void seedCatalogDocument(String tumbler, String title) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES (?, ?, ?)")) {
            ps.setString(1, TENANT);
            ps.setString(2, tumbler);
            ps.setString(3, title);
            ps.executeUpdate();
        }
    }

    private void seedManifestRow(String tumbler, int position, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.catalog_document_chunks "
                 + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, ?, ?, ?)")) {
            ps.setString(1, TENANT);
            ps.setString(2, tumbler);
            ps.setInt(3, position);
            ps.setString(4, chash);
            ps.setString(5, COLLECTION);
            ps.executeUpdate();
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

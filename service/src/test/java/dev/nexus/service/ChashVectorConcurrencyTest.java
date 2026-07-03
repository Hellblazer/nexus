// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
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

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-h8rf6.2 — reproduces {@code /v1/chash/upsert_many} 500s under concurrent
 * indexing load against a shared HikariCP pool.
 *
 * <p><strong>Root-cause.</strong> Both {@code ChashRepository
 * .ensureCollectionRegistered} and {@code PgVectorRepository.upsertChunksInternal}
 * issue an {@code INSERT INTO nexus.catalog_collections ... ON CONFLICT (tenant_id,
 * name) DO NOTHING} for every batch write, so the collection stub exists before the
 * chash/chunk row lands. PostgreSQL's {@code ON CONFLICT} clause takes a value lock
 * on the conflicting unique-index entry: when two concurrent transactions target the
 * SAME {@code (tenant_id, name)} row, the second BLOCKS until the first commits or
 * rolls back — even though the eventual outcome is a no-op — for as long as the
 * winning transaction keeps its connection open (the rest of its own batch). Under
 * concurrent indexing (one physical collection serves an entire indexing run, so
 * chash and vector-upsert batches for many files land on the SAME row) enough
 * concurrent losers piling up this way exhausts the shared HikariCP pool: unrelated
 * requests then fail {@code dataSource.getConnection()} with
 * {@code SQLTransientConnectionException} ("Connection is not available, request
 * timed out"), which {@code TenantScope} wraps and the handlers' catch-all turned
 * into an opaque 500 (pre-fix). Matches the reported shape ("~16 of the first 21
 * files" of a shakeout indexing run all writing into one collection).
 *
 * <p><strong>Repro-methodology note.</strong> A DELIBERATELY adversarial variant of
 * this suite (threads racing a collection name that rotates every second, forcing a
 * repeated first-registration burst rather than one settling into steady state) was
 * used during development to confirm the mechanism end-to-end: it reliably produced
 * {@code SQLTransientConnectionException} ("active=N, waiting=M") and dozens of raw
 * 500s pre-fix, and after the fix those became typed 503s (see
 * {@code CollectionRegistry} + the pool-exhaustion mapping in {@code HttpUtil}). That
 * variant is NOT shipped here: on Testcontainers' near-zero-RTT localhost Postgres, a
 * single registration burst against ONE collection resolves in low-single-digit
 * milliseconds regardless of fix state, so continuously rotating collections was
 * necessary to keep the race alive for a whole test window — but that also means it
 * stress-tests raw pool CAPACITY under permanent oversubscription (a different, valid
 * concern the fix does not claim to solve) rather than cleanly isolating the
 * registration-contention fix. The suite below instead models the REALISTIC
 * production shape — ONE collection, many concurrent writers, gated to race the
 * first registration together — which is what {@code CollectionRegistry} fixes.
 *
 * <p>This suite launches {@link #CHASH_THREADS} chash-upsert workers and
 * {@link #VECTOR_THREADS} vector-upsert workers, gated on a {@link CountDownLatch}
 * so their first requests fire in the same instant against ONE brand-new collection
 * — the worst-case first-registration burst — then keeps looping for
 * {@link #RUN_TIME} so the (much larger) steady-state portion of the run is
 * exercised too. Chash/chunk IDs are unique per request so the ONLY shared
 * contention point is the {@code catalog_collections} registration row — isolating
 * the mechanism under test from ordinary chash/chunk PK contention. The pool is
 * smaller than production ({@link #POOL_SIZE} vs. {@code NX_POOL_SIZE}'s default 10)
 * so oversubscription is exercised without production-scale concurrency, while
 * {@link #CONNECTION_TIMEOUT_MS} still leaves multi-second headroom (HikariCP's own
 * default is 30s) — the fix is expected to clear zero 5xx under this budget.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role (full
 * DML via {@code grants-nexus-svc.xml}, mirrors {@link PgVectorServingContractTest}),
 * {@link PgVectorRepositoryContractTest.FakeEmbedder}, port 0, PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashVectorConcurrencyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "concur-test-token-0123456789abcdef";
    private static final String TENANT = "concur-tenant";

    // ONE collection, never touched before this test runs — every worker's first
    // request races to register it, the worst-case burst this suite targets.
    private static final String COLLECTION = "code__concur__voyage-code-3__v1";

    private static final int CHASH_THREADS   = 6;
    private static final int VECTOR_THREADS  = 6;
    private static final int CHASH_BATCH     = 100;
    private static final int VECTOR_BATCH    = 60;
    private static final Duration RUN_TIME   = Duration.ofSeconds(10);
    private static final int POOL_SIZE       = 6;
    private static final int CONNECTION_TIMEOUT_MS = 3000;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
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
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "chash-vector-concurrency-test");
            ps.executeUpdate();
        }

        // Deliberately smaller-than-production pool + a still-generous (but bounded)
        // connectionTimeout — see class doc for why these values were chosen.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(POOL_SIZE);
        cfg.setConnectionTimeout(CONNECTION_TIMEOUT_MS);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var tenantScope = new TenantScope(svcDs);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        var pgRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // NexusService wires ChashHandler and VectorHandler off the SAME DataSource
        // (hence the SAME HikariCP pool) — exactly the production topology this bug
        // depends on.
        service = new NexusService(0, TOKEN, svcDs, null, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();

        // Warm the pool BEFORE the synchronized burst: HikariCP establishes its
        // physical connections asynchronously in the background after the pool is
        // constructed, so firing the concurrent burst immediately can race a
        // still-initializing connection (observed as a spurious "I/O error occurred
        // while sending to the backend" unrelated to the mechanism under test).
        // Sequentially borrowing maximumPoolSize connections here forces every
        // physical connection to be fully established first.
        for (int i = 0; i < POOL_SIZE; i++) {
            try (Connection warm = svcDs.getConnection()) {
                warm.isValid(1);
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // HTTP helpers
    // ---------------------------------------------------------------------------

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .timeout(Duration.ofSeconds(20))
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // ---------------------------------------------------------------------------
    // Concurrency repro
    // ---------------------------------------------------------------------------

    /**
     * {@link #CHASH_THREADS} + {@link #VECTOR_THREADS} workers, gated to fire their
     * first request simultaneously against ONE brand-new collection, then looping
     * for {@link #RUN_TIME}. Asserts zero 5xx responses across every request issued
     * by every worker.
     */
    @Test
    void concurrentChashAndVectorUpserts_noServerErrors() throws Exception {
        AtomicInteger totalRequests = new AtomicInteger();
        AtomicInteger status5xx     = new AtomicInteger();
        AtomicInteger exceptions    = new AtomicInteger();
        List<String> failures = new CopyOnWriteArrayList<>();

        int totalThreads = CHASH_THREADS + VECTOR_THREADS;
        CountDownLatch startGate = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(totalThreads);

        List<Runnable> tasks = new ArrayList<>();
        for (int t = 0; t < CHASH_THREADS; t++) {
            int threadId = t;
            tasks.add(() -> chashLoop(threadId, startGate, totalRequests, status5xx, exceptions, failures));
        }
        for (int t = 0; t < VECTOR_THREADS; t++) {
            int threadId = t;
            tasks.add(() -> vectorLoop(threadId, startGate, totalRequests, status5xx, exceptions, failures));
        }

        List<java.util.concurrent.Future<?>> futures = new ArrayList<>();
        for (Runnable task : tasks) {
            futures.add(pool.submit(task));
        }
        // Release every worker's first request at once — the worst-case
        // first-registration burst this suite targets (see class doc).
        startGate.countDown();

        pool.shutdown();
        assertThat(pool.awaitTermination(RUN_TIME.plusSeconds(30).toSeconds(), TimeUnit.SECONDS))
            .as("all worker threads must finish within the run window + grace period")
            .isTrue();
        // Propagate any uncaught worker exception (fails the test loudly instead of
        // silently under-counting requests).
        for (var f : futures) {
            f.get();
        }

        assertThat(totalRequests.get()).as("sanity: requests were actually issued").isGreaterThan(0);
        assertThat(status5xx.get())
            .as("zero 5xx across %d requests (%d client-side exceptions); failures: %s",
                totalRequests.get(), exceptions.get(), firstN(failures, 10))
            .isZero();
        assertThat(exceptions.get())
            .as("zero client-side request exceptions; failures: %s", firstN(failures, 10))
            .isZero();
    }

    private static List<String> firstN(List<String> list, int n) {
        return list.size() <= n ? list : new ArrayList<>(list.subList(0, n));
    }

    private void chashLoop(int threadId, CountDownLatch startGate, AtomicInteger totalRequests,
                           AtomicInteger status5xx, AtomicInteger exceptions, List<String> failures) {
        awaitGate(startGate);
        Instant deadline = Instant.now().plus(RUN_TIME);
        int iter = 0;
        while (Instant.now().isBefore(deadline)) {
            List<String> chashes = new ArrayList<>(CHASH_BATCH);
            for (int i = 0; i < CHASH_BATCH; i++) {
                chashes.add("cc-t" + threadId + "-i" + iter + "-" + i);
            }
            iter++;
            try {
                var resp = post("/v1/chash/upsert_many", Map.of(
                    "chashes", chashes, "collection", COLLECTION));
                totalRequests.incrementAndGet();
                if (resp.statusCode() >= 500) {
                    status5xx.incrementAndGet();
                    failures.add("chash t=" + threadId + " status=" + resp.statusCode()
                            + " body=" + truncate(resp.body()));
                }
            } catch (Exception e) {
                totalRequests.incrementAndGet();
                exceptions.incrementAndGet();
                failures.add("chash t=" + threadId + " exception=" + e);
            }
        }
    }

    private void vectorLoop(int threadId, CountDownLatch startGate, AtomicInteger totalRequests,
                            AtomicInteger status5xx, AtomicInteger exceptions, List<String> failures) {
        awaitGate(startGate);
        Instant deadline = Instant.now().plus(RUN_TIME);
        int iter = 0;
        while (Instant.now().isBefore(deadline)) {
            List<String> ids = new ArrayList<>(VECTOR_BATCH);
            List<String> docs = new ArrayList<>(VECTOR_BATCH);
            List<Map<String, Object>> metas = new ArrayList<>(VECTOR_BATCH);
            for (int i = 0; i < VECTOR_BATCH; i++) {
                ids.add(chunkId(threadId, iter, i));
                docs.add("concurrency probe text thread " + threadId + " iter " + iter + " item " + i);
                metas.add(new HashMap<>());
            }
            iter++;
            try {
                var resp = post("/v1/vectors/upsert-chunks", Map.of(
                    "collection", COLLECTION, "ids", ids, "documents", docs, "metadatas", metas));
                totalRequests.incrementAndGet();
                if (resp.statusCode() >= 500) {
                    status5xx.incrementAndGet();
                    failures.add("vector t=" + threadId + " status=" + resp.statusCode()
                            + " body=" + truncate(resp.body()));
                }
            } catch (Exception e) {
                totalRequests.incrementAndGet();
                exceptions.incrementAndGet();
                failures.add("vector t=" + threadId + " exception=" + e);
            }
        }
    }

    private static void awaitGate(CountDownLatch startGate) {
        try {
            startGate.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private static String truncate(String s) {
        return s == null ? "" : (s.length() > 200 ? s.substring(0, 200) + "..." : s);
    }

    /**
     * Exactly-32-char chash id (Chroma natural-ID shape, RDR-108 D1) — required by
     * {@code chunks_1024_chash_len_check} ({@code length(chash) = 32}); a shorter id
     * fails the CHECK constraint and would masquerade as a concurrency-induced 500.
     */
    private static String chunkId(int threadId, int iter, int i) {
        String base = String.format("v%02d%06d%05d", threadId, iter, i);
        return (base + "00000000000000000000000000000000").substring(0, 32);
    }
}

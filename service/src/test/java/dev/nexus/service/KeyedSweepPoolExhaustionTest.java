// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.18): a keyed sweep that fails while the service pool is exhausted sends its
 * error promptly. The failure marker used to run inside {@code HttpUtil.send}'s before-send hook through
 * {@code TenantScope.withTenant}, so the error response waited behind the same exhausted pool (an admission
 * wait, then a connection wait: up to two connection timeouts, past the edge's 30 s bound).
 *
 * <p>The pool here is real and really exhausted: the hook that fails the sweep (inside the sweep's own
 * transaction, which holds one connection) first takes every other connection, then parks a waiter on the pool
 * so the connection the sweep's rollback frees is handed to it, not left for the marker. The connection timeout
 * is {@link #CONNECTION_TIMEOUT_MS}; the old code's response took at least that long.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class KeyedSweepPoolExhaustionTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-keyedsweep-poolx-0123456789abcdef00";
    private static final String SVC_ROLE = "svc_keyedsweep_poolx";
    private static final String SVC_PASS = "svc_keyedsweep_poolx_pass";
    private static final String TENANT = "keyedsweep-poolx";
    private static final String STAMP = "2026-10-18T00:00:00Z";
    private static final String QUARANTINE_PATH = "/v1/vectors/gc/quarantine-orphans";
    private static final int POOL_SIZE = 3;
    private static final long CONNECTION_TIMEOUT_MS = 15_000L;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private PgVectorRepository repo;
    private NexusService service;
    private HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    private final List<Connection> held = new ArrayList<>();
    private volatile CountDownLatch releasePool = new CountDownLatch(1);
    private Thread waiter;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "poolx");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(POOL_SIZE);
        cfg.setConnectionTimeout(CONNECTION_TIMEOUT_MS);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        FakeEmbedder embedder = new FakeEmbedder(384);
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
        http.send(TestHttp.request(url("/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() {
        releasePoolNow();
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void clearHook() {
        repo.setAfterSweepSqlHookForTests(null);
        releasePoolNow();
    }

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** {@code n} chunks aged past the grace with no manifest row: every one is a quarantine victim. */
    private Map<String, Object> seedOrphans(int n) throws Exception {
        String origin = "knowledge__poolx" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        List<String> hashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hashes.add(Chash.ofText(origin + "/" + i).toHex());
            texts.add("chunk " + i);
            vecs.add(new float[384]);
            metas.add(Map.of("title", "t" + i));
        }
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, origin);
            PgContainerHelper.insertChunks(ctx, TENANT, origin, hashes, texts, vecs, metas);
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(ReapableFixtures.PAST_GRACE))
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(origin))).execute();
        });
        var m = new LinkedHashMap<String, Object>();
        m.put("collection", origin);
        m.put("quarantine_collection", "quarantine-" + origin);
        m.put("quarantined_at", STAMP);
        m.put("sample_limit", 20);
        m.put("row_limit", 2);
        return m;
    }

    private HttpResponse<String> post(Map<String, Object> body, String key) throws Exception {
        return http.send(TestHttp.request(url(QUARANTINE_PATH))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .header("Idempotency-Key", key)
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    /**
     * Fail the sweep with {@code failure} after exhausting the pool. Runs inside the sweep's transaction, which
     * holds one connection: take the other {@code POOL_SIZE - 1}, then park a waiter so the connection the
     * sweep's rollback frees is handed to it.
     */
    private void failWithAnExhaustedPool(java.util.function.Supplier<RuntimeException> failure) {
        var release = new CountDownLatch(1);
        releasePool = release;
        repo.setAfterSweepSqlHookForTests(() -> {
            try {
                for (int i = 0; i < POOL_SIZE - 1; i++) {
                    held.add(svcDs.getConnection());
                }
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
            waiter = Thread.ofVirtual().start(() -> {
                try (Connection c = svcDs.getConnection()) {
                    release.await(60, TimeUnit.SECONDS);
                } catch (Exception ignored) {
                    // released or timed out: either way the test is over with this connection
                }
            });
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (svcDs.getHikariPoolMXBean().getThreadsAwaitingConnection() < 1 && System.nanoTime() < deadline) {
                Thread.onSpinWait();
            }
            try {
                Thread.sleep(300); // the waiter is polling the pool's hand-off queue, not just counted
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            throw failure.get();
        });
    }

    private void releasePoolNow() {
        releasePool.countDown();
        for (Connection c : held) {
            try {
                c.close();
            } catch (java.sql.SQLException ignored) {
                // already closed
            }
        }
        held.clear();
    }

    private record Row(String state, Integer responseStatus) {
    }

    private Row ledgerRow(String key) throws Exception {
        Row[] out = new Row[1];
        su(ctx -> {
            var r = ctx.select(REQUEST_LEDGER.STATE, REQUEST_LEDGER.RESPONSE_STATUS).from(REQUEST_LEDGER)
                .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(UUID.fromString(key))))
                .fetchOne();
            out[0] = r == null ? null : new Row(r.value1(), r.value2());
        });
        return out[0];
    }

    private Row awaitRow(String key, java.util.function.Predicate<Row> done, Duration within) throws Exception {
        long deadline = System.nanoTime() + within.toNanos();
        Row row = ledgerRow(key);
        while (!done.test(row) && System.nanoTime() < deadline) {
            Thread.sleep(100);
            row = ledgerRow(key);
        }
        return row;
    }

    @Test
    void aFailureThatIsPoolExhaustion_sendsItsErrorPromptly_andQueuesNoMarkerOnTheExhaustedPool() throws Exception {
        var body = seedOrphans(3);
        String key = UUID.randomUUID().toString();
        failWithAnExhaustedPool(() -> new RuntimeException("injected",
            new java.sql.SQLTransientConnectionException("HikariPool - Connection is not available")));

        long start = System.nanoTime();
        HttpResponse<String> r = post(body, key);
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
        // PRIMARY PIN (nexus-yye8l.22): the waiter count. The skip is what keeps the failure marker from queueing
        // on the exhausted pool, and it is the only assertion here that fails when the skip is removed and the
        // marker is merely quick. The elapsed-time assertion below is SECONDARY: a marker bounded by
        // FAIL_MARKER_WAIT_MS would also answer inside CONNECTION_TIMEOUT_MS / 2, so it passes without the skip.
        var pool = svcDs.getHikariPoolMXBean();
        assertThat(pool.getActiveConnections()).as("the pool is still exhausted when the 503 is sent")
            .isEqualTo(POOL_SIZE);
        assertThat(pool.getThreadsAwaitingConnection())
            .as("the marker queued no waiter on the exhausted pool")
            .isZero();
        assertThat(elapsedMs).as("secondary: the 503 does not wait behind the exhausted pool (connection timeout %d ms)",
            CONNECTION_TIMEOUT_MS).isLessThan(CONNECTION_TIMEOUT_MS / 2);
        Row row = ledgerRow(key);
        assertThat(row.state()).as("no marker: the lease expiry covers the row").isEqualTo("in_flight");
        assertThat(row.responseStatus()).isNull();
    }

    @Test
    void anyOtherFailureMetByAnExhaustedPool_waitsForTheMarkerOnlyBriefly_thenTheMarkerLandsWhenThePoolFrees()
            throws Exception {
        var body = seedOrphans(3);
        String key = UUID.randomUUID().toString();
        failWithAnExhaustedPool(() -> new RuntimeException("injected opaque failure"));

        long start = System.nanoTime();
        HttpResponse<String> r = post(body, key);
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
        assertThat(elapsedMs).as("the 500 waits at most the marker budget, not the pool's connection timeout (%d ms)",
            CONNECTION_TIMEOUT_MS).isLessThan(CONNECTION_TIMEOUT_MS / 2);
        assertThat(ledgerRow(key).state()).as("the marker is still queued behind the pool").isEqualTo("in_flight");

        releasePoolNow();
        Row landed = awaitRow(key, row -> row != null && "failed".equals(row.state()), Duration.ofSeconds(20));
        assertThat(landed.state()).as("the deferred marker lands once a connection frees").isEqualTo("failed");
        assertThat(landed.responseStatus()).isEqualTo(500);
    }
}

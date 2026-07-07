// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.MigrationJobRepository;
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
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-176 Phase 4 (bead nexus-t9rmg.23 P4.T / .24 P4) / RDR-178 Gap 5 (bead
 * nexus-melvx) — cloud→cloud server-side ingest contract, sync AND async modes.
 *
 * <p>Assertions:
 * <ol>
 *   <li><b>Sync mode (back-compat, {@code "sync": true}).</b> Server-side copy,
 *       zero client vectors, parity signal ({@code copied}/{@code dest_counts});
 *       pagination across pages; mid-copy failure → 500 with partial progress;
 *       unknown source collection → 400. Byte-for-byte the original RDR-176 P4
 *       contract.</li>
 *   <li><b>Async mode (default).</b> {@code POST} returns {@code 202 {job_id}}
 *       immediately; {@code GET /v1/migration/jobs/{id}} polls to a terminal
 *       state with per-collection {@code copied}/{@code dest}/{@code expected}
 *       counts. A re-POST naming the same (tenant, collection-set) as an
 *       active job is idempotent: same job id, source opened only once.</li>
 *   <li><b>Ephemeral credentials (Pillar 1b + 2026-07-02 gate-critique BINDING
 *       jobs-table constraint).</b> The api key appears in NO log line, NOT the
 *       response body, NOT any persisted chunk metadata, and — new — the
 *       {@code migration_jobs} table schema itself carries NO credential-shaped
 *       column.</li>
 *   <li><b>Fail loud.</b> Missing api key → 400 synchronously (mode-agnostic,
 *       validated before the sync/async branch).</li>
 * </ol>
 *
 * <p>Integration over mocks: real Testcontainers pgvector + real
 * {@link PgVectorRepository} upsert + real {@link MigrationJobRepository}; only
 * the external ChromaCloud boundary is faked (via the
 * {@link MigrationHandler.CloudSourceFactory} seam).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MigrationHandlerIngestCloudTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String SVC_ROLE = "svc_migr_ingest_test";
    private static final String SVC_PASS = "svc_migr_ingest_test_pass";
    private static final String TENANT   = "migr-ingest-tenant";

    // 384-dim collections (minilm token) so the fake vectors stay small.
    private static final String COLL_A = "knowledge__migr__minilm-l6-v2-384__v1";
    private static final String COLL_B = "docs__migr__minilm-l6-v2-384__v1";
    private static final String COLL_BIG = "knowledge__migrbig__minilm-l6-v2-384__v1";

    // The sentinel credential we scan for across logs / response / persisted rows.
    private static final String SECRET_API_KEY = "ck-SENTINEL-super-secret-cloud-key-DO-NOT-LEAK";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository pgVectors;
    MigrationJobRepository jobRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_" + dim + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_documents, nexus.catalog_document_chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.migration_jobs TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        Embedder embedder = new NoopEmbedder(384);  // upsertChunksWithVectors skips it
        pgVectors = new PgVectorRepository(tenantScope, embedder, embedder);
        jobRepo = new MigrationJobRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @org.junit.jupiter.api.BeforeEach
    void clearChunks() throws SQLException {
        // PER_CLASS shares one DB across tests; isolate each test's parity/count
        // assertions by truncating the chunk + jobs tables (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute("TRUNCATE nexus.chunks_" + dim);
            }
            su.createStatement().execute("TRUNCATE nexus.migration_jobs");
        }
    }

    // ── sync mode (back-compat, "sync": true) ───────────────────────────────────

    @Test
    void ingestCloud_copiesServerSide_reportsParity_andNeverLeaksCredentials() throws Exception {
        var openedWith = new AtomicReference<String[]>();
        FakeCloud fake = new FakeCloud(openedWith);
        fake.put(COLL_A, chash("a", 1), "alpha one");
        fake.put(COLL_A, chash("a", 2), "alpha two");
        fake.put(COLL_B, chash("b", 1), "bravo one");

        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_A, COLL_B)));
            handleWithTenant(new MigrationHandler(pgVectors, fake, jobRepo), ex);

            assertThat(ex.status).as("ingest-cloud returns 200").isEqualTo(200);
            @SuppressWarnings("unchecked")
            Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
            assertThat(resp.get("total")).isEqualTo(3);
            @SuppressWarnings("unchecked")
            Map<String, Object> copied = (Map<String, Object>) resp.get("copied");
            assertThat(copied).containsEntry(COLL_A, 2).containsEntry(COLL_B, 1);
            @SuppressWarnings("unchecked")
            Map<String, Object> destCounts = (Map<String, Object>) resp.get("dest_counts");
            assertThat(destCounts).as("parity: dest_counts == copied")
                .containsEntry(COLL_A, 2).containsEntry(COLL_B, 1);

            // Vectors actually landed server-side (client sent none).
            assertThat(superuserCount(384, COLL_A)).isEqualTo(2L);
            assertThat(superuserCount(384, COLL_B)).isEqualTo(1L);

            // The handler used the client's ephemeral creds (in-memory only).
            assertThat(openedWith.get()).containsExactly("src-tenant", "src-db", SECRET_API_KEY);

            // The api key leaked NOWHERE: response, logs, persisted metadata.
            assertNoKeyLeak(ex, logs);
        } finally {
            root.detachAppender(logs);
        }
    }

    @Test
    void ingestCloud_paginatesLargeCollection_withAdvancingOffset() throws Exception {
        int count = MigrationHandler.PAGE + 1;  // 301 → two pages (300 then 1)
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        for (int i = 0; i < count; i++) fake.put(COLL_BIG, chash("big", i), "doc " + i);

        CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_BIG)));
        handleWithTenant(new MigrationHandler(pgVectors, fake, jobRepo), ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(superuserCount(384, COLL_BIG))
            .as("all 301 chunks landed across two pages").isEqualTo((long) count);
        assertThat(fake.offsetsFor(COLL_BIG))
            .as("offset advanced by PAGE across pages").containsExactly(0, MigrationHandler.PAGE);
    }

    @Test
    void ingestCloud_midCopyFailure_returns500_withPartialProgress_noLeak() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("fa", 1), "ok one");
        fake.put(COLL_B, chash("fb", 1), "never read");  // present in source ...
        fake.failReadOn(COLL_B);                          // ... but its read throws mid-copy

        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_A, COLL_B)));
            handleWithTenant(new MigrationHandler(pgVectors, fake, jobRepo), ex);

            assertThat(ex.status).as("mid-copy failure → 500").isEqualTo(500);
            @SuppressWarnings("unchecked")
            Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
            @SuppressWarnings("unchecked")
            Map<String, Object> partial = (Map<String, Object>) resp.get("copied_before_failure");
            assertThat(partial).as("only the collection that landed before the failure is reported")
                .containsExactly(Map.entry(COLL_A, 1));
            // Even on the failure path the api key must not leak.
            assertNoKeyLeak(ex, logs);
        } finally {
            root.detachAppender(logs);
        }
    }

    @Test
    void ingestCloud_unknownCollection_returns400() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("u", 1), "present");
        CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of("knowledge__typo__minilm-l6-v2-384__v1")));
        handleWithTenant(new MigrationHandler(pgVectors, fake, jobRepo), ex);
        assertThat(ex.status).as("a requested collection absent from source → 400").isEqualTo(400);
        assertThat(ex.bodyString()).contains("not present in source");
    }

    @Test
    void ingestCloud_missingApiKey_returns400() throws Exception {
        MigrationHandler handler = new MigrationHandler(pgVectors,
            (t, d, k) -> { throw new AssertionError("source must not be opened on a bad request"); },
            jobRepo);
        String body = MAPPER.writeValueAsString(Map.of(
            "source_tenant", "src-tenant", "source_database", "src-db"));  // no api key
        CapturingExchange ex = post(body);
        handleWithTenant(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("source_api_key");
    }

    // ── async mode (default) ─────────────────────────────────────────────────────

    @Test
    void ingestCloudAsync_returns202_andJobCompletesWithCounts() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("async", 1), "alpha one");
        fake.put(COLL_A, chash("async", 2), "alpha two");
        MigrationHandler handler = new MigrationHandler(pgVectors, fake, jobRepo);

        CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_A), false));
        handleWithTenant(handler, ex);

        assertThat(ex.status).as("async ingest-cloud returns 202").isEqualTo(202);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
        String jobId = (String) resp.get("job_id");
        assertThat(jobId).as("job_id present in 202 response").isNotBlank();

        Map<String, Object> job = pollJobUntilTerminal(handler, jobId, 5000);
        assertThat(job.get("state")).as("job completes").isEqualTo("done");
        assertThat(job.get("error")).isNull();
        assertThat(job.get("started_at")).isNotNull();
        assertThat(job.get("finished_at")).isNotNull();
        @SuppressWarnings("unchecked")
        Map<String, Object> perCollection = (Map<String, Object>) job.get("per_collection");
        @SuppressWarnings("unchecked")
        Map<String, Object> collA = (Map<String, Object>) perCollection.get(COLL_A);
        assertThat(collA.get("copied")).isEqualTo(2);
        assertThat(collA.get("dest")).isEqualTo(2);
        assertThat(collA.get("expected")).isEqualTo(2);

        // The copy actually landed server-side.
        assertThat(superuserCount(384, COLL_A)).isEqualTo(2L);
    }

    @Test
    void ingestCloudAsync_idempotentRePost_returnsSameJobId_opensSourceOnce() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("idem", 1), "one");
        fake.put(COLL_B, chash("idem", 2), "two");
        CountDownLatch gate = new CountDownLatch(1);
        fake.pauseCollectionsCallUntil(gate);  // keeps job1 "running" for the test window
        MigrationHandler handler = new MigrationHandler(pgVectors, fake, jobRepo);

        CapturingExchange ex1 = post(reqBody(SECRET_API_KEY, List.of(COLL_A, COLL_B), false));
        handleWithTenant(handler, ex1);
        assertThat(ex1.status).isEqualTo(202);
        @SuppressWarnings("unchecked")
        String jobId1 = (String) MAPPER.readValue(ex1.bodyString(), Map.class).get("job_id");

        // Second POST names the SAME collection set in a DIFFERENT order — must
        // dedupe on the order-independent canonical key while job1 is still active.
        CapturingExchange ex2 = post(reqBody(SECRET_API_KEY, List.of(COLL_B, COLL_A), false));
        handleWithTenant(handler, ex2);
        assertThat(ex2.status).isEqualTo(202);
        @SuppressWarnings("unchecked")
        String jobId2 = (String) MAPPER.readValue(ex2.bodyString(), Map.class).get("job_id");

        assertThat(jobId2).as("idempotent re-POST returns the SAME job id").isEqualTo(jobId1);

        gate.countDown();
        Map<String, Object> job = pollJobUntilTerminal(handler, jobId1, 5000);
        assertThat(job.get("state")).isEqualTo("done");

        // Only ONE copy actually ran: the fake source was opened exactly once.
        assertThat(fake.openCount()).as("idempotent re-POST must not trigger a second copy").isEqualTo(1);
    }

    @Test
    void ingestCloudAsync_missingCollections_returns400_synchronously() throws Exception {
        MigrationHandler handler = new MigrationHandler(pgVectors,
            (t, d, k) -> { throw new AssertionError("source must not be opened when collections is empty"); },
            jobRepo);
        String body = MAPPER.writeValueAsString(Map.of(
            "source_tenant", "src-tenant", "source_database", "src-db", "source_api_key", SECRET_API_KEY));
        CapturingExchange ex = post(body);
        handleWithTenant(handler, ex);
        assertThat(ex.status).as("async mode requires a non-empty collections list").isEqualTo(400);
    }

    @Test
    void getJob_unknownId_returns404() throws Exception {
        MigrationHandler handler = new MigrationHandler(pgVectors,
            (t, d, k) -> { throw new AssertionError("source must not be opened for a GET"); },
            jobRepo);
        CapturingExchange ex = get("/v1/migration/jobs/does-not-exist");
        handleWithTenant(handler, ex);
        assertThat(ex.status).isEqualTo(404);
    }

    // ── credential non-persistence schema gate (2026-07-02 gate-critique BINDING) ─

    @Test
    void migrationJobsTable_hasNoCredentialColumns() throws SQLException {
        List<String> columns = new ArrayList<>();
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT column_name FROM information_schema.columns "
                 + "WHERE table_schema = 'nexus' AND table_name = 'migration_jobs'")) {
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) columns.add(rs.getString(1).toLowerCase(Locale.ROOT));
            }
        }
        assertThat(columns).as("migration_jobs table exists with columns").isNotEmpty();

        // Extends RDR-176's test-enforced credential non-persistence decision to the
        // jobs table (bead nexus-melvx, 2026-07-02 gate-critique BINDING constraint).
        List<String> credentialShapedTokens = List.of(
            "api_key", "apikey", "credential", "secret", "password", "passwd",
            "token", "source_tenant", "source_database");
        for (String col : columns) {
            for (String bad : credentialShapedTokens) {
                assertThat(col)
                    .as("migration_jobs column '%s' must not be credential-shaped (matched '%s')", col, bad)
                    .doesNotContain(bad);
            }
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Collision-free 32-char chash id: 4-char tag field + 28-digit index. */
    private static String chash(String tag, int i) {
        String t = (tag + "xxxx").substring(0, 4);
        return t + String.format("%028d", i);  // exactly 32 chars, unique per i
    }

    /** Legacy 2-arg form: defaults sync=true, preserving every pre-nexus-melvx test's contract. */
    private static String reqBody(String apiKey, List<String> collections) throws Exception {
        return reqBody(apiKey, collections, true);
    }

    private static String reqBody(String apiKey, List<String> collections, boolean sync) throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("source_tenant", "src-tenant");
        m.put("source_database", "src-db");
        m.put("source_api_key", apiKey);
        m.put("collections", collections);
        if (sync) m.put("sync", true);
        return MAPPER.writeValueAsString(m);
    }

    private static CapturingExchange post(String body) {
        return new CapturingExchange("POST", URI.create("/v1/migration/ingest-cloud"), body);
    }

    private static CapturingExchange get(String path) {
        return new CapturingExchange("GET", URI.create(path), "");
    }

    private void handleWithTenant(MigrationHandler handler, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    /** Poll {@code GET /v1/migration/jobs/{id}} until state is done/failed or the timeout elapses. */
    private Map<String, Object> pollJobUntilTerminal(MigrationHandler handler, String jobId, long timeoutMs)
            throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            CapturingExchange ex = get("/v1/migration/jobs/" + jobId);
            handleWithTenant(handler, ex);
            assertThat(ex.status).as("GET job status").isEqualTo(200);
            @SuppressWarnings("unchecked")
            Map<String, Object> job = MAPPER.readValue(ex.bodyString(), Map.class);
            String state = (String) job.get("state");
            if ("done".equals(state) || "failed".equals(state)) {
                return job;
            }
            Thread.sleep(20);
        }
        throw new AssertionError("job " + jobId + " did not reach a terminal state within " + timeoutMs + "ms");
    }

    private void assertNoKeyLeak(CapturingExchange ex, ListAppender<ILoggingEvent> logs) throws SQLException {
        assertThat(ex.bodyString()).as("api key not in response body").doesNotContain(SECRET_API_KEY);
        for (ILoggingEvent e : logs.list) {
            assertThat(e.getFormattedMessage()).as("api key not in any log line").doesNotContain(SECRET_API_KEY);
        }
        assertThat(allChunkMetadataText(384))
            .as("api key not persisted in any chunk metadata").doesNotContain(SECRET_API_KEY);
    }

    private long superuserCount(int dim, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_" + dim + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private String allChunkMetadataText(int dim) throws SQLException {
        StringBuilder sb = new StringBuilder();
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement("SELECT metadata::text FROM nexus.chunks_" + dim);
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) sb.append(rs.getString(1)).append('\n');
        }
        return sb.toString();
    }

    /** Fake in-memory cloud source: records opened creds + read offsets; can fail or block. */
    private static final class FakeCloud implements MigrationHandler.CloudSourceFactory {
        private final Map<String, List<String>> ids = new LinkedHashMap<>();
        private final Map<String, List<String>> docs = new LinkedHashMap<>();
        private final Map<String, List<float[]>> vecs = new LinkedHashMap<>();
        private final Map<String, List<Integer>> offsets = new LinkedHashMap<>();
        private String failReadOn;
        private volatile CountDownLatch pauseLatch;
        private final AtomicInteger opens = new AtomicInteger();
        private final AtomicReference<String[]> openedWith;

        FakeCloud(AtomicReference<String[]> openedWith) { this.openedWith = openedWith; }

        void put(String coll, String id, String doc) {
            float[] v = new float[384];
            v[0] = 1.0f;
            ids.computeIfAbsent(coll, k -> new ArrayList<>()).add(id);
            docs.computeIfAbsent(coll, k -> new ArrayList<>()).add(doc);
            vecs.computeIfAbsent(coll, k -> new ArrayList<>()).add(v);
        }

        void failReadOn(String coll) { this.failReadOn = coll; }

        /** The first (and, under correct idempotency, only) {@code collections()} call blocks on {@code latch}. */
        void pauseCollectionsCallUntil(CountDownLatch latch) { this.pauseLatch = latch; }

        int openCount() { return opens.get(); }

        List<Integer> offsetsFor(String coll) { return offsets.getOrDefault(coll, List.of()); }

        @Override
        public MigrationHandler.CloudSource open(String tenant, String database, String apiKey) {
            opens.incrementAndGet();
            openedWith.set(new String[] {tenant, database, apiKey});
            return new MigrationHandler.CloudSource() {
                @Override
                public List<String> collections() {
                    CountDownLatch latch = pauseLatch;
                    if (latch != null) {
                        try {
                            latch.await(5, TimeUnit.SECONDS);
                        } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                        }
                    }
                    return new ArrayList<>(ids.keySet());
                }

                @Override
                public MigrationHandler.ChunkPage read(String coll, int limit, int offset) {
                    offsets.computeIfAbsent(coll, k -> new ArrayList<>()).add(offset);
                    if (coll.equals(failReadOn)) {
                        // Message intentionally embeds a fake secret-shaped token to
                        // prove the handler does not echo exception messages.
                        throw new RuntimeException("chroma read failed body={token:" + SECRET_API_KEY + "}");
                    }
                    List<String> cids = ids.getOrDefault(coll, List.of());
                    if (offset >= cids.size()) return MigrationHandler.ChunkPage.empty();
                    int end = Math.min(offset + limit, cids.size());
                    List<Map<String, Object>> metas = new ArrayList<>();
                    for (int i = offset; i < end; i++) metas.add(Map.of("k", "v"));
                    return new MigrationHandler.ChunkPage(
                        cids.subList(offset, end), docs.get(coll).subList(offset, end),
                        vecs.get(coll).subList(offset, end), metas);
                }
            };
        }
    }

    /** Never actually invoked (vectors are supplied); satisfies the ctor. */
    private static final class NoopEmbedder implements Embedder {
        private final int dim;
        NoopEmbedder(int dim) { this.dim = dim; }
        @Override public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (int i = 0; i < texts.size(); i++) out.add(new float[dim]);
            return out;
        }
        @Override public void close() {}
    }

    /** Minimal {@link HttpExchange} capturing response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}

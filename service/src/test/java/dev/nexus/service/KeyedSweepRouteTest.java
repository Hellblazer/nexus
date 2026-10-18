// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.RequestLedgerActivity;
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
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.5): keyed handling on the three bounded sweep forms, over real HTTP
 * through the RLS-subject service role against real Postgres.
 *
 * <p>The three keyed shapes are {@code quarantine-orphans} with {@code row_limit} and no floor
 * ({@link Form#BOUNDED}), the same with {@code floor_fraction} ({@link Form#FLOORED}), and
 * {@code restore-rereferenced} with {@code row_limit} ({@link Form#RESTORE}). A sweep is held open with
 * {@link PgVectorRepository#setAfterSweepSqlHookForTests}, which blocks inside the sweep's transaction after
 * the SQL function returned, so a second request and the status route can be sent while the ledger row is
 * {@code in_flight}.
 *
 * <p>{@link RequestLedgerActivity} counters are process-wide statics, so every assertion on them is a delta.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class KeyedSweepRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-keyedsweep-tenant-0123456789abcdef00";
    private static final String SVC_ROLE = "svc_keyedsweep_route";
    private static final String SVC_PASS = "svc_keyedsweep_route_pass";
    private static final String TENANT = "keyedsweep-route";
    private static final String STAMP = "2026-10-18T00:00:00Z";
    private static final String QUARANTINE_PATH = "/v1/vectors/gc/quarantine-orphans";
    private static final String RESTORE_PATH = "/v1/vectors/gc/restore-rereferenced";

    /** The three keyed shapes. */
    enum Form {
        BOUNDED(QUARANTINE_PATH, "moved"),
        FLOORED(QUARANTINE_PATH, "moved"),
        RESTORE(RESTORE_PATH, "restored");

        final String path;
        final String movedKey;

        Form(String path, String movedKey) {
            this.path = path;
            this.movedKey = movedKey;
        }

        boolean quarantine() {
            return this != RESTORE;
        }
    }

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private PgVectorRepository repo;
    private CatalogRepository catalog;
    private NexusService service;
    private HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();
    private final AtomicInteger sqlCalls = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "keyedsweep");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        FakeEmbedder embedder = new FakeEmbedder(384);
        var scope = new TenantScope(svcDs);
        repo = new PgVectorRepository(scope, embedder, embedder);
        catalog = new CatalogRepository(scope);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
        // The first authenticated request of a tenant runs the AuthFilter's per-tenant boot work; take it here.
        http.send(TestHttp.request(url("/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void clearHook() {
        repo.setAfterSweepSqlHookForTests(null);
    }

    // ── http ────────────────────────────────────────────────────────────────

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private HttpResponse<String> post(String path, Map<String, Object> body, String key) throws Exception {
        var b = TestHttp.request(url(path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json");
        if (key != null) b.header("Idempotency-Key", key);
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path, String key) throws Exception {
        var b = TestHttp.request(url(path)).header("Authorization", "Bearer " + TOKEN);
        if (key != null) b.header("Idempotency-Key", key);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode status(String key) throws Exception {
        HttpResponse<String> r = get("/v1/requests/" + key, null);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private static String header(HttpResponse<?> r, String name) {
        return r.headers().firstValue(name).orElse(null);
    }

    private JsonNode json(HttpResponse<String> r, int expectedStatus) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(expectedStatus);
        return MAPPER.readTree(r.body());
    }

    private static String newKey() {
        return UUID.randomUUID().toString();
    }

    // ── fixtures (substrate SQL) ─────────────────────────────────────────────

    /** A fixture: the collection a sweep reads from ({@code source}) and the one it moves rows into ({@code target}). */
    private record Fx(Form form, String origin, String quarantine, int total) {
        String source() { return form.quarantine() ? origin : quarantine; }
        String target() { return form.quarantine() ? quarantine : origin; }
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private Fx fixture(Form form, int n) throws Exception {
        return form.quarantine() ? seedOrphans(form, n, "t") : seedRestore(form, n);
    }

    /** {@code n} chunks aged past the grace with no manifest row: every one is a quarantine victim. */
    private Fx seedOrphans(Form form, int n, String titlePad) throws Exception {
        String origin = "knowledge__ks" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        List<String> hashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hashes.add(Chash.ofText(origin + "/" + i).toHex());
            texts.add("chunk " + i);
            vecs.add(new float[384]);
            metas.add(Map.of("title", titlePad + i));
        }
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, origin);
            PgContainerHelper.insertChunks(ctx, TENANT, origin, hashes, texts, vecs, metas);
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(ReapableFixtures.PAST_GRACE))
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(origin))).execute();
        });
        return new Fx(form, origin, "quarantine-" + origin, n);
    }

    /** {@code n} re-referenced chashes: real rows in the quarantine collection, a stub plus a manifest row in the origin. */
    private Fx seedRestore(Form form, int n) throws Exception {
        int id = seq.incrementAndGet();
        String origin = "code__ksr" + id + "__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-" + origin;
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, origin);
            PgContainerHelper.insertCollection(ctx, TENANT, quarantine);
        });
        for (int i = 0; i < n; i++) {
            String chash = Chash.ofText("ks-restore-" + id + "-" + i).toHex();
            repo.upsertChunks(TENANT, quarantine, List.of(chash), List.of("real-" + chash), List.of(Map.of()));
            repo.upsertChunks(TENANT, origin, List.of(chash), List.of("stub-" + chash), List.of(Map.of()));
            String docId = "ks-doc-" + id + "-" + i;
            catalog.upsertDocument(TENANT, Map.of(
                "tumbler", docId, "title", "ks-" + id + "-" + i, "content_type", "code", "corpus", "code",
                "physical_collection", origin, "chunk_count", 0));
            su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                .values(TENANT, docId, 0, Chash.fromHex(chash).toBytes(), origin,
                    PgContainerHelper.collectionModel(ctx, TENANT, origin))
                .execute());
        }
        return new Fx(form, origin, quarantine, n);
    }

    /** Rows the sweeps have taken out of the source (a restore's origin already holds a stub per chash). */
    private int movedRows(Fx f) throws Exception {
        return f.total() - count(f.source());
    }

    private int count(String collection) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))));
        return n[0];
    }

    private int ledgerRows(String key) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.fetchCount(REQUEST_LEDGER, REQUEST_LEDGER.TENANT_ID.eq(TENANT)
            .and(REQUEST_LEDGER.REQUEST_ID.eq(UUID.fromString(key)))));
        return n[0];
    }

    private static Map<String, Object> body(Fx f, int rowLimit) {
        var m = new LinkedHashMap<String, Object>();
        if (f.form() == Form.RESTORE) {
            m.put("quarantine_collection", f.quarantine());
            m.put("origin_collection", f.origin());
            m.put("row_limit", rowLimit);
            return m;
        }
        m.put("collection", f.origin());
        m.put("quarantine_collection", f.quarantine());
        m.put("quarantined_at", STAMP);
        m.put("sample_limit", 20);
        m.put("row_limit", rowLimit);
        if (f.form() == Form.FLOORED) {
            // A fraction of 1.0 is never exceeded (the judgement refuses only on MORE than the fraction), so the
            // floored form takes the move path while still carrying a floor in its response.
            m.put("floor_fraction", 1.0);
            m.put("floor_min_chunks", 1);
        }
        return m;
    }

    private HttpResponse<String> sweep(Fx f, int rowLimit, String key) throws Exception {
        return post(f.form().path, body(f, rowLimit), key);
    }

    /** Hold the next sweep open after its SQL; {@code entered} fires when it is held, {@code release} lets it go. */
    private void holdFirstSweep(CountDownLatch entered, CountDownLatch release) {
        sqlCalls.set(0);
        var first = new AtomicBoolean(true);
        repo.setAfterSweepSqlHookForTests(() -> {
            sqlCalls.incrementAndGet();
            if (first.compareAndSet(true, false)) {
                entered.countDown();
                try {
                    if (!release.await(60, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("test never released the held sweep");
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        });
    }

    // ── held sweep: in flight, then committed ────────────────────────────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void twoConcurrentSameKeyCalls_oneWins200_theOtherGets409InFlight(Form form) throws Exception {
        Fx f = fixture(form, 4);
        String key = newKey();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        holdFirstSweep(entered, release);
        var before = RequestLedgerActivity.snapshot();

        CompletableFuture<HttpResponse<String>> first = CompletableFuture.supplyAsync(() -> {
            try {
                return sweep(f, 2, key);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(entered.await(60, TimeUnit.SECONDS)).as("the first sweep is held after its SQL").isTrue();

        HttpResponse<String> second = sweep(f, 2, key);
        JsonNode busy = json(second, 409);
        assertThat(busy.get("error").asText()).isEqualTo("request_in_flight");
        assertThat(busy.get("request_id").asText()).isEqualTo(key);
        assertThat(busy.get("route").asText()).isEqualTo(form.path);
        OffsetDateTime leaseUntil = OffsetDateTime.parse(busy.get("lease_until").asText());
        assertThat(OffsetDateTime.parse(busy.get("started_at").asText())).isBefore(leaseUntil);
        assertThat(Long.parseLong(header(second, "Retry-After")))
            .as("whole seconds until the lease ends, never below 1").isGreaterThanOrEqualTo(1);
        assertThat(header(second, "Idempotency-Key")).as("a recorded request echoes its key").isEqualTo(key);
        assertThat(RequestLedgerActivity.snapshot().inFlight() - before.inFlight()).isEqualTo(1);

        release.countDown();
        HttpResponse<String> winner = first.get(60, TimeUnit.SECONDS);
        JsonNode won = json(winner, 200);
        assertThat(won.get(form.movedKey).asInt()).isEqualTo(2);
        assertThat(header(winner, "Idempotency-Key")).isEqualTo(key);
        assertThat(header(winner, "Idempotency-Replayed")).as("the first response is not a replay").isNull();
        assertThat(count(f.source())).as("exactly one execution").isEqualTo(2);
        assertThat(sqlCalls.get()).as("the loser never reached the SQL function").isEqualTo(1);
    }

    @ParameterizedTest
    @EnumSource(Form.class)
    void aStatusReadDuringAHeldSweepSaysInFlight_afterCommitItSaysCommittedWithTheTrueMoved(Form form)
            throws Exception {
        Fx f = fixture(form, 5);
        String key = newKey();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        holdFirstSweep(entered, release);

        CompletableFuture<HttpResponse<String>> call = CompletableFuture.supplyAsync(() -> {
            try {
                return sweep(f, 3, key);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(entered.await(60, TimeUnit.SECONDS)).isTrue();

        JsonNode held = status(key);
        assertThat(held.get("state").asText()).as("the sweep SQL ran but nothing committed").isEqualTo("in_flight");
        assertThat(movedRows(f)).as("uncommitted rows are invisible").isZero();

        release.countDown();
        JsonNode live = json(call.get(60, TimeUnit.SECONDS), 200);
        JsonNode done = status(key);
        assertThat(done.get("state").asText()).isEqualTo("committed");
        assertThat(done.get("response_status").asInt()).isEqualTo(200);
        assertThat(done.get("response").get(form.movedKey).asInt())
            .as("the stored moved is what the sweep really moved").isEqualTo(3).isEqualTo(movedRows(f));
        assertThat(done.get("response")).isEqualTo(live);
    }

    // ── replay ───────────────────────────────────────────────────────────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void aReplayReturnsTheFirstBody_andTheSqlFunctionIsNotInvokedAgain(Form form) throws Exception {
        Fx f = fixture(form, 5);
        String key = newKey();
        repo.setAfterSweepSqlHookForTests(sqlCalls::incrementAndGet);
        sqlCalls.set(0);
        var before = RequestLedgerActivity.snapshot();

        HttpResponse<String> first = sweep(f, 2, key);
        JsonNode firstBody = json(first, 200);
        assertThat(sqlCalls.get()).isEqualTo(1);
        assertThat(firstBody.get("row_limit").asInt()).isEqualTo(2);
        assertThat(firstBody.get("remaining").asInt()).isEqualTo(3);
        if (form == Form.FLOORED) {
            assertThat(firstBody.get("floor").get("given").asBoolean()).isTrue();
            assertThat(firstBody.get("refused").asBoolean()).isFalse();
        }
        int sourceAfterFirst = count(f.source());

        HttpResponse<String> replay = sweep(f, 2, key);

        assertThat(json(replay, 200)).as("the stored response, field for field, floor and remaining included")
            .isEqualTo(firstBody);
        assertThat(header(replay, "Idempotency-Replayed")).isEqualTo("true");
        assertThat(header(replay, "Idempotency-Key")).isEqualTo(key);
        assertThat(sqlCalls.get()).as("the replay did not run the SQL function").isEqualTo(1);
        assertThat(count(f.source())).as("and moved nothing more").isEqualTo(sourceAfterFirst);
        assertThat(RequestLedgerActivity.snapshot().replays() - before.replays()).isEqualTo(1);
    }

    @Test
    void aSweepBodyOver64KbIsStoredWithATruncatedSample_theLiveResponseIsUntouched() throws Exception {
        String pad = "x".repeat(400);
        Fx f = seedOrphans(Form.BOUNDED, 300, pad);
        String key = newKey();
        var req = body(f, 300);
        req.put("sample_limit", 300);

        HttpResponse<String> first = post(f.form().path, req, key);
        JsonNode live = json(first, 200);
        assertThat(live.get("moved").asInt()).isEqualTo(300);
        assertThat(live.get("sample")).as("the first caller sees every sample row").hasSize(300);
        assertThat(live.has("sample_truncated")).isFalse();
        assertThat(first.body().getBytes(StandardCharsets.UTF_8).length)
            .as("guard: the body really exceeds the 64 KB cap").isGreaterThan(65_536);

        HttpResponse<String> replay = post(f.form().path, req, key);
        JsonNode stored = json(replay, 200);
        assertThat(header(replay, "Idempotency-Replayed")).isEqualTo("true");
        assertThat(stored.get("sample_truncated").asBoolean()).isTrue();
        assertThat(stored.get("sample").size()).isBetween(1, 299);
        assertThat(stored.get("moved").asInt()).as("only the sample is cut").isEqualTo(300);
        assertThat(stored.get("remaining").asInt()).isZero();
        assertThat(stored.get("row_limit").asInt()).isEqualTo(300);
        assertThat(MAPPER.writeValueAsBytes(stored).length).isLessThanOrEqualTo(65_536);
        assertThat(status(key).get("response")).as("the status route reads the same stored copy").isEqualTo(stored);
    }

    // ── sweep_gate_busy: the two quarantine forms ────────────────────────────

    private Connection holdGate(Fx f) throws Exception {
        Connection holder = pg.createConnection("");
        holder.setAutoCommit(false);
        DSL.using(holder, SQLDialect.POSTGRES)
           .select(DSL.function("pg_advisory_xact_lock", Object.class,
                   DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + f.origin()))))
           .fetch();
        return holder;
    }

    @ParameterizedTest
    @EnumSource(value = Form.class, names = {"BOUNDED", "FLOORED"})
    void aGateWaitThatTimesOutAnswers503SweepGateBusy_marksItsRowFailed_andATakeoverMeetingTheHeldGateAnswersTheSame(
            Form form) throws Exception {
        Fx f = fixture(form, 3);
        String key = newKey();
        var before = RequestLedgerActivity.snapshot();
        Connection holder = holdGate(f);
        try {
            HttpResponse<String> busy = sweep(f, 2, key);
            JsonNode b = json(busy, 503);
            assertThat(b.get("error").asText()).isEqualTo("sweep_gate_busy");
            assertThat(Long.parseLong(header(busy, "Retry-After"))).isGreaterThanOrEqualTo(1);
            assertThat(header(busy, "Idempotency-Key")).isEqualTo(key);
            assertThat(status(key).get("state").asText()).as("its own row, marked failed").isEqualTo("rolled_back");
            assertThat(count(f.source())).as("nothing moved").isEqualTo(3);

            HttpResponse<String> again = sweep(f, 2, key);
            assertThat(json(again, 503).get("error").asText())
                .as("a same-key takeover that meets the gate still held").isEqualTo("sweep_gate_busy");
            assertThat(RequestLedgerActivity.snapshot().takeovers() - before.takeovers()).isEqualTo(1);
        } finally {
            holder.rollback();
            holder.close();
        }
        HttpResponse<String> ok = sweep(f, 2, key);
        assertThat(json(ok, 200).get("moved").asInt()).as("the gate released, the same key now runs").isEqualTo(2);
        assertThat(status(key).get("state").asText()).isEqualTo("committed");
    }

    @ParameterizedTest
    @EnumSource(value = Form.class, names = {"BOUNDED", "FLOORED"})
    void aRowLockTimeoutKeepsTodaysError_neverSweepGateBusy(Form form) throws Exception {
        Fx f = fixture(form, 3);
        String key = newKey();
        Connection holder = pg.createConnection("");
        holder.setAutoCommit(false);
        try {
            DSL.using(holder, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.METADATA, CHUNKS.METADATA)
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(f.origin()))).execute();

            HttpResponse<String> unkeyed = sweep(f, 2, null);
            HttpResponse<String> keyed = sweep(f, 2, key);

            assertThat(keyed.body()).doesNotContain("sweep_gate_busy");
            assertThat(keyed.statusCode()).as("same status as the keyless call: %s", unkeyed.body())
                .isEqualTo(unkeyed.statusCode());
            assertThat(keyed.body()).isEqualTo(unkeyed.body());
            assertThat(keyed.statusCode()).isNotEqualTo(200);
            assertThat(status(key).get("state").asText()).as("the failure marker ran").isEqualTo("rolled_back");
        } finally {
            holder.rollback();
            holder.close();
        }
    }

    @Test
    void restoreNeverTakesTheSweepGate_aHeldGateDoesNotBlockIt() throws Exception {
        Fx f = fixture(Form.RESTORE, 3);
        Connection holder = holdGate(f);
        try {
            HttpResponse<String> r = sweep(f, 2, newKey());
            assertThat(json(r, 200).get("restored").asInt()).isEqualTo(2);
        } finally {
            holder.rollback();
            holder.close();
        }
    }

    // ── refusals: nothing runs, no ledger row ────────────────────────────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void aMalformedKeyAnswers400_nothingRuns_noLedgerRow(Form form) throws Exception {
        Fx f = fixture(form, 3);
        int[] rowsBefore = new int[1];
        su(ctx -> rowsBefore[0] = ctx.fetchCount(REQUEST_LEDGER, REQUEST_LEDGER.TENANT_ID.eq(TENANT)));
        for (String bad : new String[] {"not-a-uuid", "1-1-1-1-1", " ", UUID.randomUUID().toString() + "x",
                                        UUID.randomUUID().toString().replace("-", "")}) {
            HttpResponse<String> r = sweep(f, 2, bad);
            assertThat(json(r, 400).get("error").asText()).as("key=%s", bad).isEqualTo("idempotency_key_malformed");
        }
        assertThat(count(f.source())).as("no sweep ran").isEqualTo(3);
        su(ctx -> assertThat(ctx.fetchCount(REQUEST_LEDGER, REQUEST_LEDGER.TENANT_ID.eq(TENANT)))
            .as("and no ledger row exists for any of them").isEqualTo(rowsBefore[0]));
    }

    @Test
    void aKeyOnAnyOtherShapeAnswers400Unsupported_nothingRuns_noLedgerRow() throws Exception {
        Fx orphans = seedOrphans(Form.BOUNDED, 3, "t");
        Fx restore = seedRestore(Form.RESTORE, 3);

        var unbounded = body(orphans, 2);
        unbounded.remove("row_limit");
        var floorOnly = body(orphans, 2);
        floorOnly.remove("row_limit");
        floorOnly.put("floor_fraction", 0.5);
        var restoreUnbounded = body(restore, 2);
        restoreUnbounded.remove("row_limit");
        var expire = new LinkedHashMap<String, Object>();
        expire.put("quarantine_collection", orphans.quarantine());
        expire.put("origin_collection", orphans.origin());
        expire.put("cutoff", "2099-01-01T00:00:00Z");
        var quarantineRestore = new LinkedHashMap<String, Object>();
        quarantineRestore.put("collection", orphans.origin());
        quarantineRestore.put("chashes", List.of(Chash.ofText("anything").toHex()));

        Map<String, Map<String, Object>> shapes = new LinkedHashMap<>();
        shapes.put(QUARANTINE_PATH + "#unbounded", unbounded);
        shapes.put(QUARANTINE_PATH + "#floor-only", floorOnly);
        shapes.put(RESTORE_PATH + "#unbounded", restoreUnbounded);
        shapes.put("/v1/vectors/gc/expire-quarantine", expire);
        shapes.put("/v1/vectors/gc/quarantine-restore", quarantineRestore);
        shapes.put("/v1/vectors/count", Map.of("collection", orphans.origin()));
        shapes.put("/v1/memory/search", Map.of("query", "x", "project", "p"));

        for (var e : shapes.entrySet()) {
            String path = e.getKey().replaceFirst("#.*", "");
            String key = newKey();
            HttpResponse<String> r = post(path, e.getValue(), key);
            assertThat(json(r, 400).get("error").asText()).as(e.getKey()).isEqualTo("idempotency_key_unsupported");
            assertThat(ledgerRows(key)).as("no ledger row for %s", e.getKey()).isZero();
            assertThat(status(key).get("state").asText()).as(e.getKey()).isEqualTo("unknown");
        }
        HttpResponse<String> getWithKey = get("/v1/catalog/collections/list", newKey());
        assertThat(json(getWithKey, 400).get("error").asText()).isEqualTo("idempotency_key_unsupported");

        assertThat(count(orphans.origin())).as("no row moved").isEqualTo(3);
        assertThat(count(orphans.quarantine())).isZero();
        assertThat(count(restore.quarantine())).isEqualTo(3);
    }

    @ParameterizedTest
    @EnumSource(Form.class)
    void aReusedKeyWithADifferentBodyOrRouteAnswers422(Form form) throws Exception {
        Fx f = fixture(form, 4);
        String key = newKey();
        json(sweep(f, 2, key), 200);
        var before = RequestLedgerActivity.snapshot();

        HttpResponse<String> otherBody = sweep(f, 3, key);
        assertThat(json(otherBody, 422).get("error").asText()).isEqualTo("idempotency_key_reused");
        assertThat(header(otherBody, "Idempotency-Key")).isEqualTo(key);

        Fx other = form.quarantine() ? fixture(Form.RESTORE, 2) : fixture(Form.BOUNDED, 2);
        HttpResponse<String> otherRoute = sweep(other, 2, key);
        assertThat(json(otherRoute, 422).get("error").asText()).isEqualTo("idempotency_key_reused");
        assertThat(RequestLedgerActivity.snapshot().keyReused() - before.keyReused()).isEqualTo(2);
        assertThat(count(other.source())).as("the refused request ran nothing").isEqualTo(2);
    }

    // ── unkeyed: byte-identical ──────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Form.class)
    void withNoKeyTheBodyIsByteIdenticalToTheOldInlineBody_andCarriesNoLedgerHeaders(Form form) throws Exception {
        Fx f = fixture(form, 4);

        HttpResponse<String> r = sweep(f, 2, null);

        Map<String, Object> got = MAPPER.readValue(r.body(), Map.class);
        // The old inline builders, reproduced verbatim: Map.of for the bounded forms (a JVM-stable order),
        // an insertion-ordered map for the floored form.
        String expected;
        switch (form) {
            case BOUNDED -> expected = MAPPER.writeValueAsString(Map.of(
                "moved", got.get("moved"), "sample", got.get("sample"), "remaining", got.get("remaining"),
                "row_limit", 2, "floor", Map.of("given", false)));
            case FLOORED -> {
                var m = new LinkedHashMap<String, Object>();
                m.put("moved", got.get("moved"));
                m.put("sample", got.get("sample"));
                m.put("remaining", got.get("remaining"));
                m.put("row_limit", 2);
                m.put("refused", false);
                m.put("reapable_count", got.get("reapable_count"));
                m.put("total_count", got.get("total_count"));
                m.put("floor", Map.of("given", true, "fraction", 1.0, "min_chunks", 1, "force", false));
                expected = MAPPER.writeValueAsString(m);
            }
            default -> expected = MAPPER.writeValueAsString(Map.of(
                "restored", got.get("restored"), "remaining", got.get("remaining"), "row_limit", 2));
        }
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo(expected);
        assertThat(header(r, "Idempotency-Key")).isNull();
        assertThat(header(r, "Idempotency-Replayed")).isNull();
    }

    // ── the commit marker rolls the sweep back when the lease is gone ────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void aLostLedgerMarkerRollsTheSweepBack(Form form) throws Exception {
        Fx f = fixture(form, 3);
        String key = newKey();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        holdFirstSweep(entered, release);

        CompletableFuture<HttpResponse<String>> call = CompletableFuture.supplyAsync(() -> {
            try {
                return sweep(f, 2, key);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(entered.await(60, TimeUnit.SECONDS)).isTrue();
        su(ctx -> ctx.update(REQUEST_LEDGER).set(REQUEST_LEDGER.LEASE_UNTIL, OffsetDateTime.now().minusSeconds(5))
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(UUID.fromString(key))))
            .execute());
        release.countDown();

        HttpResponse<String> r = call.get(60, TimeUnit.SECONDS);
        assertThat(r.statusCode()).as("the sweep did not commit: %s", r.body()).isGreaterThanOrEqualTo(500);
        assertThat(count(f.source())).as("the mutation rolled back with the marker").isEqualTo(3);
        assertThat(movedRows(f)).isZero();
        assertThat(status(key).get("state").asText()).isEqualTo("rolled_back");
    }
}

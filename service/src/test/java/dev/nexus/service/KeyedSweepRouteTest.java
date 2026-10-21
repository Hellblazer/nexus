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
 * <p>The four keyed shapes are {@code quarantine-orphans} with {@code row_limit} and no floor
 * ({@link Form#BOUNDED}), the same with {@code floor_fraction} ({@link Form#FLOORED}),
 * {@code restore-rereferenced} with {@code row_limit} ({@link Form#RESTORE}) and, from RDR-222 Amendment A1
 * (nexus-1579m), {@code expire-quarantine} with {@code row_limit} ({@link Form#EXPIRE}). A sweep is held open with
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
    private static final String EXPIRE_PATH = "/v1/vectors/gc/expire-quarantine";
    private static final String EXPIRE_CUTOFF = "2099-01-01T00:00:00Z";

    /** The four keyed shapes (RDR-222 Amendment A1 added {@link #EXPIRE}, nexus-1579m). */
    enum Form {
        BOUNDED(QUARANTINE_PATH, "moved"),
        FLOORED(QUARANTINE_PATH, "moved"),
        RESTORE(RESTORE_PATH, "restored"),
        EXPIRE(EXPIRE_PATH, "expired");

        final String path;
        final String movedKey;

        Form(String path, String movedKey) {
            this.path = path;
            this.movedKey = movedKey;
        }

        /** The two quarantine-orphans shapes, which move rows out of the origin. */
        boolean quarantine() {
            return this == BOUNDED || this == FLOORED;
        }

        /** The sweep gate (503 sweep_gate_busy) applies to the quarantine shapes and to expire. */
        boolean takesTheGate() {
            return this != RESTORE;
        }

        /** The terminator field a replay carries: {@code remaining}, or {@code more} for expire. */
        String restField() {
            return this == EXPIRE ? "more" : "remaining";
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
        dev.nexus.service.http.HttpUtil.setSendObserverForTests(null);
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

    /** Whole seconds from {@code from} to {@code to}, rounded up. */
    private static long ceilSeconds(OffsetDateTime from, OffsetDateTime to) {
        long millis = java.time.Duration.between(from, to).toMillis();
        return Math.floorDiv(millis + 999L, 1000L);
    }

    // ── fixtures (substrate SQL) ─────────────────────────────────────────────

    /** A fixture: the collection a sweep reads from ({@code source}) and the one it moves rows into ({@code target}). */
    private record Fx(Form form, String origin, String quarantine, int total) {
        String source() { return form.quarantine() ? origin : quarantine; }
        String target() { return form.quarantine() ? quarantine : form == Form.EXPIRE ? null : origin; }
        /** The collection whose sweep gate the form takes: the origin's for quarantine, the sibling's for expire. */
        String gateKey() { return form == Form.EXPIRE ? quarantine : origin; }
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private Fx fixture(Form form, int n) throws Exception {
        return form.quarantine() ? seedOrphans(form, n, "t")
            : form == Form.EXPIRE ? seedExpire(form, n) : seedRestore(form, n);
    }

    /** {@code n} client-moved rows in the quarantine sibling, tagged for the origin and past any cutoff. */
    private Fx seedExpire(Form form, int n) throws Exception {
        int id = seq.incrementAndGet();
        String origin = "code__kse" + id + "__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-" + origin;
        List<String> hashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hashes.add(Chash.ofText(quarantine + "/" + i).toHex());
            texts.add("expiring " + i);
            vecs.add(new float[384]);
            metas.add(Map.of("quarantined_at", "2026-07-01T00:00:00Z", "origin_collection", origin));
        }
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, origin);
            PgContainerHelper.insertCollection(ctx, TENANT, quarantine);
            PgContainerHelper.insertChunks(ctx, TENANT, quarantine, hashes, texts, vecs, metas);
        });
        return new Fx(form, origin, quarantine, n);
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

    /** The {@code response_status} column of the ledger row, null when the row has none. */
    private Integer ledgerStatus(String key) throws Exception {
        Integer[] st = new Integer[1];
        su(ctx -> st[0] = ctx.select(REQUEST_LEDGER.RESPONSE_STATUS).from(REQUEST_LEDGER)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(UUID.fromString(key))))
            .fetchOne(REQUEST_LEDGER.RESPONSE_STATUS));
        return st[0];
    }

    private static Map<String, Object> body(Fx f, int rowLimit) {
        var m = new LinkedHashMap<String, Object>();
        if (f.form() == Form.EXPIRE) {
            m.put("quarantine_collection", f.quarantine());
            m.put("origin_collection", f.origin());
            m.put("cutoff", EXPIRE_CUTOFF);
            m.put("row_limit", rowLimit);
            return m;
        }
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

        OffsetDateTime beforeSecond = OffsetDateTime.now();
        HttpResponse<String> second = sweep(f, 2, key);
        OffsetDateTime afterSecond = OffsetDateTime.now();
        JsonNode busy = json(second, 409);
        assertThat(busy.get("error").asText()).isEqualTo("request_in_flight");
        assertThat(busy.get("request_id").asText()).isEqualTo(key);
        assertThat(busy.get("route").asText()).isEqualTo(form.path);
        OffsetDateTime leaseUntil = OffsetDateTime.parse(busy.get("lease_until").asText());
        assertThat(OffsetDateTime.parse(busy.get("started_at").asText())).isBefore(leaseUntil);
        // Retry-After is ceil((lease_until - now) / 1 s) at the server's `now`, which lies between the two stamps
        // taken here on the same JVM clock, floored at 1. The lease on the default pool is 25 s + 2 x 30 s + 10 s.
        long retryAfter = Long.parseLong(header(second, "Retry-After"));
        assertThat(retryAfter).as("never below 1").isGreaterThanOrEqualTo(1);
        assertThat(retryAfter).as("no later than the lease length (95 s on the default pool)")
            .isLessThanOrEqualTo(95);
        assertThat(retryAfter).as("no later than the time left at the instant the request was sent, rounded up, "
                + "plus one")
            .isLessThanOrEqualTo(ceilSeconds(beforeSecond, leaseUntil) + 1);
        assertThat(retryAfter).as("and not less than the time left when the reply came back, rounded up")
            .isGreaterThanOrEqualTo(Math.max(1, ceilSeconds(afterSecond, leaseUntil)));
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
        if (form == Form.EXPIRE) {
            assertThat(firstBody.get("more").asBoolean()).as("3 of 5 eligible chashes are still there").isTrue();
            assertThat(firstBody.get("floor_refused").asBoolean()).isFalse();
            assertThat(firstBody.get("refused").asInt()).as("counted by default: no manifest row names any").isZero();
        } else {
            assertThat(firstBody.get("remaining").asInt()).isEqualTo(3);
        }
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

    /**
     * The cap is on what the ledger hands back, and Postgres renders a jsonb with a space after every ':' and ','
     * (several KB more than the compact text the engine sends). So this reads the replay's raw bytes, not a
     * re-serialisation, and asks Postgres itself what the next-longer sample would have rendered to.
     */
    @ParameterizedTest
    @EnumSource(value = Form.class, names = {"BOUNDED", "FLOORED"})
    void aSweepBodyOver64KbIsStoredWithATruncatedSample_theLiveResponseIsUntouched(Form form) throws Exception {
        String pad = "x".repeat(400);
        Fx f = seedOrphans(form, 300, pad);
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
        assertThat(replay.body().getBytes(StandardCharsets.UTF_8).length)
            .as("the replay's raw body, as Postgres renders the stored copy").isLessThanOrEqualTo(65_536);
        assertThat(stored.get("sample_truncated").asBoolean()).isTrue();
        int kept = stored.get("sample").size();
        assertThat(kept).isBetween(1, 299);
        for (int i = 0; i < kept; i++) {
            assertThat(stored.get("sample").get(i)).as("sample[%d] is the live sample's prefix", i)
                .isEqualTo(live.get("sample").get(i));
        }
        // Longest prefix that fits: one more element and the stored copy would be over the cap.
        var longer = (com.fasterxml.jackson.databind.node.ObjectNode) live.deepCopy();
        var longerSample = MAPPER.createArrayNode();
        for (int i = 0; i <= kept; i++) longerSample.add(live.get("sample").get(i));
        longer.set("sample", longerSample);
        longer.put("sample_truncated", true);
        assertThat(pgJsonbBytes(longer.toString()))
            .as("the stored sample is the longest prefix that fits: %d elements kept, %d would not", kept, kept + 1)
            .isGreaterThan(65_536);
        assertThat(stored.get("moved").asInt()).as("only the sample is cut").isEqualTo(300);
        assertThat(stored.get("remaining").asInt()).isZero();
        assertThat(stored.get("row_limit").asInt()).isEqualTo(300);
        if (form == Form.FLOORED) {
            assertThat(stored.get("floor")).isEqualTo(live.get("floor"));
            assertThat(stored.get("refused").asBoolean()).isFalse();
        }
        assertThat(status(key).get("response")).as("the status route reads the same stored copy").isEqualTo(stored);
    }

    /** The UTF-8 length of {@code json} as Postgres renders it back from a jsonb column. */
    private int pgJsonbBytes(String json) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.select(DSL.function("octet_length", Integer.class,
                DSL.val(json).cast(org.jooq.impl.SQLDataType.JSONB).cast(org.jooq.impl.SQLDataType.CLOB)))
            .fetchOne(0, Integer.class));
        return n[0];
    }

    // ── sweep_gate_busy: the two quarantine forms ────────────────────────────

    private Connection holdGate(Fx f) throws Exception {
        return holdGateOn(f.gateKey());
    }

    private Connection holdGateOn(String collection) throws Exception {
        Connection holder = pg.createConnection("");
        holder.setAutoCommit(false);
        DSL.using(holder, SQLDialect.POSTGRES)
           .select(DSL.function("pg_advisory_xact_lock", Object.class,
                   DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + collection))))
           .fetch();
        return holder;
    }

    @ParameterizedTest
    @EnumSource(value = Form.class, names = {"BOUNDED", "FLOORED", "EXPIRE"})
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
            assertThat(header(busy, "Retry-After")).as("the sweep gate wait is 2 s: ask again in 5").isEqualTo("5");
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
        assertThat(json(ok, 200).get(form.movedKey).asInt()).as("the gate released, the same key now runs").isEqualTo(2);
        assertThat(status(key).get("state").asText()).isEqualTo("committed");
    }

    @ParameterizedTest
    @EnumSource(value = Form.class, names = {"BOUNDED", "FLOORED", "EXPIRE"})
    void aRowLockTimeoutKeepsTodaysError_neverSweepGateBusy(Form form) throws Exception {
        Fx f = fixture(form, 3);
        String key = newKey();
        Connection holder = pg.createConnection("");
        holder.setAutoCommit(false);
        try {
            DSL.using(holder, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.METADATA, CHUNKS.METADATA)
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(f.source()))).execute();

            HttpResponse<String> unkeyed = sweep(f, 2, null);
            HttpResponse<String> keyed = sweep(f, 2, key);

            assertThat(keyed.body()).doesNotContain("sweep_gate_busy");
            assertThat(keyed.statusCode()).as("same status as the keyless call: %s", unkeyed.body())
                .isEqualTo(unkeyed.statusCode());
            assertThat(keyed.body()).isEqualTo(unkeyed.body());
            // Today's ladder has no arm for a row-lock 55P03 (the sweep gate's 55P03 is told apart by its
            // message): it is the opaque 500, and the keyed call must say exactly that, not some other failure.
            assertThat(keyed.statusCode()).as(keyed.body()).isEqualTo(500);
            assertThat(keyed.body()).isEqualTo("{\"error\":\"internal server error\"}");
            assertThat(header(keyed, "Idempotency-Key")).isEqualTo(key);
            assertThat(ledgerStatus(key)).as("the failed row records what the ladder sent").isEqualTo(500);
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

    @Test
    void expireTakesTheSiblingsGateNotTheOrigins_aHeldOriginGateDoesNotBlockIt() throws Exception {
        Fx f = fixture(Form.EXPIRE, 3);
        Connection holder = holdGateOn(f.origin());
        try {
            HttpResponse<String> r = sweep(f, 2, newKey());
            assertThat(json(r, 200).get("expired").asInt()).isEqualTo(2);
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
            case EXPIRE -> {
                var m = new LinkedHashMap<String, Object>();
                m.put("expired", got.get("expired"));
                m.put("refused", got.get("refused"));
                m.put("more", got.get("more"));
                m.put("row_limit", 2);
                m.put("floor_refused", false);
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

    // ── a pad that fits compact but not as Postgres renders it ───────────────

    @Test
    void aBodyThatFitsCompactButNotAsPostgresRendersItIsStillTruncated() throws Exception {
        Fx f = seedOrphans(Form.BOUNDED, ROWS_FIT_COMPACT_NOT_JSONB, "p".repeat(PAD_FIT_COMPACT_NOT_JSONB));
        String key = newKey();
        var req = body(f, ROWS_FIT_COMPACT_NOT_JSONB);
        req.put("sample_limit", ROWS_FIT_COMPACT_NOT_JSONB);

        HttpResponse<String> first = post(f.form().path, req, key);
        JsonNode live = json(first, 200);
        int compact = first.body().getBytes(StandardCharsets.UTF_8).length;
        int rendered = pgJsonbBytes(first.body());
        assertThat(live.get("sample")).hasSize(ROWS_FIT_COMPACT_NOT_JSONB);
        assertThat(compact).as("guard: the compact body fits the cap (compact=%d, as PG renders it=%d)", compact, rendered)
            .isLessThanOrEqualTo(65_536);
        assertThat(rendered).as("guard: and its jsonb rendering does not (compact=%d)", compact)
            .isGreaterThan(65_536);

        HttpResponse<String> replay = post(f.form().path, req, key);
        JsonNode stored = json(replay, 200);
        assertThat(stored.has("sample_truncated") && stored.get("sample_truncated").asBoolean())
            .as("the cap is on the rendered text, so the stored copy was cut though the compact body fit").isTrue();
        assertThat(stored.get("sample").size()).isLessThan(ROWS_FIT_COMPACT_NOT_JSONB);
        assertThat(replay.body().getBytes(StandardCharsets.UTF_8).length).isLessThanOrEqualTo(65_536);
    }

    private static final int ROWS_FIT_COMPACT_NOT_JSONB = 585;
    private static final int PAD_FIT_COMPACT_NOT_JSONB = 20;

    // ── per-exchange failure hook: concurrent failures and unkeyed neighbours ─

    /** A keyed failing sweep held after it armed its failure marker and before its response is written. */
    private record Held(CompletableFuture<HttpResponse<String>> call, CountDownLatch release) {
    }

    /**
     * Start {@code key}'s sweep so that its SQL raises {@code IllegalArgumentException} (400 from the ladder), and
     * hold it at the top of {@code HttpUtil.send}: the failure marker is armed, nothing is written yet. Any later
     * sweep SQL call raises an opaque {@code RuntimeException} (500).
     */
    private Held holdFailingSweepAtSend(Fx f, String key) throws Exception {
        var armed = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var calls = new AtomicInteger();
        repo.setAfterSweepSqlHookForTests(() -> {
            if (calls.incrementAndGet() == 1) throw new IllegalArgumentException("injected for the held request");
            throw new RuntimeException("injected for the second request");
        });
        dev.nexus.service.http.HttpUtil.setSendObserverForTests(ex -> {
            if (key.equals(ex.getRequestHeaders().getFirst("Idempotency-Key"))) {
                armed.countDown();
                try {
                    if (!release.await(60, TimeUnit.SECONDS)) throw new IllegalStateException("never released");
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        });
        var call = CompletableFuture.supplyAsync(() -> {
            try {
                return sweep(f, 2, key);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(armed.await(60, TimeUnit.SECONDS)).as("the failing sweep reached its send").isTrue();
        return new Held(call, release);
    }

    @Test
    void twoFailingKeyedSweepsAtOnceEachRecordTheirOwnStatus() throws Exception {
        Fx a = fixture(Form.BOUNDED, 3);
        Fx b = fixture(Form.RESTORE, 3);
        String keyA = newKey();
        String keyB = newKey();
        Held held = holdFailingSweepAtSend(a, keyA);
        try {
            HttpResponse<String> second = sweep(b, 2, keyB);

            assertThat(second.statusCode()).as(second.body()).isEqualTo(500);
            assertThat(ledgerStatus(keyB)).as("the second request marks its own row with its own status").isEqualTo(500);
            assertThat(status(keyB).get("state").asText()).isEqualTo("rolled_back");
            assertThat(ledgerStatus(keyA)).as("the held request's marker has not fired: it is still waiting to send")
                .isNull();
        } finally {
            held.release().countDown();
        }
        HttpResponse<String> first = held.call().get(60, TimeUnit.SECONDS);
        assertThat(first.statusCode()).as(first.body()).isEqualTo(400);
        assertThat(ledgerStatus(keyA)).as("the held request marks its own row, once, with its own status")
            .isEqualTo(400);
        assertThat(status(keyA).get("state").asText()).as("not left in_flight to its lease").isEqualTo("rolled_back");
        assertThat(ledgerStatus(keyB)).as("and did not touch the other's").isEqualTo(500);
    }

    @Test
    void anUnkeyedVectorsCallWhileAKeyedFailureIsPendingTriggersNoMarker() throws Exception {
        Fx a = fixture(Form.BOUNDED, 3);
        String keyA = newKey();
        Held held = holdFailingSweepAtSend(a, keyA);
        try {
            HttpResponse<String> neighbour = get("/v1/vectors/count?collection=" + a.origin(), null);

            assertThat(neighbour.statusCode()).as(neighbour.body()).isEqualTo(200);
            assertThat(ledgerStatus(keyA)).as("another /v1/vectors response must not fire the held request's marker")
                .isNull();
        } finally {
            held.release().countDown();
        }
        HttpResponse<String> first = held.call().get(60, TimeUnit.SECONDS);
        assertThat(first.statusCode()).isEqualTo(400);
        assertThat(ledgerStatus(keyA)).as("its own send marks it with its own status").isEqualTo(400);
        assertThat(status(keyA).get("state").asText()).isEqualTo("rolled_back");
    }

    // ── a committed key replays even when its collection has gone ────────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void aCommittedKeyReplaysAfterItsCollectionIsRemoved_otherKeysGetTodays422(Form form) throws Exception {
        Fx f = fixture(form, 4);
        String key = newKey();
        JsonNode firstBody = json(sweep(f, 2, key), 200);
        su(ctx -> {
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)
                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(f.origin()))).execute();
            ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(f.origin()))).execute();
            ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
               .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                   .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(f.origin()))).execute();
        });
        dev.nexus.service.db.CollectionRegistry.evict(TENANT, f.origin());
        var before = RequestLedgerActivity.snapshot();

        HttpResponse<String> replay = sweep(f, 2, key);

        assertThat(json(replay, 200)).as("the stored response, though the collection is gone").isEqualTo(firstBody);
        assertThat(header(replay, "Idempotency-Replayed")).isEqualTo("true");
        assertThat(header(replay, "Idempotency-Key")).isEqualTo(key);
        assertThat(RequestLedgerActivity.snapshot().replays() - before.replays()).isEqualTo(1);

        HttpResponse<String> otherBody = sweep(f, 3, key);
        assertThat(json(otherBody, 422).get("error").asText()).as("a committed key with another body: reused")
            .isEqualTo("idempotency_key_reused");

        String fresh = newKey();
        HttpResponse<String> unknown = sweep(f, 2, fresh);
        assertThat(json(unknown, 422).get("reason").asText()).as("a key never seen: today's error")
            .isEqualTo("unregistered_collection");
        assertThat(ledgerRows(fresh)).as("and no ledger row").isZero();
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
        // LedgerMarkerLost is no typed arm of the ladder: the opaque 500, with the generic body and no detail.
        assertThat(r.statusCode()).as("the sweep did not commit: %s", r.body()).isEqualTo(500);
        assertThat(r.body()).isEqualTo("{\"error\":\"internal server error\"}");
        assertThat(header(r, "Idempotency-Key")).as("a 500 after the claim still echoes the key").isEqualTo(key);
        assertThat(count(f.source())).as("the mutation rolled back with the marker").isEqualTo(3);
        assertThat(movedRows(f)).isZero();
        assertThat(status(key).get("state").asText()).isEqualTo("rolled_back");
        assertThat(ledgerStatus(key)).isEqualTo(500);
    }

    // ── the failure status the ledger records is the one the ladder sent ─────

    /** An exception a sweep can raise after its SQL, with the status today's handler ladder answers for it. */
    enum Failure {
        ILLEGAL_ARGUMENT(400, () -> new IllegalArgumentException("injected bad argument")),
        ILLEGAL_STATE(422, () -> new IllegalStateException("injected refused state")),
        UNREGISTERED_COLLECTION(422,
            () -> new dev.nexus.service.db.UnregisteredCollectionException(TENANT, "injected-collection")),
        POOL_EXHAUSTED(503, () -> new RuntimeException("injected",
            new java.sql.SQLTransientConnectionException("HikariPool - Connection is not available"))),
        RESTORE_BUSY(503, () -> new PgVectorRepository.QuarantineRestoreBusyException("injected lock bound", null)),
        CLASS_23(409, () -> new RuntimeException("injected", new java.sql.SQLException("duplicate", "23505"))),
        OPAQUE(500, () -> new RuntimeException("injected opaque failure"));

        final int status;
        final java.util.function.Supplier<RuntimeException> make;

        Failure(int status, java.util.function.Supplier<RuntimeException> make) {
            this.status = status;
            this.make = make;
        }
    }

    static java.util.stream.Stream<org.junit.jupiter.params.provider.Arguments> failures() {
        var out = new ArrayList<org.junit.jupiter.params.provider.Arguments>();
        for (Failure fl : Failure.values()) {
            for (Form form : Form.values()) {
                out.add(org.junit.jupiter.params.provider.Arguments.of(fl, form));
            }
        }
        return out.stream();
    }

    @ParameterizedTest
    @org.junit.jupiter.params.provider.MethodSource("failures")
    void aFailedKeyedSweepRecordsTheStatusTheLadderSent_andEchoesTheKey(Failure failure, Form form) throws Exception {
        Fx f = fixture(form, 3);
        String key = newKey();
        repo.setAfterSweepSqlHookForTests(() -> { throw failure.make.get(); });

        HttpResponse<String> r = sweep(f, 2, key);

        assertThat(r.statusCode()).as("%s: %s", failure, r.body()).isEqualTo(failure.status);
        assertThat(header(r, "Idempotency-Key")).as("the echo on a failure after the claim").isEqualTo(key);
        if (failure == Failure.POOL_EXHAUSTED) {
            // nexus-yye8l.18: a failure that IS pool exhaustion skips the marker (it would queue on the same
            // exhausted pool); the row stays in_flight until the lease lapses, which covers it.
            assertThat(ledgerStatus(key)).as("no failure marker on pool exhaustion").isNull();
            assertThat(status(key).get("state").asText()).isEqualTo("in_flight");
        } else {
            assertThat(ledgerStatus(key)).as("%s: the failed row records the status the client was sent", failure)
                .isEqualTo(failure.status);
            assertThat(status(key).get("state").asText()).isEqualTo("rolled_back");
        }
        assertThat(count(f.source())).as("the sweep rolled back").isEqualTo(3);
    }

    // ── a response that cannot be written after the commit never un-commits it ──

    @ParameterizedTest
    @EnumSource(Form.class)
    void aResponseWriteFailureAfterTheCommit_leavesTheRequestCommitted_andAResendReplaysIt(Form form) throws Exception {
        // RDR-222 minimum viable validation: "a committed request is followed by an exception". The edge has cut
        // the connection, so writing the 200 fails after the sweep committed; the handler's ladder then answers
        // 500 on the (dead) exchange. None of that may touch the row.
        // nexus-yye8l.22: this pins the OUTCOME (the row stays committed, a resend replays it), not the code shape
        // that gets there. It would pass for any implementation that keeps a committed row committed, including
        // one that never reaches a failure marker for a different reason; that is intended.
        Fx f = fixture(form, 3);
        String key = newKey();
        var armed = new AtomicBoolean(true);
        dev.nexus.service.http.HttpUtil.setSendObserverForTests(ex -> {
            if (ex.getRequestURI().getPath().equals(form.path) && armed.compareAndSet(true, false)) {
                throw new java.io.UncheckedIOException(new java.io.IOException("Broken pipe"));
            }
        });

        HttpResponse<String> first = sweep(f, 2, key);

        assertThat(armed).as("the write failure was injected on the success send").isFalse();
        assertThat(first.statusCode()).as("the client saw a failure: %s", first.body()).isEqualTo(500);
        assertThat(movedRows(f)).as("the sweep committed before the write failed").isEqualTo(2);
        JsonNode st = status(key);
        assertThat(st.get("state").asText()).as("a committed request never reads rolled_back").isEqualTo("committed");
        assertThat(st.get("response_status").asInt()).isEqualTo(200);
        assertThat(ledgerStatus(key)).isEqualTo(200);

        dev.nexus.service.http.HttpUtil.setSendObserverForTests(null);
        HttpResponse<String> resend = sweep(f, 2, key);
        assertThat(resend.statusCode()).as(resend.body()).isEqualTo(200);
        assertThat(header(resend, "Idempotency-Replayed")).isEqualTo("true");
        assertThat(MAPPER.readTree(resend.body())).isEqualTo(st.get("response"));
        assertThat(movedRows(f)).as("the replay moved nothing more").isEqualTo(2);
    }

    // ── a claim that cannot settle answers 503, not 422 ───────────────────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void aClaimThatNeverSettles_answers503ClaimContendedWithRetryAfter_notThe422DoNotResend(Form form)
            throws Exception {
        Fx f = fixture(form, 2);
        String key = newKey();
        // A failed row for the key (the first attempt's opaque failure), so the resend reaches the takeover.
        repo.setAfterSweepSqlHookForTests(() -> { throw new RuntimeException("injected opaque failure"); });
        assertThat(sweep(f, 2, key).statusCode()).isEqualTo(500);
        repo.setAfterSweepSqlHookForTests(null);
        assertThat(status(key).get("state").asText()).isEqualTo("rolled_back");
        // Another attempt wins the row between this claim's read and its takeover UPDATE, every round: the seam
        // changes the holder (the lease stays expired) just before the takeover, so each round ends with taken == 0.
        var ledger = service.requestLedgerForTests();
        ledger.setClaimPhaseHookForTests((phase, round) -> {
            if (phase == dev.nexus.service.db.RequestLedger.ClaimPhase.BEFORE_TAKEOVER) {
                try {
                    su(ctx -> ctx.update(REQUEST_LEDGER).set(REQUEST_LEDGER.HOLDER, UUID.randomUUID())
                        .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT)
                            .and(REQUEST_LEDGER.REQUEST_ID.eq(UUID.fromString(key)))).execute());
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }
        });
        try {
            HttpResponse<String> r = sweep(f, 2, key);

            assertThat(r.statusCode()).as("the claim could not settle; resending is safe: %s", r.body()).isEqualTo(503);
            assertThat(MAPPER.readTree(r.body()).get("error").asText()).isEqualTo("request_claim_contended");
            assertThat(header(r, "Retry-After")).isEqualTo("1");
            assertThat(count(f.source())).as("nothing ran").isEqualTo(2);
        } finally {
            ledger.setClaimPhaseHookForTests(null);
        }
        HttpResponse<String> resend = sweep(f, 2, key);
        assertThat(resend.statusCode()).as("with the contention gone the same key runs: %s", resend.body()).isEqualTo(200);
    }

    // ── a collection the sweep cannot resolve fails before the claim ─────────

    @ParameterizedTest
    @EnumSource(Form.class)
    void anUnknownCollectionWithAKeyGivesTodaysError_andNoLedgerRow(Form form) throws Exception {
        Fx f = fixture(form, 2);
        var req = body(f, 2);
        String missing = (form.quarantine() ? "knowledge__nosuch" : "code__nosuch") + seq.incrementAndGet()
            + "__minilm-l6-v2-384__v1";
        req.put(form.quarantine() ? "collection" : "origin_collection", missing);
        String key = newKey();
        var before = RequestLedgerActivity.snapshot();

        HttpResponse<String> unkeyed = post(form.path, req, null);
        HttpResponse<String> keyed = post(form.path, req, key);

        assertThat(unkeyed.statusCode()).as(unkeyed.body()).isEqualTo(422);
        assertThat(unkeyed.body()).contains("unregistered_collection").contains(missing);
        assertThat(keyed.statusCode()).isEqualTo(unkeyed.statusCode());
        assertThat(keyed.body()).isEqualTo(unkeyed.body());
        assertThat(ledgerRows(key)).as("the collection is resolved before the claim: no row").isZero();
        assertThat(status(key).get("state").asText()).isEqualTo("unknown");
        assertThat(RequestLedgerActivity.snapshot().inFlight() - before.inFlight()).isZero();
        assertThat(count(f.source())).as("nothing moved").isEqualTo(2);
    }

    // ── expire-quarantine specifics (RDR-222 Amendment A1) ───────────────────

    @Test
    void anExpireRowLimitMustBeAPositiveInteger_andAnUnboundedCallKeepsItsExactBody() throws Exception {
        Fx f = fixture(Form.EXPIRE, 3);
        for (Object bad : new Object[] {0, -1, "5"}) {
            var req = body(f, 2);
            req.put("row_limit", bad);
            HttpResponse<String> r = post(EXPIRE_PATH, req, null);
            assertThat(r.statusCode()).as("row_limit=%s: %s", bad, r.body()).isEqualTo(400);
        }
        assertThat(count(f.source())).as("nothing ran").isEqualTo(3);

        var unbounded = body(f, 2);
        unbounded.remove("row_limit");
        HttpResponse<String> r = post(EXPIRE_PATH, unbounded, null);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(MAPPER.readValue(r.body(), Map.class).keySet()).as("byte-compatible with the pre-A1 route")
            .containsExactlyInAnyOrder("expired", "refused");
        assertThat(count(f.source())).as("the unbounded form expired everything").isZero();
    }

    @Test
    void anOversizedExpireRowLimitIsEchoedAsTheClampedFiveThousand() throws Exception {
        Fx f = fixture(Form.EXPIRE, 3);

        JsonNode b = json(sweep(f, 6_000, null), 200);

        assertThat(b.get("row_limit").asInt()).as("the function clamps at 5,000 and the route echoes what applied")
            .isEqualTo(5_000);
        assertThat(b.get("expired").asInt()).isEqualTo(3);
        assertThat(b.get("more").asBoolean()).isFalse();
    }

    @Test
    void anUncountedRefusedIsJsonNullEndToEnd_storedReplayedAndReadFromTheStatusRoute() throws Exception {
        Fx f = fixture(Form.EXPIRE, 4);
        var req = body(f, 2);
        // The inert floor both client callers send: with the route's own defaults (0.5 / 50) the function runs
        // its floor scan, which answers refused whatever count_refused says.
        req.put("floor_fraction", 1.0);
        req.put("floor_min_chunks", 1);
        req.put("count_refused", false);
        String key = newKey();
        repo.setAfterSweepSqlHookForTests(sqlCalls::incrementAndGet);
        sqlCalls.set(0);

        HttpResponse<String> first = post(EXPIRE_PATH, req, key);
        JsonNode live = json(first, 200);
        assertThat(live.has("refused")).as("the field is present").isTrue();
        assertThat(live.get("refused").isNull()).as("and null, not 0: %s", first.body()).isTrue();
        assertThat(live.get("expired").asInt()).isEqualTo(2);
        assertThat(live.get("more").asBoolean()).isTrue();

        HttpResponse<String> replay = post(EXPIRE_PATH, req, key);
        assertThat(json(replay, 200)).isEqualTo(live);
        assertThat(header(replay, "Idempotency-Replayed")).isEqualTo("true");
        assertThat(sqlCalls.get()).as("the replay did not run the SQL function").isEqualTo(1);
        JsonNode st = status(key);
        assertThat(st.get("state").asText()).isEqualTo("committed");
        assertThat(st.get("response").get("refused").isNull()).as("the stored copy carries the null").isTrue();
    }

    @Test
    void aKeyWithoutRowLimitIsRefusedOnExpireAsOnTheUnboundedRestore() throws Exception {
        Fx f = fixture(Form.EXPIRE, 2);
        var unbounded = body(f, 2);
        unbounded.remove("row_limit");
        String key = newKey();

        HttpResponse<String> r = post(EXPIRE_PATH, unbounded, key);

        assertThat(json(r, 400).get("error").asText()).isEqualTo("idempotency_key_unsupported");
        assertThat(ledgerRows(key)).isZero();
        assertThat(count(f.source())).as("nothing ran").isEqualTo(2);
    }

    // ── census: every route the guard lets a key through is wired to the keyed path ──

    @Test
    void everyRouteTheGuardPassesAKeyOnIsServedByAKeyedHandler() throws Exception {
        // The keyable request per route. A route added to KEYED_ROUTES with no entry here fails the first
        // assertion, so the author must say how to key it; one whose handler never reaches the ledger fails the
        // second (the guard would pass its key and the handler would drop it).
        Map<String, Fx> keyable = new LinkedHashMap<>();
        keyable.put(QUARANTINE_PATH, seedOrphans(Form.BOUNDED, 2, "t"));
        keyable.put(RESTORE_PATH, seedRestore(Form.RESTORE, 2));
        keyable.put(EXPIRE_PATH, seedExpire(Form.EXPIRE, 2));

        var routes = dev.nexus.service.http.IdempotencyKeyGuard.KEYED_ROUTES;
        assertThat(routes).as("the census must see the guard's routes").isNotEmpty();
        assertThat(keyable.keySet()).as("a keyable request is defined for every route in KEYED_ROUTES")
            .containsAll(routes);
        for (String route : routes) {
            Fx f = keyable.get(route);
            String key = newKey();
            HttpResponse<String> r = sweep(f, 1, key);
            assertThat(r.statusCode()).as("%s: %s", route, r.body()).isEqualTo(200);
            assertThat(header(r, "Idempotency-Key")).as(route).isEqualTo(key);
            assertThat(ledgerRows(key)).as("%s is served by a keyed handler: its key left a ledger row", route)
                .isEqualTo(1);
            assertThat(status(key).get("state").asText()).as(route).isEqualTo("committed");
            assertThat(status(key).get("response")).as(route).isEqualTo(MAPPER.readTree(r.body()));
        }
    }
}

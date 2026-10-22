// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
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
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.64: the expire-quarantine and restore-rereferenced routes resolve the quarantine siblings of the
 * origin themselves when a request says {@code resolve_siblings: true}, so a client no longer learns them from a
 * dry-run {@code quarantine-restore} probe (an unbounded scan the {@code nx index repo} hot path could not afford).
 *
 * <p>Real HTTP through the RLS-subject service role against real Postgres. What is pinned:
 * <ul>
 *   <li>the answer: the reaper's own name for the origin, then every other registered quarantine collection holding a
 *       chunk tagged with the origin, and NOT a collection holding only another origin's chunks;
 *   <li>the same on all six shapes (restore and expire, each unbounded, bounded and keyed bounded);
 *   <li>a request without the field sees the body it always saw, with neither new key (so {@code [additive]});
 *   <li>a keyed replay returns the stored body, the sibling list included;
 *   <li>the read runs under a statement bound of its own and reports a trip as {@code timedOut}, never as "none".
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcSiblingResolutionRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-siblingres-tenant-0123456789abcdef00";
    private static final String SVC_ROLE = "svc_siblingres_route";
    private static final String SVC_PASS = "svc_siblingres_route_pass";
    private static final String TENANT = "siblingres-route";
    private static final String RESTORE_PATH = "/v1/vectors/gc/restore-rereferenced";
    private static final String EXPIRE_PATH = "/v1/vectors/gc/expire-quarantine";
    private static final String EXPIRE_CUTOFF = "2000-01-01T00:00:00Z";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private PgVectorRepository repo;
    private NexusService service;
    private HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "siblingres");
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

    // ── fixtures ──────────────────────────────────────────────────────────────

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

    private JsonNode ok(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String origin() throws Exception {
        String origin = "code__sr" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        su(ctx -> PgContainerHelper.insertCollection(ctx, TENANT, origin));
        return origin;
    }

    /** A registered quarantine collection holding one chunk tagged {@code originTag} (null: an untagged chunk). */
    private String sibling(String name, String originTag) throws Exception {
        var meta = new LinkedHashMap<String, Object>();
        meta.put("quarantined_at", "2026-07-01T00:00:00Z");
        if (originTag != null) meta.put("origin_collection", originTag);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, name);
            PgContainerHelper.insertChunks(ctx, TENANT, name, List.of(Chash.ofText(name + "/0").toHex()),
                List.of("quarantined " + name), List.of(new float[384]), List.of(meta));
        });
        return name;
    }

    /** The catalog-044 shape: the reaper's name, a renamed sibling holding tagged chunks, and an unrelated one. */
    private record Siblings(String origin, String reaper, String renamed, String other) {}

    private Siblings siblings() throws Exception {
        String origin = origin();
        int id = seq.incrementAndGet();
        String reaper = sibling("quarantine-" + origin, origin);
        String renamed = sibling("quarantine-code__oldowner" + id + "__minilm-l6-v2-384__v1", origin);
        String otherOrigin = origin();
        String other = sibling("quarantine-code__otherowner" + id + "__minilm-l6-v2-384__v1", otherOrigin);
        return new Siblings(origin, reaper, renamed, other);
    }

    private static Map<String, Object> restoreBody(Siblings s, Integer rowLimit, Boolean resolve) {
        var m = new LinkedHashMap<String, Object>();
        m.put("quarantine_collection", s.reaper());
        m.put("origin_collection", s.origin());
        if (rowLimit != null) m.put("row_limit", rowLimit);
        if (resolve != null) m.put("resolve_siblings", resolve);
        return m;
    }

    private static Map<String, Object> expireBody(Siblings s, Integer rowLimit, Boolean resolve) {
        var m = restoreBody(s, rowLimit, resolve);
        m.put("cutoff", EXPIRE_CUTOFF);
        return m;
    }

    private static List<String> names(JsonNode body) {
        var out = new ArrayList<String>();
        body.path("quarantine_collections").forEach(n -> out.add(n.asText()));
        return out;
    }

    // ── the answer ────────────────────────────────────────────────────────────

    @Test
    void theRepositoryAnswersTheReapersNameThenTheTaggedSiblingAndNotAnotherOriginsOne() throws Exception {
        Siblings s = siblings();
        var resolved = repo.resolveQuarantineSiblingsBounded(TENANT, s.origin());
        assertThat(resolved.timedOut()).isFalse();
        assertThat(resolved.names()).containsExactly(s.reaper(), s.renamed());
        assertThat(resolved.names()).doesNotContain(s.other());
        // The bounded read is the same set the unbounded one gives, in the same order.
        assertThat(resolved.names()).isEqualTo(repo.resolveQuarantineSiblings(TENANT, s.origin()));
    }

    @Test
    void anOriginWithNothingQuarantinedResolvesToNoSibling() throws Exception {
        var resolved = repo.resolveQuarantineSiblingsBounded(TENANT, origin());
        assertThat(resolved.timedOut()).isFalse();
        assertThat(resolved.names()).isEmpty();
    }

    @Test
    void aReadThatRunsIntoItsBoundIsTimedOutNeverAnEmptyList() throws Exception {
        Siblings s = siblings();
        // An ACCESS EXCLUSIVE lock on the chunks table held by another session blocks the EXISTS over the tagged
        // sibling (the reaper's name is answered from the catalog row alone), so the read waits until its bound.
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long start = System.nanoTime();
            var resolved = repo.resolveQuarantineSiblingsBounded(TENANT, s.origin(), 300);
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;
            holder.rollback();
            assertThat(resolved.timedOut()).as("a read that could not finish is reported, not answered as none").isTrue();
            assertThat(resolved.names()).isEmpty();
            assertThat(elapsedMs).as("it gave up at its bound, it did not wait for the lock").isLessThan(5_000);
        }
        // With the lock released the same read answers.
        assertThat(repo.resolveQuarantineSiblingsBounded(TENANT, s.origin(), 300).names())
            .containsExactly(s.reaper(), s.renamed());
    }

    @Test
    void aLockWaitThatRunsOutBeforeTheStatementBoundIsTimedOutToo() throws Exception {
        Siblings s = siblings();
        // The default bounds are both 2 s, so which one fires first is a race. A statement bound well past the lock
        // bound (the lock bound is fixed at its 2 s default) makes the lock wait the one that ends the read: SQLSTATE
        // 55P03, which resolveQuarantineSiblingsBounded must answer as timedOut, not throw.
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long start = System.nanoTime();
            var resolved = repo.resolveQuarantineSiblingsBounded(TENANT, s.origin(), 30_000);
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;
            holder.rollback();
            assertThat(resolved.timedOut()).as("a lock wait that ran out is reported, not thrown or answered as none").isTrue();
            assertThat(resolved.names()).isEmpty();
            assertThat(elapsedMs).as("the 2 s lock bound ended it, not the 30 s statement bound")
                .isBetween(1_500L, 15_000L);
        }
    }

    // ── a read that cannot finish: the route says so and the page still runs ──

    /**
     * POSTs {@code body} with the sibling read blocked until it runs out its bound, then lets the page through.
     *
     * <p>Another session holds ACCESS EXCLUSIVE on {@code nexus.chunks}, which the sibling read's EXISTS scans. The
     * page needs it too, so the lock cannot be held for the whole request (the page would wait on it, and the holder
     * waits on the page). A releaser thread watches {@code pg_stat_activity} for the sibling read waiting on the lock,
     * remembers which statement it is, and drops the lock only once THAT statement is gone: the read has by then ended
     * on its bound, and the page's own wait is over as soon as the lock goes. (The table is not the catalog's collection
     * table: a keyed request reads the origin's dimension from it before the sibling read, and would wait there.)
     */
    private JsonNode postWithTheSiblingReadBlocked(String path, Map<String, Object> body, String key) throws Exception {
        try (Connection holder = pg.createConnection("");
             Connection probe = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var seenWaiting = new java.util.concurrent.atomic.AtomicBoolean();
            var failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            Thread releaser = new Thread(() -> {
                try {
                    var ctx = DSL.using(probe, SQLDialect.POSTGRES);
                    String tracked = null;   // "pid/query_start" of the sibling read once seen waiting
                    long deadline = System.nanoTime() + 60_000_000_000L;
                    while (System.nanoTime() < deadline) {
                        var waiting = new ArrayList<String>();
                        var act = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
                        var pid = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "pid"), Integer.class);
                        var usename = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "usename"), String.class);
                        var startedAt = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "query_start"),
                            java.time.OffsetDateTime.class);
                        var query = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "query"), String.class);
                        var waitType = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "wait_event_type"), String.class);
                        for (var r : ctx.select(pid, usename, startedAt, query).from(act).where(waitType.eq("Lock")).fetch()) {
                            String q = r.get(query);
                            // The sibling read: a catalog scan for quarantine collections with the EXISTS over chunks.
                            if (SVC_ROLE.equals(r.get(usename)) && q != null && q.contains("catalog_collections")
                                    && q.contains("lifecycle_state") && q.contains("exists")) {
                                waiting.add(r.get(pid) + "/" + r.get(startedAt));
                            }
                        }
                        if (tracked == null && !waiting.isEmpty()) {
                            tracked = waiting.get(0);
                            seenWaiting.set(true);
                        } else if (tracked != null && !waiting.contains(tracked)) {
                            break;
                        }
                        Thread.sleep(25);
                    }
                } catch (Throwable t) {
                    failure.set(t);
                } finally {
                    try {
                        holder.rollback();
                    } catch (Exception ignored) {
                        // the holder is closed by the test on its way out
                    }
                }
            }, "sibling-read-lock-releaser");
            releaser.start();
            try {
                return ok(post(path, body, key));
            } finally {
                releaser.join(70_000);
                assertThat(failure.get()).as("the releaser thread").isNull();
                assertThat(seenWaiting.get()).as("the sibling read was seen waiting on the lock, so it timed out").isTrue();
            }
        }
    }

    private static Map<String, Object> expireNow(Map<String, Object> body) {
        var m = new LinkedHashMap<String, Object>(body);
        m.put("cutoff", "2100-01-01T00:00:00Z");   // later than every quarantined_at: the page expires the chunk
        return m;
    }

    private long chunksIn(String collection) throws Exception {
        var n = new java.util.concurrent.atomic.AtomicLong();
        su(ctx -> n.set(ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection))));
        return n.get();
    }

    @Test
    void anExpiryWhoseSiblingReadTimesOutAnswersTimedOutWithNoListAndStillExpiresOnEveryShape() throws Exception {
        record Shape(String name, Integer rowLimit, String key) {}
        for (Shape shape : List.of(new Shape("unbounded", null, null), new Shape("bounded", 100, null),
                                   new Shape("keyed", 100, UUID.randomUUID().toString()))) {
            Siblings s = siblings();
            assertThat(chunksIn(s.reaper())).as(shape.name()).isEqualTo(1);
            JsonNode body = postWithTheSiblingReadBlocked(EXPIRE_PATH, expireNow(expireBody(s, shape.rowLimit(), true)),
                shape.key());
            assertThat(body.path("sibling_resolution").asText()).as(shape.name() + " " + body).isEqualTo("timed_out");
            assertThat(body.has("quarantine_collections"))
                .as(shape.name() + ": no list at all, never [] standing in for none: " + body).isFalse();
            assertThat(body.path("expired").asLong()).as(shape.name() + ": the page's own count " + body).isEqualTo(1);
            assertThat(body.has("refused")).as(shape.name() + " " + body).isTrue();
            if (shape.rowLimit() != null) {
                assertThat(body.has("more")).as(shape.name() + " " + body).isTrue();
            }
            assertThat(chunksIn(s.reaper())).as(shape.name() + ": the page's effect landed").isZero();
        }
    }

    @Test
    void aRestoreWhoseSiblingReadTimesOutAnswersTimedOutWithNoListAndStillRunsOnEveryShape() throws Exception {
        record Shape(String name, Integer rowLimit, String key) {}
        for (Shape shape : List.of(new Shape("unbounded", null, null), new Shape("bounded", 100, null),
                                   new Shape("keyed", 100, UUID.randomUUID().toString()))) {
            Siblings s = siblings();
            JsonNode body = postWithTheSiblingReadBlocked(RESTORE_PATH, restoreBody(s, shape.rowLimit(), true), shape.key());
            assertThat(body.path("sibling_resolution").asText()).as(shape.name() + " " + body).isEqualTo("timed_out");
            assertThat(body.has("quarantine_collections")).as(shape.name() + " " + body).isFalse();
            assertThat(body.has("restored")).as(shape.name() + ": the page ran " + body).isTrue();
            if (shape.rowLimit() != null) {
                assertThat(body.has("remaining")).as(shape.name() + " " + body).isTrue();
            }
        }
    }

    /**
     * A keyed request the ledger already committed is answered from the ledger, so the sibling read, which a fresh page
     * runs before it claims its row, would be thrown away; the route skips it. With ACCESS EXCLUSIVE held on
     * {@code nexus.chunks} that read waits out its 2 s bound, so a replay that answers at once did not run it. The
     * answer is the stored body, the sibling list the first run read included, and it carries the replayed header.
     */
    @Test
    void aCommittedKeyedReplayDoesNotReadTheSiblingsAgain() throws Exception {
        record Shape(String name, String path, boolean expire) {}
        for (Shape shape : List.of(new Shape("expire", EXPIRE_PATH, true), new Shape("restore", RESTORE_PATH, false))) {
            Siblings s = siblings();
            String key = UUID.randomUUID().toString();
            Map<String, Object> request = shape.expire() ? expireBody(s, 100, true) : restoreBody(s, 100, true);
            JsonNode first = ok(post(shape.path(), request, key));
            assertThat(names(first)).as(shape.name()).containsExactly(s.reaper(), s.renamed());
            try (Connection holder = pg.createConnection("")) {
                holder.setAutoCommit(false);
                PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
                long start = System.nanoTime();
                HttpResponse<String> replay = post(shape.path(), request, key);
                long elapsedMs = (System.nanoTime() - start) / 1_000_000;
                holder.rollback();
                assertThat(replay.statusCode()).as(replay.body()).isEqualTo(200);
                assertThat(replay.headers().firstValue("Idempotency-Replayed")).as(shape.name()).hasValue("true");
                assertThat(MAPPER.readTree(replay.body())).as(shape.name() + ": the stored body, siblings included")
                    .isEqualTo(first);
                assertThat(elapsedMs).as(shape.name() + ": answered from the ledger, no 2 s sibling read")
                    .isLessThan(1_500);
            }
        }
    }

    // ── the six route shapes ──────────────────────────────────────────────────

    @Test
    void restoreRereferencedAnswersTheSiblingsOnEveryShape() throws Exception {
        Siblings s = siblings();
        JsonNode unbounded = ok(post(RESTORE_PATH, restoreBody(s, null, true), null));
        assertThat(unbounded.has("restored")).isTrue();
        assertThat(names(unbounded)).containsExactly(s.reaper(), s.renamed());

        JsonNode bounded = ok(post(RESTORE_PATH, restoreBody(s, 100, true), null));
        assertThat(bounded.path("row_limit").asInt()).isEqualTo(100);
        assertThat(bounded.has("remaining")).isTrue();
        assertThat(names(bounded)).containsExactly(s.reaper(), s.renamed());

        JsonNode keyed = ok(post(RESTORE_PATH, restoreBody(s, 100, true), UUID.randomUUID().toString()));
        assertThat(keyed.has("remaining")).isTrue();
        assertThat(names(keyed)).containsExactly(s.reaper(), s.renamed());
    }

    @Test
    void expireQuarantineAnswersTheSiblingsOnEveryShape() throws Exception {
        Siblings s = siblings();
        JsonNode unbounded = ok(post(EXPIRE_PATH, expireBody(s, null, true), null));
        assertThat(unbounded.has("expired")).isTrue();
        assertThat(names(unbounded)).containsExactly(s.reaper(), s.renamed());

        // The unbounded form above expired the rows (cutoff in the past is later than quarantined_at): re-seed.
        Siblings again = siblings();
        JsonNode bounded = ok(post(EXPIRE_PATH, expireBody(again, 100, true), null));
        assertThat(bounded.path("row_limit").asInt()).isEqualTo(100);
        assertThat(bounded.has("more")).isTrue();
        assertThat(names(bounded)).containsExactly(again.reaper(), again.renamed());

        Siblings third = siblings();
        JsonNode keyed = ok(post(EXPIRE_PATH, expireBody(third, 100, true), UUID.randomUUID().toString()));
        assertThat(keyed.has("more")).isTrue();
        assertThat(names(keyed)).containsExactly(third.reaper(), third.renamed());
    }

    /**
     * Why the client replaces its derived names with the engine's list instead of taking the union (nexus-i827c review,
     * item 4). The engine's list holds every registered quarantine collection a derived name could mean, so a derived
     * name the list lacks is not a registered collection and cannot hold a chunk. Two facts pin that:
     * <ul>
     *   <li>the reaper's name for the origin is listed when it is registered whether or not any chunk in it carries
     *       the origin tag (an untagged chunk an older client moved there is still reached);
     *   <li>a name that is not registered is answered as an empty page (below), so the union could only add empty round
     *       trips (one restore page and one expire page per such name per run), never a chunk.
     * </ul>
     */
    @Test
    void theReapersNameIsListedWhenRegisteredEvenIfItsChunksCarryNoOriginTag() throws Exception {
        String origin = origin();
        String reaper = sibling("quarantine-" + origin, null);   // registered, one untagged chunk
        var resolved = repo.resolveQuarantineSiblingsBounded(TENANT, origin);
        assertThat(resolved.names()).containsExactly(reaper);
    }

    @Test
    void aNameThatIsNotARegisteredCollectionIsAnEmptyPageNotAnError() throws Exception {
        Siblings s = siblings();
        String ghost = "quarantine-code__never-registered-" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        var restore = new LinkedHashMap<String, Object>(restoreBody(s, 100, null));
        restore.put("quarantine_collection", ghost);
        JsonNode restored = ok(post(RESTORE_PATH, restore, null));
        assertThat(restored.path("restored").asLong()).isZero();
        assertThat(restored.path("remaining").asLong()).isZero();
        var expire = new LinkedHashMap<String, Object>(expireNow(expireBody(s, 100, null)));
        expire.put("quarantine_collection", ghost);
        JsonNode expired = ok(post(EXPIRE_PATH, expire, UUID.randomUUID().toString()));
        assertThat(expired.path("expired").asLong()).isZero();
        assertThat(expired.path("more").asBoolean()).isFalse();
    }

    // ── additive: a request that does not ask sees what it always saw ─────────

    @Test
    void aRequestWithoutTheFieldSeesNeitherNewKeyOnAnyShape() throws Exception {
        Siblings s = siblings();
        for (Boolean resolve : new Boolean[] {null, Boolean.FALSE}) {
            for (JsonNode body : List.of(
                    ok(post(RESTORE_PATH, restoreBody(s, null, resolve), null)),
                    ok(post(RESTORE_PATH, restoreBody(s, 100, resolve), null)),
                    ok(post(EXPIRE_PATH, expireBody(s, null, resolve), null)),
                    ok(post(EXPIRE_PATH, expireBody(s, 100, resolve), null)),
                    ok(post(RESTORE_PATH, restoreBody(s, 100, resolve), UUID.randomUUID().toString())),
                    ok(post(EXPIRE_PATH, expireBody(s, 100, resolve), UUID.randomUUID().toString())))) {
                assertThat(body.has("quarantine_collections")).as(body.toString()).isFalse();
                assertThat(body.has("sibling_resolution")).as(body.toString()).isFalse();
            }
        }
        // The unbounded restore body is exactly what it was before the field existed.
        JsonNode plain = ok(post(RESTORE_PATH, restoreBody(s, null, null), null));
        assertThat(plain.fieldNames()).toIterable().containsExactly("restored");
    }

    @Test
    void aKeyedReplayReturnsTheStoredBodyWithTheSiblingList() throws Exception {
        Siblings s = siblings();
        String key = UUID.randomUUID().toString();
        JsonNode first = ok(post(EXPIRE_PATH, expireBody(s, 100, true), key));
        assertThat(names(first)).containsExactly(s.reaper(), s.renamed());
        JsonNode replay = ok(post(EXPIRE_PATH, expireBody(s, 100, true), key));
        assertThat(replay).as("a same-key request is answered from the ledger, siblings included").isEqualTo(first);
    }
}

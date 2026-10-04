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
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.52 (RDR-192): the optional fraction floor on {@code POST /v1/vectors/gc/quarantine-orphans},
 * {@code nexus.gc_quarantine_orphans_floored} (vectors-027). Over real HTTP through the RLS-subject service role,
 * fixtures by substrate SQL.
 *
 * <p>A fixture is a collection of {@code total} chunks of which {@code reapable} are aged past the 30 day grace
 * with no manifest row (so {@code chunk_is_reapable} selects them) and the rest are fresh (inside the grace, so it
 * does not). That makes the floor's two numbers, the whole reapable set and every stored chunk, independent knobs.
 *
 * <p>What each test pins is named by what its revert would do: a route that ignored the floor moves in
 * {@link #refusesAnOverFloorMoveInsteadOfMovingIt}; a floor judged without the sweep gate answers a refusal while a
 * manifest writer holds the gate in {@link #theFloorIsJudgedUnderTheSweepGateNotBeforeIt}; an engine that drops the
 * {@code floor} key from its response is what an old engine looks like in
 * {@link #everyResponseEchoesTheFloorItApplied_orThatNoneWasGiven}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcQuarantineOrphansFloorRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-gcfloor-tenant-0123456789abcdef00000";
    private static final String SVC_ROLE = "svc_gcfloor_route";
    private static final String SVC_PASS = "svc_gcfloor_route_pass";
    private static final String TENANT = "gcfloor-route";
    private static final String STAMP = "2026-10-04T00:00:00Z";

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
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "gcfloor");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        FakeEmbedder embedder = new FakeEmbedder(384);
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── http ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(Map<String, Object> body) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/gc/quarantine-orphans")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readValue(r.body(), Map.class);
    }

    private static Map<String, Object> req(Fixture f, Object... kv) {
        var m = new LinkedHashMap<String, Object>();
        m.put("collection", f.origin());
        m.put("quarantine_collection", f.sibling());
        m.put("quarantined_at", STAMP);
        m.put("sample_limit", 20);
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    // ── fixtures (substrate SQL) ─────────────────────────────────────────────

    private record Fixture(String origin, String sibling) {}

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** {@code total} chunks, the first {@code reapable} of them aged past the grace with no manifest row. */
    private Fixture seed(int total, int reapable) throws Exception {
        String origin = "knowledge__gcf" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        String sibling = "quarantine-" + origin;
        List<String> aged = new ArrayList<>();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, origin);
            for (int i = 0; i < total; i++) {
                String hex = Chash.ofText(origin + "/" + i).toHex();
                if (i < reapable) aged.add(hex);
                PgContainerHelper.insertChunks(ctx, TENANT, origin, List.of(hex), List.of("chunk " + i),
                    List.of(new float[384]), List.of(Map.<String, Object>of("title", "t" + i)));
            }
            OffsetDateTime then = OffsetDateTime.now().minus(ReapableFixtures.PAST_GRACE);
            for (String hex : aged) {
                ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, then)
                   .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(origin))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))).execute();
            }
        });
        return new Fixture(origin, sibling);
    }

    private int count(String collection) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))));
        return n[0];
    }

    private List<Map<String, Object>> audit(String collection, String operation) throws Exception {
        List<Map<String, Object>> out = new ArrayList<>();
        su(ctx -> ctx.selectFrom(GC_AUDIT)
            .where(GC_AUDIT.TENANT_ID.eq(TENANT).and(GC_AUDIT.COLLECTION.eq(collection))
                .and(GC_AUDIT.OPERATION.eq(operation)))
            .orderBy(GC_AUDIT.ID).forEach(r -> {
                try {
                    @SuppressWarnings("unchecked")
                    Map<String, Object> details = MAPPER.readValue(r.getDetails().data(), Map.class);
                    var row = new LinkedHashMap<String, Object>(details);
                    row.put("_chash_count", r.getChashCount());
                    row.put("_actor", r.getActor());
                    out.add(row);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }));
        return out;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> floorOf(Map<String, Object> body) {
        return (Map<String, Object>) body.get("floor");
    }

    // ── the floor ────────────────────────────────────────────────────────────

    @Test
    void refusesAnOverFloorMoveInsteadOfMovingIt() throws Exception {
        Fixture f = seed(10, 6);

        var body = json(post(req(f, "floor_fraction", 0.5, "floor_min_chunks", 3)));

        assertThat(body.get("refused")).isEqualTo(true);
        assertThat(body.get("moved")).isEqualTo(0);
        assertThat(body.get("reapable_count")).as("the whole reapable set, as judged").isEqualTo(6);
        assertThat(body.get("total_count")).as("every stored chunk, as judged").isEqualTo(10);
        assertThat(count(f.origin())).as("a refusal copies and deletes nothing").isEqualTo(10);
        assertThat(count(f.sibling())).isZero();
        assertThat(audit(f.origin(), "gc_quarantine_orphans")).as("no move row for a move that did not happen").isEmpty();
    }

    @Test
    void aRefusalIsAuditedWithItsCounts_oneRowPerRefusedCall() throws Exception {
        Fixture f = seed(10, 6);

        post(req(f, "floor_fraction", 0.5, "floor_min_chunks", 3));
        post(req(f, "floor_fraction", 0.5, "floor_min_chunks", 3, "row_limit", 2));

        var rows = audit(f.origin(), "gc_quarantine_orphans_refused");
        assertThat(rows).as("one row per refused call").hasSize(2);
        assertThat(rows.get(0)).containsEntry("reason", "FLOOR").containsEntry("form", "unbounded")
            .containsEntry("reapable_count", 6).containsEntry("total_count", 10)
            .containsEntry("floor_fraction", 0.5).containsEntry("floor_min_chunks", 3)
            .containsEntry("quarantine_collection", f.sibling())
            .containsEntry("_actor", "engine").containsEntry("_chash_count", 0);
        assertThat(rows.get(1)).containsEntry("form", "bounded");
    }

    @Test
    void theBoundedFormIsRefusedOnItsFirstBatch_andEchoesRemaining() throws Exception {
        Fixture f = seed(10, 6);

        var body = json(post(req(f, "row_limit", 2, "floor_fraction", 0.5, "floor_min_chunks", 3)));

        assertThat(body.get("refused")).isEqualTo(true);
        assertThat(body.get("moved")).isEqualTo(0);
        assertThat(body.get("remaining")).as("nothing was taken, so all of it remains").isEqualTo(6);
        assertThat(body.get("row_limit")).isEqualTo(2);
        assertThat(count(f.sibling())).isZero();
    }

    @Test
    void aSetUnderTheFractionMoves_andTheBoundedDrainIsNeverRefusedHalfway() throws Exception {
        Fixture f = seed(10, 3);

        var first = json(post(req(f, "row_limit", 1, "floor_fraction", 0.5, "floor_min_chunks", 2)));
        assertThat(first.get("refused")).isEqualTo(false);
        assertThat(first.get("moved")).isEqualTo(1);
        assertThat(first.get("remaining")).isEqualTo(2);
        assertThat(first.get("reapable_count")).as("judged before the batch moved").isEqualTo(3);

        for (int i = 0; i < 2; i++) {
            var next = json(post(req(f, "row_limit", 1, "floor_fraction", 0.5, "floor_min_chunks", 2)));
            assertThat(next.get("refused")).as("each batch lowers the ratio, so a drain that began is not stopped").isEqualTo(false);
            assertThat(next.get("moved")).isEqualTo(1);
        }
        assertThat(count(f.sibling())).isEqualTo(3);
        assertThat(count(f.origin())).isEqualTo(7);
    }

    @Test
    void aSetUnderTheMinimumMovesWhateverItsFraction() throws Exception {
        Fixture f = seed(6, 6);

        var body = json(post(req(f, "floor_fraction", 0.1, "floor_min_chunks", 100)));

        assertThat(body.get("refused")).isEqualTo(false);
        assertThat(body.get("moved")).as("100% reapable but under the 100 minimum").isEqualTo(6);
    }

    @Test
    void theComparisonIsStrict_exactlyTheFractionMoves() throws Exception {
        Fixture f = seed(10, 5);

        var body = json(post(req(f, "floor_fraction", 0.5, "floor_min_chunks", 1)));

        assertThat(body.get("refused")).as("5/10 is not MORE than 0.5").isEqualTo(false);
        assertThat(body.get("moved")).isEqualTo(5);
    }

    @Test
    void anOnlyFractionRequestUsesTheGcFamilysMinimumOf100() throws Exception {
        Fixture f = seed(10, 9);

        var body = json(post(req(f, "floor_fraction", 0.25)));

        assertThat(body.get("refused")).as("9 reapable is under the default minimum of 100").isEqualTo(false);
        assertThat(body.get("moved")).isEqualTo(9);
        assertThat(floorOf(body)).containsEntry("min_chunks", 100);
    }

    @Test
    void forceMovesAnOverFloorSet_andWritesNoRefusal() throws Exception {
        Fixture f = seed(10, 8);

        var body = json(post(req(f, "floor_fraction", 0.25, "floor_min_chunks", 3, "force", true)));

        assertThat(body.get("refused")).isEqualTo(false);
        assertThat(body.get("moved")).isEqualTo(8);
        assertThat(floorOf(body)).containsEntry("given", true).containsEntry("force", true);
        assertThat(audit(f.origin(), "gc_quarantine_orphans_refused")).isEmpty();
        assertThat(audit(f.origin(), "gc_quarantine_orphans")).as("the move's own row is still written").hasSize(1);
    }

    @Test
    void noFloorFieldsIsTheUnchangedMove() throws Exception {
        Fixture f = seed(10, 9);

        var unbounded = json(post(req(f)));
        assertThat(unbounded.get("moved")).as("90% reapable and nothing refuses it: no floor was given").isEqualTo(9);
        assertThat(unbounded).doesNotContainKeys("refused", "reapable_count", "total_count", "remaining");
        assertThat(audit(f.origin(), "gc_quarantine_orphans")).hasSize(1);

        Fixture g = seed(10, 9);
        var bounded = json(post(req(g, "row_limit", 4)));
        assertThat(bounded.get("moved")).isEqualTo(4);
        assertThat(bounded.get("remaining")).isEqualTo(5);
        assertThat(bounded).doesNotContainKeys("refused", "reapable_count", "total_count");
        assertThat(audit(g.origin(), "gc_quarantine_orphans_bounded")).hasSize(1);
        assertThat(audit(g.origin(), "gc_quarantine_orphans_refused")).isEmpty();
    }

    @Test
    void everyResponseEchoesTheFloorItApplied_orThatNoneWasGiven() throws Exception {
        Fixture none = seed(4, 2);
        assertThat(floorOf(json(post(req(none))))).as("no floor given").isEqualTo(Map.of("given", false));

        Fixture noneBounded = seed(4, 2);
        assertThat(floorOf(json(post(req(noneBounded, "row_limit", 1)))))
            .as("no floor given, bounded").isEqualTo(Map.of("given", false));

        Fixture floored = seed(10, 6);
        var refused = json(post(req(floored, "floor_fraction", 0.5, "floor_min_chunks", 3)));
        assertThat(floorOf(refused)).as("a refusal echoes the floor it judged")
            .isEqualTo(Map.of("given", true, "fraction", 0.5, "min_chunks", 3, "force", false));

        Fixture ok = seed(10, 2);
        var moved = json(post(req(ok, "floor_fraction", 0.5, "floor_min_chunks", 3)));
        assertThat(floorOf(moved)).as("a move that passed echoes it too")
            .isEqualTo(Map.of("given", true, "fraction", 0.5, "min_chunks", 3, "force", false));
    }

    @Test
    void theFloorIsJudgedUnderTheSweepGateNotBeforeIt() throws Exception {
        // A manifest writer holds the exclusive sweep gate. A floor judged on a snapshot taken before the gate
        // would answer (here: a refusal) at once; judged under it, the call waits for the gate and dies on the
        // 2 s lock bound with lock_not_available instead of answering anything.
        Fixture f = seed(10, 8);
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES)
               .select(DSL.function("pg_advisory_xact_lock", Object.class,
                       DSL.function("hashtext", Integer.class,
                                    DSL.val("sweepgate:" + TENANT + "/" + f.origin()))))
               .fetch();
            Throwable thrown = null;
            try {
                repo.quarantineOrphansFloored(TENANT, f.origin(), f.sibling(), STAMP, 20, null, 0.5, 3, false);
            } catch (RuntimeException ex) {
                thrown = ex;
            }
            holder.rollback();
            assertThat(thrown).as("the over-floor call must wait on the gate, not answer a refusal").isNotNull();
            assertThat(sqlState(thrown)).isEqualTo("55P03");
        }
        assertThat(audit(f.origin(), "gc_quarantine_orphans_refused")).as("it judged nothing").isEmpty();
        assertThat(count(f.origin())).isEqualTo(10);
    }

    // ── request validation ───────────────────────────────────────────────────

    @Test
    void aMalformedFloorIsA400_neverAFloorSilentlyDropped() throws Exception {
        Fixture f = seed(4, 2);
        for (Map<String, Object> bad : List.of(
                Map.<String, Object>of("floor_fraction", 1.5),
                Map.<String, Object>of("floor_fraction", -0.1),
                Map.<String, Object>of("floor_fraction", "half"),
                Map.<String, Object>of("floor_fraction", 0.5, "floor_min_chunks", -1),
                Map.<String, Object>of("floor_fraction", 0.5, "floor_min_chunks", 2.5),
                Map.<String, Object>of("floor_min_chunks", 10),
                Map.<String, Object>of("floor_fraction", 0.5, "force", "yes"))) {
            var kv = new ArrayList<Object>();
            bad.forEach((k, v) -> { kv.add(k); kv.add(v); });
            var r = post(req(f, kv.toArray()));
            assertThat(r.statusCode()).as("%s -> %s", bad, r.body()).isEqualTo(400);
        }
        assertThat(count(f.origin())).as("none of them moved anything").isEqualTo(4);
    }

    private static String sqlState(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }
}

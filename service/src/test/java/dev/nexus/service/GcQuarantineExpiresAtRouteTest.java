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
import dev.nexus.service.vectors.ReaperRepository;
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
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-alsq3: the client's quarantine retention, stamped on each row at move time
 * ({@code nexus.gc_quarantine_orphans_floored}'s {@code p_quarantine_expires_at}, vectors-056-1) and honoured by the
 * engine's expiry of client-moved knowledge quarantine ({@code nexus.reaper_expire_client_quarantine}, vectors-056-2).
 * Over real HTTP through the RLS-subject service role, fixtures by substrate SQL.
 *
 * <p>What each test pins is named by what its revert would do: a route that dropped the field leaves the moved rows
 * unstamped in {@link #theStampRidesEveryFormOfTheMove}; an expiry that ignored the stamp deletes the future-stamped
 * rows in {@link #theEngineExpiresAtTheLaterOfItsRetentionAndTheRowsStamp}; an expiry that read a missing stamp as
 * expired is the same test's unstamped case, and one that read it as never expiring is the same test's other half.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcQuarantineExpiresAtRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-gcexp-tenant-0123456789abcdef000000";
    private static final String SVC_ROLE = "svc_gcexp_route";
    private static final String SVC_PASS = "svc_gcexp_route_pass";
    private static final String TENANT = "gcexp-route";

    /** Far enough in the future that no clock this suite runs on has reached it, and in the past likewise. */
    private static final String FUTURE = "2999-01-01T00:00:00Z";
    private static final String PAST = "2000-01-01T00:00:00Z";
    /** The engine's cutoff in these tests: later than every quarantined_at below except {@link #AFTER_CUTOFF}. */
    private static final String CUTOFF = "2026-06-01T00:00:00Z";
    private static final String AFTER_CUTOFF = "2026-09-01T00:00:00Z";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private PgVectorRepository repo;
    private ReaperRepository reaper;
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
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "gcexp");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        FakeEmbedder embedder = new FakeEmbedder(384);
        var scope = new TenantScope(svcDs);
        repo = new PgVectorRepository(scope, embedder, embedder);
        reaper = new ReaperRepository(scope);
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
        return post(body, null);
    }

    /** As {@link #post(Map)}, with an {@code Idempotency-Key} when {@code key} is given (the keyed, bounded form). */
    private HttpResponse<String> post(Map<String, Object> body, String key) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/gc/quarantine-orphans")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json");
        if (key != null) b.header("Idempotency-Key", key);
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readValue(r.body(), Map.class);
    }

    private static Map<String, Object> req(Fixture f, String quarantinedAt, Object... kv) {
        var m = new LinkedHashMap<String, Object>();
        m.put("collection", f.origin());
        m.put("quarantine_collection", f.sibling());
        m.put("quarantined_at", quarantinedAt);
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

    /** A knowledge origin; the move registers its quarantine sibling from the origin's row. */
    private Fixture newFixture() throws Exception {
        String origin = "knowledge__gcx" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        su(ctx -> PgContainerHelper.insertCollection(ctx, TENANT, origin));
        return new Fixture(origin, "quarantine-" + origin);
    }

    /** {@code n} more chunks in the origin, aged past the grace with no manifest row, so the next move takes them. */
    private List<String> addReapable(Fixture f, String tag, int n) throws Exception {
        List<String> hexes = new ArrayList<>();
        su(ctx -> {
            for (int i = 0; i < n; i++) {
                String hex = Chash.ofText(f.origin() + "/" + tag + i).toHex();
                hexes.add(hex);
                PgContainerHelper.insertChunks(ctx, TENANT, f.origin(), List.of(hex), List.of(tag + " " + i),
                    List.of(new float[384]), List.of(Map.<String, Object>of("title", tag + i)));
            }
            OffsetDateTime then = OffsetDateTime.now().minus(ReapableFixtures.PAST_GRACE);
            for (String hex : hexes) {
                ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, then)
                   .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(f.origin()))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))).execute();
            }
        });
        return hexes;
    }

    /** hex chash to the row's {@code quarantine_expires_at} (null when the key is absent), for one collection. */
    private Map<String, String> stamps(String collection) throws Exception {
        Map<String, String> out = new TreeMap<>();
        su(ctx -> ctx.select(CHUNKS.CHASH, DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "quarantine_expires_at"))
            .from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection)))
            .forEach(r -> out.put(java.util.HexFormat.of().formatHex(r.value1()), r.value2())));
        return out;
    }

    private boolean present(String collection, String hex) throws Exception {
        boolean[] there = new boolean[1];
        su(ctx -> there[0] = ctx.fetchExists(CHUNKS,
            CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        return there[0];
    }

    // ── the move writes the stamp ────────────────────────────────────────────

    @Test
    void theStampRidesEveryFormOfTheMove() throws Exception {
        Fixture f = newFixture();

        List<String> floored = addReapable(f, "floored", 2);
        var a = json(post(req(f, "2026-01-01T00:00:00Z", "floor_fraction", 1.0, "floor_min_chunks", 1000,
            "quarantine_expires_at", "2026-02-01T00:00:00Z")));
        List<String> bounded = addReapable(f, "bounded", 3);
        var b = json(post(req(f, "2026-01-02T00:00:00Z", "row_limit", 2, "quarantine_expires_at", "2026-02-02T00:00:00Z")));
        var b2 = json(post(req(f, "2026-01-02T00:00:00Z", "row_limit", 2, "quarantine_expires_at", "2026-02-02T00:00:00Z")));
        List<String> unbounded = addReapable(f, "unbounded", 2);
        var c = json(post(req(f, "2026-01-03T00:00:00Z", "quarantine_expires_at", "2026-02-03T00:00:00Z")));
        List<String> unstamped = addReapable(f, "unstamped", 2);
        var d = json(post(req(f, "2026-01-04T00:00:00Z")));

        assertThat(a.get("moved")).as("floored form").isEqualTo(2);
        assertThat(b.get("moved")).isEqualTo(2);
        assertThat(b2.get("moved")).as("the second batch of a bounded drain").isEqualTo(1);
        assertThat(c.get("moved")).as("floor-less unbounded form").isEqualTo(2);
        assertThat(d.get("moved")).isEqualTo(2);
        var got = stamps(f.sibling());
        for (String h : floored) assertThat(got).containsEntry(h, "2026-02-01T00:00:00Z");
        for (String h : bounded) assertThat(got).as("every batch of a drain, the first and the last").containsEntry(h, "2026-02-02T00:00:00Z");
        for (String h : unbounded) assertThat(got).containsEntry(h, "2026-02-03T00:00:00Z");
        for (String h : unstamped) assertThat(got).as("no field sent, nothing written").containsEntry(h, null);
    }

    @Test
    void aStampedCallKeepsTheResponseShapeOfTheFormItIs() throws Exception {
        Fixture f = newFixture();
        addReapable(f, "shape", 3);

        var bounded = json(post(req(f, "2026-01-01T00:00:00Z", "row_limit", 2, "quarantine_expires_at", FUTURE)));
        var unbounded = json(post(req(f, "2026-01-01T00:00:00Z", "quarantine_expires_at", FUTURE)));

        assertThat(bounded.keySet()).as("the floor-less bounded body, no refused key")
            .containsExactlyInAnyOrder("moved", "sample", "remaining", "row_limit", "floor");
        assertThat(bounded.get("remaining")).isEqualTo(1);
        assertThat(unbounded.keySet()).containsExactlyInAnyOrder("moved", "sample", "floor");
        @SuppressWarnings("unchecked")
        var floor = (Map<String, Object>) bounded.get("floor");
        assertThat(floor).containsEntry("given", false);
    }

    @Test
    void aMalformedStampIsA400AndNothingMoves() throws Exception {
        Fixture f = newFixture();
        addReapable(f, "bad", 2);

        for (Object bad : new Object[] {"2999-01-01", "2999-01-01T00:00:00+00:00", "2999-01-01T00:00:00.5Z", "", 30, true}) {
            var r = post(req(f, "2026-01-01T00:00:00Z", "quarantine_expires_at", bad));
            assertThat(r.statusCode()).as("stamp %s: %s", bad, r.body()).isEqualTo(400);
        }
        assertThat(stamps(f.sibling())).as("a refused call moves nothing").isEmpty();
    }

    @Test
    void aReMovedChunkDoesNotKeepAStaleStamp() throws Exception {
        Fixture f = newFixture();
        List<String> hexes = addReapable(f, "again", 1);
        json(post(req(f, "2026-01-01T00:00:00Z", "quarantine_expires_at", FUTURE)));
        // Put the same chash back in the live collection (a restore does), then move it again without a stamp.
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, TENANT, f.origin(), List.of(hexes.get(0)), List.of("again 0"),
                List.of(new float[384]), List.of(Map.<String, Object>of("title", "again0")));
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(ReapableFixtures.PAST_GRACE))
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(f.origin()))).execute();
        });

        json(post(req(f, "2026-01-02T00:00:00Z")));

        assertThat(stamps(f.sibling())).as("the second move wrote a row with no stamp").containsEntry(hexes.get(0), null);
    }

    // ── the engine expires at the later of its retention and the stamp ───────

    @Test
    void theEngineExpiresAtTheLaterOfItsRetentionAndTheRowsStamp() throws Exception {
        Fixture f = newFixture();
        // Four tranches, each moved by the route as a client would, all old enough for the engine's cutoff but one.
        List<String> future = addReapable(f, "future", 2);
        json(post(req(f, "2026-01-01T00:00:00Z", "quarantine_expires_at", FUTURE)));
        List<String> past = addReapable(f, "past", 2);
        json(post(req(f, "2026-01-02T00:00:00Z", "quarantine_expires_at", PAST)));
        List<String> none = addReapable(f, "none", 2);
        json(post(req(f, "2026-01-03T00:00:00Z")));
        List<String> young = addReapable(f, "young", 2);
        json(post(req(f, AFTER_CUTOFF, "quarantine_expires_at", PAST)));

        var out = reaper.expireClientMoved(TENANT, f.sibling(), f.origin(), CUTOFF, 5000, 25_000, 2_000);

        assertThat(out.expired()).as("the past-stamped pair and the unstamped pair").isEqualTo(4);
        for (String h : future) assertThat(present(f.sibling(), h)).as("stamp in the future: kept past the engine's cutoff").isTrue();
        for (String h : past) assertThat(present(f.sibling(), h)).as("both the cutoff and the stamp have passed: expired").isFalse();
        for (String h : none) assertThat(present(f.sibling(), h)).as("no stamp: the engine's retention alone, expired").isFalse();
        for (String h : young) assertThat(present(f.sibling(), h))
            .as("the stamp has passed but the engine's cutoff has not: the later of the two keeps it").isTrue();
    }

    @Test
    void aStampThatIsNotAnInstantIsNoStamp_theRowExpiresAtTheEnginesRetentionNeverLater() throws Exception {
        Fixture f = newFixture();
        String old = Chash.ofText(f.sibling() + "/hand-edited-old").toHex();
        String young = Chash.ofText(f.sibling() + "/hand-edited-young").toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, f.sibling());
            for (String[] row : new String[][] {{old, "2026-01-01T00:00:00Z"}, {young, AFTER_CUTOFF}}) {
                PgContainerHelper.insertChunks(ctx, TENANT, f.sibling(), List.of(row[0]), List.of("hand edited"),
                    List.of(new float[384]), List.of(Map.<String, Object>of(
                        "quarantined_at", row[1], "origin_collection", f.origin(),
                        "quarantine_expires_at", "tomorrow")));
            }
        });

        var out = reaper.expireClientMoved(TENANT, f.sibling(), f.origin(), CUTOFF, 5000, 25_000, 2_000);

        assertThat(out.expired()).as("a stamp that is not an instant is no stamp: the engine default applies").isEqualTo(1);
        assertThat(present(f.sibling(), old)).as("past the engine's cutoff: expired, the bad stamp does not hold it").isFalse();
        assertThat(present(f.sibling(), young)).as("a bad stamp never expires a row before the engine's cutoff").isTrue();
    }

    // ── the stamp reaches only the rows this call moved ──────────────────────

    @Test
    void theStampIsWrittenOnTheRowsThisCallMovedAndOnNoOtherRowOfTheSibling() throws Exception {
        Fixture f = newFixture();
        // An earlier, unstamped move with the SAME quarantined_at and origin: indistinguishable by metadata from this
        // call's rows, so a stamp that scanned the sibling by metadata would take it. A victim-set join does not.
        List<String> earlier = addReapable(f, "earlier", 2);
        json(post(req(f, "2026-01-01T00:00:00Z")));
        List<String> mine = addReapable(f, "mine", 2);

        json(post(req(f, "2026-01-01T00:00:00Z", "quarantine_expires_at", "2026-02-01T00:00:00Z")));

        var got = stamps(f.sibling());
        for (String h : mine) assertThat(got).containsEntry(h, "2026-02-01T00:00:00Z");
        for (String h : earlier) assertThat(got).as("moved by another call: not this call's to stamp").containsEntry(h, null);
    }

    @Test
    void theBoundedDrainStampsEachBatchsRowsFromThatBatchsVictimSet() throws Exception {
        Fixture f = newFixture();
        addReapable(f, "drain", 5);

        var first = json(post(req(f, "2026-01-01T00:00:00Z", "row_limit", 2, "quarantine_expires_at", "2026-02-01T00:00:00Z")));
        assertThat(stamps(f.sibling()).values()).as("only the first batch's two rows are stamped").containsOnly("2026-02-01T00:00:00Z");
        assertThat(stamps(f.sibling())).hasSize(2);
        var second = json(post(req(f, "2026-01-01T00:00:00Z", "row_limit", 2, "quarantine_expires_at", "2026-03-01T00:00:00Z")));

        assertThat(first.get("moved")).isEqualTo(2);
        assertThat(second.get("moved")).isEqualTo(2);
        var got = stamps(f.sibling());
        assertThat(got).hasSize(4);
        assertThat(got.values()).as("two rows per batch, each with its own batch's stamp; the first batch's rows are not restamped")
            .containsExactlyInAnyOrder("2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
                "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z");
    }

    // ── the keyed (Idempotency-Key) form, which is the one the client's bounded move takes ──

    @Test
    void aKeyedBoundedMoveCarriesTheStamp_withOrWithoutAFloor_andItsReplayReturnsTheStoredBody() throws Exception {
        Fixture f = newFixture();
        addReapable(f, "keyed-nofloor", 3);
        String key1 = java.util.UUID.randomUUID().toString();
        var request1 = req(f, "2026-01-01T00:00:00Z", "row_limit", 2, "quarantine_expires_at", "2026-02-01T00:00:00Z");

        var r1 = post(request1, key1);
        var body1 = json(r1);

        assertThat(r1.headers().firstValue("Idempotency-Key")).contains(key1);
        assertThat(body1.keySet()).as("a stamped keyed call without a floor keeps the floor-less bounded shape")
            .containsExactlyInAnyOrder("moved", "sample", "remaining", "row_limit", "floor");
        assertThat(body1.get("moved")).isEqualTo(2);
        assertThat(body1.get("remaining")).isEqualTo(1);
        var after1 = stamps(f.sibling());
        assertThat(after1).hasSize(2);
        assertThat(after1.values()).as("the keyed branch passed the stamp to the function").containsOnly("2026-02-01T00:00:00Z");

        var replay = post(request1, key1);

        assertThat(replay.headers().firstValue("Idempotency-Replayed")).contains("true");
        assertThat(json(replay)).as("the replay is the stored body, field for field").isEqualTo(body1);
        assertThat(stamps(f.sibling())).as("and moved and stamped nothing more").isEqualTo(after1);

        // The same keyed branch with a floor in the request.
        addReapable(f, "keyed-floored", 2);
        String key2 = java.util.UUID.randomUUID().toString();
        var request2 = req(f, "2026-01-02T00:00:00Z", "row_limit", 10, "floor_fraction", 1.0, "floor_min_chunks", 1000,
            "quarantine_expires_at", "2026-03-01T00:00:00Z");
        var body2 = json(post(request2, key2));
        assertThat(body2).as("the floored shape carries the floor echo and the refused key").containsKeys("refused", "floor");
        var replay2 = post(request2, key2);
        assertThat(replay2.headers().firstValue("Idempotency-Replayed")).contains("true");
        assertThat(json(replay2)).isEqualTo(body2);
        assertThat(stamps(f.sibling()).values()).contains("2026-03-01T00:00:00Z");
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_768;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bbtc2 (vectors-060): {@code nexus.text_gate_probe_by_collection_<dim>}, the probe that answers per collection,
 * against {@code nexus.text_gate_probe_<dim>} run one collection at a time, which is what a lexical arm ran before.
 *
 * <p>Fixture, one tenant and four collections, each holding {@code n} rows with the token {@value #TOKEN} (and filler
 * that does not): {@code small} 4, {@code edge} 5, {@code big} 9, {@code none} 0, and a second tenant holding the token in
 * a collection of the same name. The cap is 5, so {@code small} is one under it (every chash comes back), {@code edge}
 * is exactly at it and {@code big} past it (one {@code (collection, NULL)} row each), the boundary the engine's
 * {@code selectiveGateMax + 1} sets. Runs as {@code nexus_svc} in the shared superuser-migrated container (the definer
 * posture).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TextGateProbeByCollectionIntegrationTest {

    static final String TENANT = "bbtc2-bycol";
    static final String OTHER_TENANT = "bbtc2-bycol-other";
    static final String MODEL = "minilm-l6-v2-384";
    static final String SMALL = "knowledge__bbtc2-bc-small__minilm-l6-v2-384__v1";
    static final String EDGE = "knowledge__bbtc2-bc-edge__minilm-l6-v2-384__v1";
    static final String BIG = "knowledge__bbtc2-bc-big__minilm-l6-v2-384__v1";
    static final String NONE = "knowledge__bbtc2-bc-none__minilm-l6-v2-384__v1";
    /** 3 live and 4 unowned token rows: 7 rows to the gate, 3 to a reader. */
    static final String MIXED = "knowledge__bbtc2-bc-mixed__minilm-l6-v2-384__v1";
    /** 4 token rows, none owned (no live owner): absent from every answer. */
    static final String GHOST = "knowledge__bbtc2-bc-ghost__minilm-l6-v2-384__v1";
    static final String TOKEN = "quuxbycoltoken";
    static final int CAP = 5;
    static final String TOKEN_WHERE_EN = "{\"lang\": \"en\"}";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;
    /** The token rows of each collection that have a live owner, hex chashes in seed order. */
    final Map<String, List<String>> liveIds = new TreeMap<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            for (String c : List.of(SMALL, EDGE, BIG, NONE, MIXED, GHOST)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
            PgContainerHelper.insertCollection(dsl, OTHER_TENANT, SMALL);
        }
        seed(TENANT, SMALL, 4, Map.of());
        seed(TENANT, EDGE, 5, Map.of());
        seed(TENANT, BIG, 9, Map.of("lang", "en"));
        seed(TENANT, NONE, 0, Map.of());
        seed(OTHER_TENANT, SMALL, 3, Map.of());
        seedOwned(TENANT, MIXED, 7, 3, Map.of());
        seedOwned(TENANT, GHOST, 4, 0, Map.of());
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** {@code n} rows holding the token and {@code 3} filler rows, all in {@code collection}, all owned. */
    private void seed(String tenant, String collection, int n, Map<String, Object> tokenMeta) {
        seedOwned(tenant, collection, n, n, tokenMeta);
    }

    /** As {@link #seed}, but only the first {@code owned} token rows (and the filler) get a live owner. */
    private void seedOwned(String tenant, String collection, int n, int owned, Map<String, Object> tokenMeta) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n + 3; i++) {
            String text = i < n ? "row " + i + " of " + collection + " holds " + TOKEN + " here"
                                : "filler " + i + " of " + collection + " alpha bravo";
            embedder.register(text, 1f, 0f);
            ids.add(Chash.ofText(text).toHex());
            texts.add(text);
            metas.add(i < n ? tokenMeta : Map.of());
        }
        repo.upsertChunks(tenant, collection, ids, texts, metas);
        List<String> live = new ArrayList<>(ids.subList(0, owned));
        live.addAll(ids.subList(n, ids.size()));
        liveIds.put(collection, new ArrayList<>(ids.subList(0, owned)));
        scope.withTenant(tenant, ctx -> {
            PgContainerHelper.ownChunks(ctx, tenant, collection, live.toArray(new String[0]));
            return null;
        });
    }

    private <T> T asTenant(String guc, Function<DSLContext, T> body) {
        return asTenant(guc, false, body);
    }

    /**
     * {@code forceParallel} sets {@code debug_parallel_query = on} for the transaction, so the function's step-1
     * statement runs under a Gather whatever its cost: the tenant GUC the gate reads and the owner policy are then
     * evaluated by the workers (a worker that lost either would return no row, and the comparisons below are not empty).
     */
    private <T> T asTenant(String guc, boolean forceParallel, Function<DSLContext, T> body) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold", "0.6");
            if (guc != null) PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, guc);
            if (forceParallel) {
                ctx.select(DSL.function("set_config", org.jooq.impl.SQLDataType.VARCHAR,
                    DSL.val("debug_parallel_query"), DSL.val("on"), DSL.inline(true))).fetch();
                ctx.select(DSL.function("set_config", org.jooq.impl.SQLDataType.VARCHAR,
                    DSL.val("max_parallel_workers_per_gather"), DSL.val("2"), DSL.inline(true))).fetch();
            }
            return body.apply(ctx);
        });
    }

    private int count(Table<?> fn, String guc) {
        return asTenant(guc, ctx -> ctx.selectFrom(fn).fetch().size());
    }

    /** What the arm of {@code collection} got before vectors-060: the one-collection probe's chashes, hex, sorted. */
    private List<String> perArm(String collection, String text, String guc, int cap, String where) {
        Table<?> fn = TEXT_GATE_PROBE_384.call(text, new String[] {collection}, where == null ? null : org.jooq.JSONB.valueOf(where),
            null, cap, MODEL, TENANT);
        return asTenant(guc, ctx -> new ArrayList<>(new TreeSet<>(ctx.selectFrom(fn).fetch().getValues(0, byte[].class)
            .stream().map(b -> HexFormat.of().formatHex(b)).toList())));
    }

    /** The by-collection function's rows grouped by collection: hex chashes sorted, or null for the dense marker. */
    private Map<String, List<String>> byCollection(String text, String guc, int cap, String where,
                                                    String... collections) {
        return byCollection(text, guc, false, cap, where, collections);
    }

    private Map<String, List<String>> byCollection(String text, String guc, boolean forceParallel, int cap,
                                                    String where, String... collections) {
        Table<?> fn = TEXT_GATE_PROBE_BY_COLLECTION_384.call(text, collections,
            where == null ? null : org.jooq.JSONB.valueOf(where), null, cap, MODEL, TENANT);
        Result<? extends Record> rows = asTenant(guc, forceParallel, ctx -> ctx.selectFrom(fn).fetch());
        Map<String, List<String>> chashes = new TreeMap<>();
        java.util.Set<String> dense = new java.util.HashSet<>();
        for (Record r : rows) {
            String c = r.get(0, String.class);
            byte[] h = r.get(1, byte[].class);
            if (h == null) {
                assertThat(dense.add(c)).as("one dense marker per collection").isTrue();
            } else {
                chashes.computeIfAbsent(c, k -> new ArrayList<>()).add(HexFormat.of().formatHex(h));
            }
        }
        for (String c : dense) {
            assertThat(chashes).as("a dense collection sends no chash").doesNotContainKey(c);
        }
        Map<String, List<String>> out = new TreeMap<>();
        chashes.forEach((c, hs) -> out.put(c, new ArrayList<>(new TreeSet<>(hs))));
        for (String c : dense) {
            out.put(c, null);
        }
        return out;
    }

    @Test
    void underTheCapEveryChashComes_atOrPastItOneMarker_noRowAtAllWhereTheGateAdmitsNothing() {
        for (String guc : new String[] {"substring", "on", null}) {
            Map<String, List<String>> got = byCollection(TOKEN, guc, CAP, null, SMALL, EDGE, BIG, NONE);
            assertThat(got.keySet()).as("guc=%s: a collection with no gated row is absent", guc)
                .containsExactlyInAnyOrder(SMALL, EDGE, BIG);
            assertThat(got.get(SMALL)).as("guc=%s: 4 rows under a cap of 5", guc)
                .isEqualTo(perArm(SMALL, TOKEN, guc, 100, null)).hasSize(4);
            assertThat(got.get(EDGE)).as("guc=%s: exactly 5 rows at a cap of 5 is dense, as a per-arm probe's 5001st row", guc)
                .isNull();
            assertThat(got.get(BIG)).as("guc=%s: 9 rows is dense", guc).isNull();
        }
    }

    @Test
    void aSelectiveCollectionsRowsAreExactlyItsOwnProbes_inEveryMode() {
        // 'off' is the FTS half alone: the token has no English stem, plainto_tsquery still matches the exact lexeme.
        for (String guc : new String[] {"off", "substring", "on", null}) {
            Map<String, List<String>> got = byCollection(TOKEN, guc, 100, null, SMALL, EDGE, BIG, NONE);
            for (String c : List.of(SMALL, EDGE, BIG)) {
                assertThat(got.get(c)).as("guc=%s collection=%s", guc, c).isEqualTo(perArm(c, TOKEN, guc, 100, null));
            }
            assertThat(got).as("guc=%s", guc).doesNotContainKey(NONE);
        }
        assertThat(byCollection(TOKEN, "off", 100, null, SMALL).get(SMALL))
            .as("non-vacuity: the 'off' branch admits the rows it should").hasSize(4);
    }

    @Test
    void theSubstringBranch_admitsAFragmentTheFtsHalfMisses() {
        Map<String, List<String>> got = byCollection("bycoltok", "substring", 100, null, SMALL);
        assertThat(got.get(SMALL)).hasSize(4);
        assertThat(byCollection("bycoltok", "off", 100, null, SMALL)).as("the 'off' branch has no substring half").isEmpty();
    }

    @Test
    void theWhereFilterIsPerRow() {
        String where = "{\"lang\": \"en\"}";
        Map<String, List<String>> got = byCollection(TOKEN, "substring", 100, where, SMALL, EDGE, BIG);
        assertThat(got.keySet()).as("only BIG's token rows carry lang=en").containsExactly(BIG);
        assertThat(got.get(BIG)).isEqualTo(perArm(BIG, TOKEN, "substring", 100, where)).hasSize(9);
    }

    @Test
    void anotherTenantsRowsAreNotVisible() {
        // OTHER_TENANT holds 3 token rows in a collection named like SMALL; the calling tenant's own 4 are all it sees.
        assertThat(byCollection(TOKEN, "substring", 100, null, SMALL).get(SMALL)).hasSize(4);
        // Naming the other tenant while the session's tenant is TENANT fails closed: the tenant GUC decides.
        Table<?> fn = TEXT_GATE_PROBE_BY_COLLECTION_384.call(TOKEN, new String[] {SMALL}, null, null, 100, MODEL,
                                                              OTHER_TENANT);
        assertThat(count(fn, "substring")).isZero();
    }

    @Test
    void theOtherTwoDimensionsRunInEveryMode() {
        // An empty leaf plans and runs every statement of the body, so a malformed branch fails here.
        for (String guc : new String[] {"off", "substring", "on", null}) {
            Table<?> f768 = TEXT_GATE_PROBE_BY_COLLECTION_768.call("anything", new String[] {"x__y__bge-768__v1"}, null, null,
                CAP, "bge-768", TENANT);
            Table<?> f1024 = TEXT_GATE_PROBE_BY_COLLECTION_1024.call("anything", new String[] {"x__y__voyage-1024__v1"},
                null, null, CAP, "voyage-1024", TENANT);
            assertThat(count(f768, guc)).as("768, guc=%s", guc).isZero();
            assertThat(count(f1024, guc)).as("1024, guc=%s", guc).isZero();
            // and the plain probes of those dimensions, which share the posture the new ones read
            Table<?> p768 = TEXT_GATE_PROBE_768.call("anything", new String[] {"x"}, null, null, CAP, "bge-768", TENANT);
            Table<?> p1024 = TEXT_GATE_PROBE_1024.call("anything", new String[] {"x"}, null, null, CAP, "voyage-1024", TENANT);
            assertThat(count(p768, guc)).isZero();
            assertThat(count(p1024, guc)).isZero();
        }
    }

    // ── liveness (nexus-bbtc2 fix round 1): the function applies the probes' own predicate ──────────────────

    @Test
    void aRowWithNoLiveOwnerIsNotReturned_andDoesNotCountTowardTheCap() {
        // MIXED holds 7 gated rows, 3 live: under a cap of 5 it is NOT dense (3 live rows), and it returns exactly the
        // live chashes. Without the liveness EXISTS the gate's 7 rows would reach the cap and the answer would be a dense
        // marker. GHOST holds 4 gated rows and no live one: it is absent, where an unfiltered 4 rows would be returned.
        for (String guc : new String[] {"substring", "on", null}) {
            Map<String, List<String>> got = byCollection(TOKEN, guc, CAP, null, MIXED, GHOST, SMALL);
            assertThat(got.keySet()).as("guc=%s: a collection with no live gated row is absent", guc)
                .containsExactlyInAnyOrder(MIXED, SMALL);
            assertThat(got.get(MIXED)).as("guc=%s: only the live rows, not dense", guc)
                .containsExactlyElementsOf(new TreeSet<>(liveIds.get(MIXED))).hasSize(3);
            assertThat(got.get(MIXED)).as("guc=%s: the per-arm probe says the same", guc)
                .isEqualTo(perArm(MIXED, TOKEN, guc, 100, null));
            assertThat(perArm(GHOST, TOKEN, guc, 100, null)).as("guc=%s: the per-arm probe agrees GHOST has none", guc)
                .isEmpty();
        }
    }

    // ── the parallel path (a Gather runs the step-1 statement in workers) ───────────────────────────────────

    @Test
    void underAForcedGather_everyModeReturnsTheRowsItReturnsSerially() {
        for (String guc : new String[] {"off", "substring", "on", null}) {
            Map<String, List<String>> serial = byCollection(TOKEN, guc, false, 100, null, SMALL, EDGE, BIG, MIXED, GHOST);
            Map<String, List<String>> parallel = byCollection(TOKEN, guc, true, 100, null, SMALL, EDGE, BIG, MIXED, GHOST);
            assertThat(serial.keySet()).as("non-vacuity, guc=%s: the leaf has gated rows to compare", guc)
                .containsExactlyInAnyOrder(SMALL, EDGE, BIG, MIXED);
            assertThat(parallel).as("guc=%s: the same collections and chashes under debug_parallel_query", guc)
                .isEqualTo(serial);
            // the dense markers too
            assertThat(byCollection(TOKEN, guc, true, CAP, null, SMALL, EDGE, BIG, MIXED))
                .as("guc=%s: the same dense/selective split", guc)
                .isEqualTo(byCollection(TOKEN, guc, false, CAP, null, SMALL, EDGE, BIG, MIXED));
        }
        assertThat(byCollection(TOKEN, "substring", true, 100, TOKEN_WHERE_EN, BIG)).as("a where under a Gather")
            .isEqualTo(byCollection(TOKEN, "substring", false, 100, TOKEN_WHERE_EN, BIG)).hasSize(1);
    }

    // ── the gather's ceiling (a custom setting lowers it for a test; the engine answers per-arm on it) ──────

    @Test
    void aGateThatAdmitsMoreRowsThanTheCeilingRaisesProgramLimitExceeded() {
        Table<?> fn = TEXT_GATE_PROBE_BY_COLLECTION_384.call(TOKEN, new String[] {SMALL, EDGE, BIG}, null, null, 100, MODEL,
                                                              TENANT);
        // SMALL, EDGE and BIG hold 4 + 5 + 9 = 18 gated rows: a ceiling of 17 is passed, a ceiling of 18 is not.
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> scope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, "substring");
            ctx.select(DSL.function("set_config", org.jooq.impl.SQLDataType.VARCHAR,
                DSL.val("nexus.gate_probe_max_rows"), DSL.val("17"), DSL.inline(true))).fetch();
            return ctx.selectFrom(fn).fetch();
        })).isInstanceOfSatisfying(org.jooq.exception.DataAccessException.class, e ->
            assertThat(e.sqlState()).as("a distinct, class-54 error the engine reads as 'too dense to share'")
                .isEqualTo("54000"));
        Result<? extends Record> atTheCeiling = scope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, "substring");
            ctx.select(DSL.function("set_config", org.jooq.impl.SQLDataType.VARCHAR,
                DSL.val("nexus.gate_probe_max_rows"), DSL.val("18"), DSL.inline(true))).fetch();
            return ctx.selectFrom(fn).fetch();
        });
        assertThat(atTheCeiling).as("exactly the ceiling is not past it").hasSize(18);
    }

    // ── what the six functions are, in the catalog ───────────────────────────────────────────────────────────

    @Test
    void theCatalogShapeOfTheThreeFunctions_isTheIntendedOne() throws Exception {
        try (Connection su = pg.createConnection("")) {
            TextGateProbeByCollectionShape.assertShape(DSL.using(su, org.jooq.SQLDialect.POSTGRES), true);
        }
    }
}

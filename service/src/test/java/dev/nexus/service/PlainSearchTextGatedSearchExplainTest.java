// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.Query;
import org.jooq.Table;
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
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_1024;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-zrcj7 — EXPLAIN-based inlining/index-engagement proof for
 * {@code nexus.plain_search_&lt;dim&gt;} (vectors-009) and {@code
 * nexus.text_gated_search_by_chash_&lt;dim&gt;}/{@code
 * nexus.text_gated_search_hnsw_first_&lt;dim&gt;} (vectors-011), the schema functions
 * that retire {@link PgVectorRepository#searchWithTokens}/
 * {@link PgVectorRepository#hybridSearch}'s raw-SQL StringBuilder assembly.
 * EXPLAIN is run via jOOQ's {@code DSLContext#explain} (T2 critic follow-up
 * 2026-09-04: no SQL strings in tests either), never a raw JDBC {@code EXPLAIN}
 * string.
 *
 * <p>Mirrors {@code CombinedQueryParityTest}'s GROUP 3 EXPLAIN discipline (a {@code
 * Function Scan} node means the function is not inlinable — LANGUAGE sql required, not
 * plpgsql) and {@code HybridSelectiveGateTest}'s precedent that a SELECTIVE text gate must
 * never touch the HNSW index (the nexus-lcogi starvation class).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PlainSearchTextGatedSearchExplainTest {

    private static final String TENANT = "zrcj7-explain";
    private static final String SVC_ROLE = "svc_zrcj7_explain";
    private static final String SVC_PASS = "svc_zrcj7_explain_pass";
    private static final String COLL = "knowledge__zrcj7-explain__voyage-context-3__v1"; // 1024
    private static final String TOKEN = "zrcj7raretoken";
    private static final int FILLER = 200;  // vector-closest to the query, no token
    private static final int TARGETS = 4;   // farthest from the query, carry the token

    // Dense/non-selective-gate probe (nexus-zrcj7 pushback item 2): a SEPARATE collection
    // where MOST rows carry the token, so the text gate is non-selective. DENSE_MATCHING
    // is deliberately > SELECTIVE_GATE_MAX (5000) -- the retired Java hybridSearch's own
    // selective/HNSW-first dispatch threshold -- so this fixture exercises the regime
    // where OLD code would have taken the HNSW-first branch, not the (still materializing)
    // selective branch a smaller dense gate would share with the new function trivially.
    private static final String DENSE_COLL = "knowledge__zrcj7-explain-dense__voyage-context-3__v1";
    private static final String DENSE_TOKEN = "zrcj7densetoken";
    private static final int DENSE_ROWS = 6000;
    private static final int DENSE_MATCHING = 5500;  // > PgVectorRepository.SELECTIVE_GATE_MAX (5000)

    // ORDER/distance parity fixture (nexus-zrcj7 pushback item 3): hybridSearch's exact
    // ordered ids + numeric distance values, checked against an INDEPENDENT hand-computed
    // oracle (analytic cosine distance) rather than "the retired Java path" -- that path
    // is deleted, so there is nothing live to diff against; the oracle is the closest
    // faithful reading of "byte-identical to the retired Java path" now available: the
    // retired path's OWN documented contract was "rank gate survivors by exact cosine
    // distance ASC" (T2 [24207]), which an analytic oracle checks directly and exactly,
    // not merely approximately via a second implementation that could share a bug.
    private static final String ORDER_COLL = "knowledge__zrcj7-explain-order__voyage-context-3__v1";
    private static final String ORDER_TOKEN = "zrcj7ordertoken";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    final List<String> targetChashes = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // EXECUTE ON FUNCTION is not part of bootstrapServiceRole's fixed grant
            // set -- kept as an explicit grant (nexus-cbo4a batch 1b).
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.plain_search_1024, "
                + "nexus.text_gate_probe_1024, nexus.text_gated_search_hnsw_first_1024, "
                + "nexus.text_gated_search_by_chash_1024 TO " + SVC_ROLE);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seedFixtures() throws Exception {
        embedder.register(TOKEN, 1.0f, 0.0f);  // query vector points at the filler cluster

        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();

        for (int i = 0; i < FILLER; i++) {
            String text = "common filler document number " + i + " alpha bravo charlie";
            embedder.register(text, 1.0f, 0.0f);
            ids.add(chash("zrcj7fill", i));
            texts.add(text);
            metas.add(Map.of());
        }
        for (int i = 0; i < TARGETS; i++) {
            String text = TOKEN + " selective gate target row " + i;
            embedder.register(text, -1.0f, 0.0f);
            String c = chash("zrcj7target", i);
            targetChashes.add(c);
            ids.add(c);
            texts.add(text);
            metas.add(Map.of());
        }

        repo.upsertChunks(TENANT, COLL, ids, texts, metas);
        seedDenseGateFixture();
        seedOrderParityFixture();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    /**
     * Dense/non-selective-gate fixture (nexus-zrcj7 pushback item 2): {@link #DENSE_MATCHING}
     * of {@link #DENSE_ROWS} rows carry {@link #DENSE_TOKEN} — a NON-selective text gate,
     * the regime the retired Java hybridSearch's HNSW-first branch existed for. Seeded in a
     * separate collection so it does not perturb the selective-gate fixture above.
     */
    private void seedDenseGateFixture() throws Exception {
        embedder.register(DENSE_TOKEN, 1.0f, 0.0f);
        List<String> ids = new ArrayList<>(DENSE_ROWS);
        List<String> texts = new ArrayList<>(DENSE_ROWS);
        List<Map<String, Object>> metas = new ArrayList<>(DENSE_ROWS);
        for (int i = 0; i < DENSE_ROWS; i++) {
            double theta = -0.3 + 0.6 * i / (DENSE_ROWS - 1);
            boolean matches = i < DENSE_MATCHING;
            String text = (matches ? DENSE_TOKEN + " " : "") + "dense fixture row " + i
                + " alpha bravo charlie delta";
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash("zrcj7dense", i));
            texts.add(text);
            metas.add(Map.of());
        }
        for (int from = 0; from < DENSE_ROWS; from += 300) {
            int to = Math.min(DENSE_ROWS, from + 300);
            repo.upsertChunks(TENANT, DENSE_COLL, ids.subList(from, to), texts.subList(from, to),
                              metas.subList(from, to));
        }
    }

    /**
     * ORDER/distance parity fixture (nexus-zrcj7 pushback item 3): three gate-matching
     * chunks at ANALYTICALLY KNOWN angles from the query vector (30/60/90 degrees), plus
     * filler at 5 degrees (vector-closest, no token — must be excluded by the gate). Unit
     * 2-D vectors embedded into dim 1024 (rest zero), so pgvector's cosine distance
     * {@code <=>} is exactly {@code 1 - cos(theta)} — no floating-point embedding noise to
     * account for, only pgvector's own float8 arithmetic (checked to 1e-4).
     */
    private void seedOrderParityFixture() throws Exception {
        embedder.register(ORDER_TOKEN, 1.0f, 0.0f);
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();

        for (double thetaDeg : new double[] {30, 60, 90}) {
            double theta = Math.toRadians(thetaDeg);
            String text = ORDER_TOKEN + " order-parity target " + (int) thetaDeg + "deg";
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash("zrcj7order", (int) thetaDeg));
            texts.add(text);
            metas.add(Map.of());
        }
        // Filler: 5 degrees from the query (closer than any target) but carries no token —
        // must never appear, proving the gate (not raw vector proximity) governs presence.
        double fillerTheta = Math.toRadians(5);
        String fillerText = "order-parity filler no token alpha bravo";
        embedder.register(fillerText, (float) Math.cos(fillerTheta), (float) Math.sin(fillerTheta));
        ids.add(chash("zrcj7orderfiller", 0));
        texts.add(fillerText);
        metas.add(Map.of());

        repo.upsertChunks(TENANT, ORDER_COLL, ids, texts, metas);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // plain_search_1024: inlinable, HNSW index survives EXPLAIN (vectors-009)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void explain_plainSearch_usesHnswIndex_notFunctionScan() {
        Table<?> fn = PLAIN_SEARCH_1024.call(vec(1.0, 0.0), colls(COLL), null, null, 10);
        String plan = explain(ctx -> ctx.select(fn.field("id")).from(fn));
        assertThat(plan)
            .as("plain_search_1024 must use the HNSW index idx_chunks_embedding_1024 for "
                + "the ANN ordering — the vector is a plan-time argument and the function "
                + "inlines. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("a Function Scan node means the function is not inlinable (plpgsql) — "
                + "vectors-009 must use an inlinable LANGUAGE sql function. Plan was:%n%s",
                plan)
            .doesNotContain("Function Scan");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // text_gated_search_by_chash_1024 (vectors-011): the selective rank must NOT
    // re-evaluate the text gate (T2 [24219] critique finding B, single-gate-eval
    // restored) and must NOT naturally touch the HNSW index -- both retargeted here
    // (T2 critic follow-up, 2026-09-04) from the deleted text_gated_search_1024
    // (vectors-010, a single materializing-CTE design with zero production callers
    // once this dispatch existed). text_gated_search_by_chash_1024 has a different
    // plan SHAPE than the deleted function's row_number()-over-the-gate CTE (a bare
    // chash = ANY(...) filter + ORDER BY/LIMIT, no window function), so the retired
    // test's positive "WindowAgg" assertion does not carry over -- only the negative
    // "never reaches HNSW" claim does.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void explain_textGatedSearchByChash_selectiveRank_neverTouchesHnsw_noTrigramOperator_notFunctionScan() {
        // targetChashes come from the SAME selective fixture the retired
        // text_gated_search_1024 EXPLAIN test used -- hybridSearch's own probe would
        // have fetched exactly this chash set for this fixture (below
        // SELECTIVE_GATE_MAX). The rank query below takes those chashes directly,
        // exactly as hybridSearch's Java dispatch now does.
        //
        // Deliberately the WEAKER explainSeqscanOff() (enable_seqscan off only), not
        // the stronger explain(): this is a NEGATIVE assertion ("HNSW must never be
        // reachable"), so forcing every OTHER access path away too would
        // adversarially strip the plan's own natural, correct, safe alternative (a
        // cheap chash-membership lookup over ~4 matched rows) — proving nothing about
        // what the planner actually chooses under real cost pressure (the same
        // discipline the retired text_gated_search_1024 test used).
        Table<?> fn = TEXT_GATED_SEARCH_BY_CHASH_1024.call(
            vec(1.0, 0.0), chashes(targetChashes), colls(COLL), null, null, 50);
        String plan = explainSeqscanOff(ctx -> ctx.select(fn.field("id")).from(fn));
        assertThat(plan)
            .as("a Function Scan node means the function is not inlinable (plpgsql) — "
                + "vectors-011 must use an inlinable LANGUAGE sql function. Plan was:%n%s",
                plan)
            .doesNotContain("Function Scan");
        assertThat(plan)
            .as("text_gated_search_by_chash_1024's rank must NOT NATURALLY touch the "
                + "HNSW index (the nexus-lcogi starvation class this design closes) "
                + "under normal cost pressure (enable_seqscan=off only). Plan was:%n%s",
                plan)
            .doesNotContain("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("text_gated_search_by_chash_1024's plan must carry NO trigram `<%` "
                + "operator -- the gate is not re-evaluated here, only chash = ANY(...) "
                + "plus scope. Plan was:%n%s", plan)
            .doesNotContain("<%");
    }

    @Test
    void textGatedSearchByChash_selectiveRank_returnsExactMatches_viaPublicApi() {
        // Behavioral companion, through hybridSearch's actual Java dispatch (which
        // calls text_gated_search_by_chash_1024 for the selective branch -- T2 [24219]
        // finding B).
        List<Map<String, Object>> rows = repo.hybridSearch(TENANT, TOKEN, List.of(COLL), 50, null);
        List<String> ids = rows.stream().map(r -> (String) r.get("id")).toList();
        assertThat(ids)
            .as("hybridSearch's selective branch (probe -> text_gated_search_by_chash_1024) "
                + "must return exactly the token-bearing targets, excluding vector-closest "
                + "filler with no text signal")
            .containsExactlyInAnyOrderElementsOf(targetChashes);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-zrcj7 pushback item 2 (coordinator, post-completion review): the original
    // task instructed EITHER preserving the lcogi/x7z7l two-branch dispatch (selective
    // chash-rank vs HNSW-first) inside the new function, OR proving with EXPLAIN evidence
    // that a single function keeps the index engaged on BOTH the selective AND
    // non-selective gate regimes. text_gated_search_<dim>'s single materializing CTE was
    // proven NOT to reach the HNSW index for a dense gate (T2 nexus/finding-zrcj7-dense-
    // gate-hnsw-not-preserved [24216]) -- FIXED, not accepted as a known gap: vectors-011
    // restores the lcogi/x7z7l two-branch dispatch as three generated schema functions
    // (text_gate_probe_<dim>, text_gated_search_<dim> unchanged for the selective path,
    // text_gated_search_hnsw_first_<dim> for the dense path). This test now proves the
    // HNSW-first function DOES reach the index for the exact same dense fixture the
    // earlier (retired) test proved the single-function design could not.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void explain_textGatedSearchHnswFirst_denseGate_usesHnswIndex_notFunctionScan() {
        // DENSE_MATCHING (5500) is deliberately chosen ABOVE
        // PgVectorRepository.SELECTIVE_GATE_MAX (5000) -- the retired-and-now-restored
        // selective/HNSW-first dispatch threshold -- so this fixture exercises exactly
        // the regime where hybridSearch's Java dispatch routes to
        // text_gated_search_hnsw_first_1024, not the selective by-chash function.
        Table<?> fn = TEXT_GATED_SEARCH_HNSW_FIRST_1024.call(
            vec(1.0, 0.0), DENSE_TOKEN, colls(DENSE_COLL), null, null, 50);
        String plan = explain(ctx -> ctx.select(fn.field("id")).from(fn));
        assertThat(plan)
            .as("text_gated_search_hnsw_first_1024 must use the HNSW index "
                + "idx_chunks_embedding_1024 for a dense/non-selective gate -- this is the "
                + "restored lcogi/x7z7l HNSW-first branch, the bare ORDER BY/LIMIT shape "
                + "(no CTE, no window function) that keeps the index reachable exactly as "
                + "the retired raw SQL did. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("a Function Scan node means the function is not inlinable (plpgsql) — "
                + "vectors-011 must use an inlinable LANGUAGE sql function. Plan was:%n%s",
                plan)
            .doesNotContain("Function Scan");
    }

    @Test
    void hybridSearch_denseGate_dispatchesToHnswFirst_viaPublicApi() {
        // Behavioral companion: the production PgVectorRepository#hybridSearch API,
        // given a gate PAST SELECTIVE_GATE_MAX, must dispatch to the HNSW-first branch
        // and still return exactly the token-bearing matches ranked by distance —
        // the probe/dispatch is a Java-side count comparison, invisible from the
        // caller's perspective except via the plan shape the EXPLAIN test above pins.
        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, DENSE_TOKEN, List.of(DENSE_COLL), 5, null);
        assertThat(rows)
            .as("hybridSearch must return results for a dense gate via the HNSW-first "
                + "branch, not silently collapse to empty")
            .hasSize(5);
        assertThat(rows)
            .as("every returned row must carry the dense token — the HNSW-first branch's "
                + "gate filter is load-bearing, not a vector passthrough")
            .allSatisfy(r -> assertThat((String) r.get("content")).contains(DENSE_TOKEN));
    }

    @Test
    void textGatedSearch_selectiveGate_returnsExactMatches_viaPublicApi() {
        // Behavioral companion to the EXPLAIN proof above, through the production
        // PgVectorRepository#hybridSearch API (which dispatches to text_gate_probe_1024
        // then text_gated_search_by_chash_1024 for the selective case -- T2 [24219]
        // finding B; see textGatedSearchByChash_selectiveRank_returnsExactMatches_
        // viaPublicApi below for the dedicated coverage of that exact path): the
        // selective gate (filler is vector-closest but carries no token) must return
        // exactly the token-bearing targets.
        List<Map<String, Object>> rows = repo.hybridSearch(TENANT, TOKEN, List.of(COLL), 50, null);
        List<String> ids = rows.stream().map(r -> (String) r.get("id")).toList();
        assertThat(ids)
            .as("the selective dispatch must return exactly the token-bearing gate, "
                + "excluding vector-closest filler with no text signal")
            .containsExactlyInAnyOrderElementsOf(targetChashes);
    }

    @Test
    void hybridSearch_orderAndDistanceParity_matchesAnalyticCosineOracle() {
        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, ORDER_TOKEN, List.of(ORDER_COLL), 50, null);

        List<String> ids = rows.stream().map(r -> (String) r.get("id")).toList();
        assertThat(ids)
            .as("hybridSearch must return exactly the three token-bearing targets in "
                + "ASCENDING distance order (30deg, 60deg, 90deg) — the vector-closest "
                + "5deg filler carries no token and must never appear despite being "
                + "nearer than all three")
            .containsExactly(
                chash("zrcj7order", 30), chash("zrcj7order", 60), chash("zrcj7order", 90));

        // Distance VALUES, not just order: 1 - cos(theta) for each analytically-known angle.
        double[] expectedDistances = {
            1 - Math.cos(Math.toRadians(30)),
            1 - Math.cos(Math.toRadians(60)),
            1 - Math.cos(Math.toRadians(90)),
        };
        for (int i = 0; i < rows.size(); i++) {
            double got = ((Number) rows.get(i).get("distance")).doubleValue();
            assertThat(got)
                .as("row %d (id=%s) distance must match the analytic cosine-distance oracle "
                    + "1 - cos(theta) to within float8 tolerance", i, ids.get(i))
                .isCloseTo(expectedDistances[i], org.assertj.core.data.Offset.offset(1e-4));
        }
    }

    @Test
    void textGatedSearchHnswFirst_orderAndDistanceParity_matchesAnalyticCosineOracle() {
        // Same oracle as hybridSearch_orderAndDistanceParity_matchesAnalyticCosineOracle
        // above, called DIRECTLY against text_gated_search_hnsw_first_1024 (vectors-011)
        // rather than through hybridSearch's Java dispatch -- the ORDER_COLL fixture (4
        // rows) is far below SELECTIVE_GATE_MAX, so hybridSearch itself would route to
        // the selective function, never exercising the HNSW-first function's OWN
        // correctness independent of when Java chooses to call it (coordinator: "add the
        // same oracle against the hnsw-first function").
        Table<?> fn = TEXT_GATED_SEARCH_HNSW_FIRST_1024.call(
            vec(1.0, 0.0), ORDER_TOKEN, colls(ORDER_COLL), null, null, 50);
        List<String> ids = new ArrayList<>();
        List<Double> distances = new ArrayList<>();
        tenantScope.withTenant(TENANT, ctx -> {
            for (var rec : ctx.select(fn.field("id"), fn.field("distance")).from(fn).fetch()) {
                ids.add(rec.get(0, String.class));
                distances.add(rec.get(1, Double.class));
            }
            return null;
        });
        assertThat(ids)
            .as("text_gated_search_hnsw_first_1024 must return exactly the three "
                + "token-bearing targets in ASCENDING distance order (30deg, 60deg, "
                + "90deg) — the vector-closest 5deg filler carries no token and must "
                + "never appear despite being nearer than all three")
            .containsExactly(
                chash("zrcj7order", 30), chash("zrcj7order", 60), chash("zrcj7order", 90));

        double[] expectedHnswFirstDistances = {
            1 - Math.cos(Math.toRadians(30)),
            1 - Math.cos(Math.toRadians(60)),
            1 - Math.cos(Math.toRadians(90)),
        };
        for (int i = 0; i < distances.size(); i++) {
            assertThat(distances.get(i))
                .as("row %d (id=%s) distance must match the analytic cosine-distance oracle "
                    + "1 - cos(theta) to within float8 tolerance", i, ids.get(i))
                .isCloseTo(expectedHnswFirstDistances[i], org.assertj.core.data.Offset.offset(1e-4));
        }
    }

    /**
     * EXPLAIN (via jOOQ's {@code DSLContext#explain}, T2 critic follow-up 2026-09-04:
     * no SQL strings in tests either -- Sam's rule) with every non-index-scan access
     * path penalized so the HNSW-ordered scan is reachable at fixture scale
     * (CombinedQueryParityTest's own {@code explain()} discipline, GROUP 3's comment):
     * {@code enable_seqscan}/{@code enable_bitmapscan}/{@code enable_sort}/
     * {@code enable_hashjoin} off, via {@link PgSession#setLocal} (the sanctioned,
     * SQL-string-free {@code SET LOCAL} form -- {@code enable_hashjoin} added to its
     * allowlist for this test class). {@code enable_nestloop} stays ON — the
     * HNSW-ordered chunk scan joins to the tombstone-check subqueries via nested loop;
     * disabling it would defeat the very plan this proof asserts. A stronger disabling
     * set than {@code enable_seqscan} alone is needed here (unlike
     * HybridSelectiveGateTest's identically-named helper, which only needs to prove
     * HNSW is NEVER reachable — a weaker GUC set suffices for a negative assertion):
     * plain_search_1024's tombstone Anti Join adds enough cost that, at this fixture's
     * ~200-row scale, a Sort over a plain index scan on {@code idx_chunks_tenant_chash}
     * otherwise costs less than the HNSW-ordered scan.
     */
    private String explain(Function<DSLContext, ? extends Query> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> {
            for (String guc : List.of("enable_seqscan", "enable_bitmapscan",
                    "enable_sort", "enable_hashjoin")) {
                PgSession.setLocal(ctx, guc, "off");
            }
            return ctx.explain(queryBuilder.apply(ctx)).plan();
        });
    }

    /** EXPLAIN with ONLY seqscan disabled — see the negative-assertion rationale on
     * {@link #explain_textGatedSearchByChash_selectiveRank_neverTouchesHnsw_noTrigramOperator_notFunctionScan}. */
    private String explainSeqscanOff(Function<DSLContext, ? extends Query> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "enable_seqscan", "off");
            return ctx.explain(queryBuilder.apply(ctx)).plan();
        });
    }

    /** Full 64-hex chunk id deterministically derived from prefix + index (RDR-180). */
    private static String chash(String prefix, int i) {
        return dev.nexus.service.db.Chash.ofText(prefix + i).toHex();
    }

    /** 1024-dim pgvector value with first two components (x, y), rest 0. */
    private static Vector vec(double x, double y) {
        float[] v = new float[1024];
        v[0] = (float) x;
        v[1] = (float) y;
        return Vector.of(v);
    }

    /** Single-collection array, the shape every {@code p_collections} parameter expects. */
    private static String[] colls(String collection) {
        return new String[] {collection};
    }

    /** Hex chash strings decoded to the {@code bytea[]} shape {@code p_chashes} expects. */
    private static byte[][] chashes(List<String> hexChashes) {
        return hexChashes.stream().map(hex -> HexFormat.of().parseHex(hex)).toArray(byte[][]::new);
    }
}

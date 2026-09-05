// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 P4 follow-on, bead nexus-lcogi — regression suite for the selective-text-gate
 * collapse in {@link PgVectorRepository#hybridSearch}.
 *
 * <p>The bug (found by the conexus xr7.8.9 production gate, cloud 116k chunks): the prior
 * single-query plan ran {@code ORDER BY embedding <=> q} on the HNSW index with the text
 * gate as a SCAN FILTER. For a SELECTIVE gate (a few matches in a large corpus), the
 * matches rank deeper than {@code hnsw.max_scan_tuples} from the query vector, so the HNSW
 * scan terminates before reaching them and the endpoint returns 0 rows (full recall costs
 * ~95s, past every HTTP timeout). The fix is a TEXT-FIRST plan: a {@code MATERIALIZED} CTE
 * gates via the GIN indexes first, then ranks the (small) gated set by exact distance —
 * no dependence on {@code hnsw.max_scan_tuples}.
 *
 * <p><strong>What this suite can and cannot prove.</strong> The runtime collapse is a
 * production-scale (&gt;20k-row) phenomenon: at container scale Postgres cheaply uses the
 * collection index + sort (or seqscans) and never picks the HNSW-first plan, so the
 * collapse cannot be reproduced faithfully here — that production-scale recall
 * verification is owned by the conexus xr7.8.9 gate (per the bead). What IS deterministic
 * at fixture scale:
 * <ul>
 *   <li>{@link #explain_textFirstPlan_gatesViaGin_neverRanksViaHnsw} — the SELECTIVE-gate
 *       plan shape: gate the chashes via the GIN indexes (once), rank as a Sort over the
 *       chash-filtered set, the HNSW index ({@code idx_chunks_embedding_1024}) never
 *       touched. This is the scale-independent structural core of the fix (it is a
 *       transcription of the two-query gate-then-rank SQL the dispatch builds for a
 *       selective gate — the behavioral tests below anchor that the production method
 *       actually produces these results).</li>
 *   <li>{@link #hybridSearch_textFirst_returnsAllSelectiveMatches} /
 *       {@link #hybridSearch_textFirst_excludesNonGatedFiller} — behavioral: the production
 *       {@code hybridSearch} returns the COMPLETE selective gate even when the matches are
 *       the FARTHEST vectors, and excludes vector-closest non-gated filler. (These pass on
 *       the pre-fix impl too at fixture scale — they are correctness/regression anchors,
 *       not the discriminating proof; that is the conexus gate's.)</li>
 * </ul>
 * No real embeddings: a {@code FakeEmbedder} pins both the stored and query vectors so the
 * geometry is exact.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HybridSelectiveGateTest {

    private static final String TENANT = "selgate-tenant";
    private static final String SVC_ROLE = "svc_selgate_test";
    private static final String SVC_PASS = "svc_selgate_pass";
    private static final String COLL = "knowledge__selgate__voyage-context-3__v1";   // 1024

    // Rare token present ONLY in the target rows — a selective gate.
    private static final String TOKEN = "ztokenxyz";
    private static final String QUERY = TOKEN;
    private static final int FILLER = 250;   // near the query vector, NO token
    private static final int TARGETS = 5;    // farthest from the query, carry the token

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
        embedder.register(QUERY, 1.0f, 0.0f);   // query vector points at the filler cluster

        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();

        // Filler: near the query vector (distance 0), NO token → not in the gate.
        for (int i = 0; i < FILLER; i++) {
            String text = "common filler document number " + i + " alpha bravo charlie";
            embedder.register(text, 1.0f, 0.0f);
            ids.add(chash("selfill", i));
            texts.add(text);
            metas.add(Map.of());
        }
        // Targets: FARTHEST from the query (distance 2.0), carry the rare token → the
        // entire selective gate. A vector-biased plan reaches them last (or never).
        for (int i = 0; i < TARGETS; i++) {
            String text = TOKEN + " selective gate target row " + i;
            embedder.register(text, -1.0f, 0.0f);
            String c = chash("seltarget", i);
            targetChashes.add(c);
            ids.add(c);
            texts.add(text);
            metas.add(Map.of());
        }

        repo.upsertChunks(TENANT, COLL, ids, texts, metas);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // Structural proof: the rank no longer routes through the HNSW index, so the
    // hnsw.max_scan_tuples budget cannot starve a selective gate.
    //
    // The runtime collapse itself is a production-scale (>20k-row) phenomenon — at
    // container scale Postgres cheaply seqscans, so the bad plan cannot be forced
    // faithfully; the production-scale recall verification is owned by the conexus
    // xr7.8.9 gate (per the bead). What IS deterministic here is the PLAN SHAPE: the
    // retired plan ranks via the HNSW index (idx_chunks_embedding_1024) with the gate as
    // a filter (→ starvable); the text-first plan gates via the GIN indexes and sorts the
    // materialized set, never touching the HNSW index (→ unstarvable).
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void explain_textFirstPlan_gatesViaGin_neverRanksViaHnsw() throws Exception {
        // The two-query shape PgVectorRepository.hybridSearch builds for a selective gate
        // (single-gate-eval, nexus-x7z7l): (1) a bounded fetch of the gate's chashes via
        // TEXT_GATE_PROBE_1024 (the GIN/trigram probe), then (2) a rank of those exact
        // chashes by cosine distance via TEXT_GATED_SEARCH_BY_CHASH_1024 — the text gate
        // (<% trigram recheck) is evaluated ONCE, in query 1, not re-run at rank time.
        //
        // UPDATED (nexus-cbo4a batch 3, critic follow-up T2 critique-cbo4a-batch3):
        // these two queries now call the SAME generated function tables hybridSearch()'s
        // selective branch actually dispatches to (vectors-011, nexus-zrcj7) — the
        // former hand-transcribed raw SQL mirrored a two-query Java-built shape that was
        // retired the day before this suite's raw-SQL cleanup ran, so the "MAINTENANCE:
        // hand TRANSCRIPTION" comment this replaces was already testing a query shape
        // production no longer executes. The behavioral tests below anchor that
        // production actually returns the right rows at fixture scale; production-scale
        // recall is the conexus xr7.8.9 gate's.
        Table<?> gateFn = TEXT_GATE_PROBE_1024.call(TOKEN, new String[] {COLL}, null, null, TARGETS + 1);
        String gatePlan = explain(gateFn);
        assertThat(gatePlan)
            .as("the gate MUST be evaluated via the GIN text indexes (tsv/trgm), once. "
                + "Plan was:%n%s", gatePlan)
            .containsAnyOf("idx_chunks_tsv", "idx_chunks_trgm", "Bitmap Index Scan");

        byte[][] chashes = targetChashes.stream()
            .map(c -> Chash.fromHex(c).toBytes())
            .toArray(byte[][]::new);
        Table<?> rankFn = TEXT_GATED_SEARCH_BY_CHASH_1024.call(
            queryVector(), chashes, new String[] {COLL}, null, null, 50);
        String rankPlan = explain(rankFn);
        // The rank filters by chash (PK) and sorts by exact distance — the ORDER BY embedding
        // can never be pushed into an HNSW index scan (which is what hnsw.max_scan_tuples
        // starves). Scale-independent structural core of the lcogi fix; the production-scale
        // recall collapse only manifests at >20k rows (the conexus xr7.8.9 gate owns that).
        assertThat(rankPlan)
            .as("rank MUST NOT route through the HNSW index — that is what made the retired "
                + "plan starvable by hnsw.max_scan_tuples. Plan was:%n%s", rankPlan)
            .doesNotContain("idx_chunks_embedding_1024");
        assertThat(rankPlan)
            .as("the rank MUST be a Sort over the chash-filtered set (exact distance), "
                + "not an index-ordered scan. Plan was:%n%s", rankPlan)
            .contains("Sort");
    }

    /** 1024-dim typed pgvector value: first component 1.0, rest zero — the query vector
     *  the FakeEmbedder points at the filler cluster (same shape as the retired {@code
     *  vec(1.0, 0.0)} literal helper). */
    private static Vector queryVector() {
        float[] v = new float[1024];
        v[0] = 1.0f;
        return Vector.of(v);
    }

    /** EXPLAIN a typed jOOQ table-function call, seqscan disabled so index access paths
     *  are chosen at fixture scale (nexus-cbo4a batch 3). */
    private String explain(Table<?> fn) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            su.createStatement().execute("SET LOCAL enable_seqscan = off");
            // nexus-cbo4a batch 3 critic follow-up: TEXT_GATE_PROBE_1024's own gate
            // predicate now also carries the SAME live-chunks (tombstone dead-set)
            // anti-join every sibling combined-query function does (vectors-011's own
            // header: "byte-for-byte the same gate predicate the retired Java gate/
            // scope builders used"), which the retired hand-transcribed raw SQL this
            // helper replaced did not include. At this fixture's tiny scale (255 rows,
            // near-empty manifest tables) the added anti-join makes a plain Index Scan
            // on idx_chunks_tenant_chash cheaper than a Bitmap Index Scan via the GIN
            // text indexes, so enable_indexscan must ALSO be forced off -- matching
            // CatalogFtsFilenameSearchTest's own established technique for the same
            // "prove the GIN index, not a btree, serves the text gate" claim -- or the
            // gate assertion below stops being a meaningful proof at this scale.
            su.createStatement().execute("SET LOCAL enable_indexscan = off");
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String plan = ctx.explain(ctx.selectFrom(fn)).plan();
            su.rollback();
            return plan;
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // The fix: text-first hybridSearch returns the COMPLETE selective gate.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_textFirst_returnsAllSelectiveMatches() {
        List<Map<String, Object>> rows = repo.hybridSearch(TENANT, QUERY, List.of(COLL), 50, null);
        List<String> ids = rows.stream().map(r -> (String) r.get("id")).toList();
        assertThat(ids)
            .as("text-first hybridSearch must return ALL %d selective-gate matches even "
                + "though they are the FARTHEST vectors from the query — the gate is "
                + "applied via the GIN bitmap first, then ranked by exact distance, with "
                + "no hnsw.max_scan_tuples dependence", TARGETS)
            .containsExactlyInAnyOrderElementsOf(targetChashes);
    }

    @Test
    void hybridSearch_nonSelectiveBranch_returnsSameGateSet() {
        // Force the NON-selective (HNSW-first) dispatch branch via the package-private
        // threshold overload (count of 5 matches > threshold 3). The fixture is the same;
        // the branch differs. Both branches are semantically equivalent — the dispatch is
        // a performance choice — so the HNSW-first branch must return the identical gate
        // set. (At fixture scale max_scan_tuples=20k scans all rows, so it does not
        // collapse; this covers the branch the 16 selective contract tests never exercise.)
        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, QUERY, List.of(COLL), 50, null, 3);
        List<String> ids = rows.stream().map(r -> (String) r.get("id")).toList();
        assertThat(ids)
            .as("non-selective HNSW-first branch must return the same complete gate set as "
                + "the text-first branch — dispatch is performance, not semantics")
            .containsExactlyInAnyOrderElementsOf(targetChashes);
    }

    @Test
    void hybridSearch_nonPositiveThreshold_rejected() {
        // A non-positive selectiveGateMax would route every gate to HNSW-first and
        // silently re-enable the collapse — it must fail loud, not mis-dispatch.
        org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                repo.hybridSearch(TENANT, QUERY, List.of(COLL), 50, null, 0))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("selectiveGateMax");
    }

    @Test
    void hybridSearch_textFirst_excludesNonGatedFiller() {
        // Non-vacuity for the gate: the 250 filler rows are vector-CLOSEST (distance 0)
        // but carry no token, so a working text gate excludes every one of them.
        List<Map<String, Object>> rows = repo.hybridSearch(TENANT, QUERY, List.of(COLL), 50, null);
        assertThat(rows)
            .as("filler rows (vector-closest, no token) must never appear — the text gate "
                + "is load-bearing, not a vector passthrough")
            .allSatisfy(r -> assertThat(targetChashes).contains((String) r.get("id")));
        assertThat(rows).as("exactly the gate set, nothing else").hasSize(TARGETS);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Full 64-hex chunk id deterministically derived from prefix + index (RDR-180:
     *  chunks_&lt;dim&gt;.chash is bytea(32) — the full sha256 digest). */
    private static String chash(String prefix, int i) {
        return dev.nexus.service.db.Chash.ofText(prefix + i).toHex();
    }
}

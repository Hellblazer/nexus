// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import org.jooq.SQLDialect;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bbtc2: the lexical leg of {@link PgVectorRepository#searchPerCollection} runs the text-gate probe once for all
 * the request's collections ({@link dev.nexus.service.vectors.SharedLexicalProbe}) instead of once per collection arm,
 * and must return exactly what the per-arm path returns: the same rows in the same order, the same per-collection ids.
 * Each test runs one request twice, with {@code sharedLexicalProbe} off (the per-arm path, today's behaviour) and on,
 * and compares everything the response carries; then reads the request's log line to see WHICH shared path ran, so a
 * run that silently fell back to the per-arm path fails the test instead of passing it for free.
 *
 * <p>Fixture, tenant {@value #TENANT}, six collections of 384-dim rows, one query vector. Each has plain filler rows
 * near the query and token rows ({@code zebracorn}) spread in distance, so the per-collection ranking is not a trivial
 * order: {@code S1} 3 token rows, {@code S2} 5, {@code S3} 2, {@code D1} 12 (dense under a cutoff of 8), {@code Z}
 * none, and {@code SH1}/{@code SH2} holding the SAME text with different metadata (one chash in two collections: the
 * case where one collection's chash set is a superset of another's own).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LexicalSharedProbeIdentityIntegrationTest {

    static final String TENANT = "bbtc2-share";
    static final String S1 = "knowledge__bbtc2-s1__minilm-l6-v2-384__v1";
    static final String S2 = "knowledge__bbtc2-s2__minilm-l6-v2-384__v1";
    static final String S3 = "knowledge__bbtc2-s3__minilm-l6-v2-384__v1";
    static final String D1 = "knowledge__bbtc2-d1__minilm-l6-v2-384__v1";
    static final String Z = "knowledge__bbtc2-z__minilm-l6-v2-384__v1";
    static final String SH1 = "knowledge__bbtc2-sh1__minilm-l6-v2-384__v1";
    static final String SH2 = "knowledge__bbtc2-sh2__minilm-l6-v2-384__v1";
    static final List<String> ALL = List.of(S1, S2, S3, D1, Z, SH1, SH2);
    static final String ONE_TOKEN = "zebracorn";
    static final String TWO_WORDS = "zebracorn stripe";
    static final String SHARED_ROW = "shared zebracorn stripe row";
    static final int K = 20;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;
    /** nexus-bbtc2 fix round 1: a pool whose sessions carry a gather ceiling of 3 rows, so the shared probe raises. */
    HikariDataSource tinyCeilingDs;
    PgVectorRepository tinyCeilingRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(ONE_TOKEN, 1f, 0f);
        embedder.register(TWO_WORDS, 1f, 0f);
        repo = new PgVectorRepository(scope, embedder, embedder);
        var tinyCfg = new HikariConfig();
        tinyCfg.setJdbcUrl(pg.getJdbcUrl());
        tinyCfg.setUsername(PgContainerHelper.SVC_USERNAME);
        tinyCfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        tinyCfg.setMaximumPoolSize(8);
        tinyCfg.setAutoCommit(true);
        // The by-collection function reads this custom setting (vectors-060); a startup value lowers its ceiling.
        tinyCfg.addDataSourceProperty("options", "-c nexus.gate_probe_max_rows=3");
        tinyCeilingDs = new HikariDataSource(tinyCfg);
        tinyCeilingRepo = new PgVectorRepository(new TenantScope(tinyCeilingDs), embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : ALL) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        seed(S1, 3, Map.of("lang", "en"));
        seed(S2, 5, Map.of("lang", "en"));
        seed(S3, 2, Map.of("lang", "en"));
        seed(D1, 12, Map.of("lang", "en"));
        seed(Z, 0, Map.of());
        seed(SH1, 1, Map.of("lang", "en"));
        seed(SH2, 1, Map.of("lang", "fr"));
        // The same text in two collections, a different metadata row in each.
        seedShared(SH1, Map.of("lang", "en"));
        seedShared(SH2, Map.of("lang", "fr"));
    }

    @AfterAll
    void stopAll() {
        if (tinyCeilingDs != null) tinyCeilingDs.close();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** {@code tokenRows} rows holding the token at varied distances from the query, plus ten filler rows near it. */
    private void seed(String collection, int tokenRows, Map<String, Object> meta) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            add(ids, texts, metas, "filler " + i + " in " + collection + " alpha", i * 0.002, Map.of());
        }
        for (int i = 0; i < tokenRows; i++) {
            // spread: some inside the vector top-K, some far outside it, none at the same distance as another
            add(ids, texts, metas, "row " + i + " zebracorn stripe in " + collection, 0.05 + i * 0.11, meta);
        }
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        own(collection, ids);
    }

    private void seedShared(String collection, Map<String, Object> meta) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        add(ids, texts, metas, SHARED_ROW, 0.3, meta);
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        own(collection, ids);
    }

    private void add(List<String> ids, List<String> texts, List<Map<String, Object>> metas, String text, double theta,
                     Map<String, Object> meta) {
        embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
        ids.add(Chash.ofText(text).toHex());
        texts.add(text);
        metas.add(meta);
    }

    private void own(String collection, List<String> ids) {
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
    }

    private static FanoutSettings settings(boolean shared, int selectiveGateMax) {
        return new FanoutSettings(4, 60_000, 60_000, selectiveGateMax, shared);
    }

    /** The same with the probe's workers named, so a test does not depend on the process environment. */
    private static FanoutSettings settings(boolean shared, int selectiveGateMax, int probeWorkers) {
        return new FanoutSettings(4, 60_000, 60_000, selectiveGateMax, shared, probeWorkers);
    }

    /**
     * One request, the {@code shared_probe=} word and the {@code shared_dense=} count its lexical log line carries, and
     * the fallback reason the probe logged ({@code null} when it did not fall back), and the {@code probe_workers=} word
     * (the setting the shared probe ran with, or {@code none}).
     */
    private record Run(PerCollectionResult result, String sharedProbe, int sharedDense, String fallbackReason,
                       String probeWorkers) {}

    private Run run(String query, List<String> cols, Map<String, Object> where, boolean shared, int selectiveGateMax) {
        return run(repo, query, cols, where, shared, selectiveGateMax);
    }

    private Run run(PgVectorRepository on, String query, List<String> cols, Map<String, Object> where, boolean shared,
                    int selectiveGateMax) {
        return run(on, query, cols, where, settings(shared, selectiveGateMax));
    }

    private Run run(PgVectorRepository on, String query, List<String> cols, Map<String, Object> where,
                    FanoutSettings settings) {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(PgVectorRepository.class);
        var probeLogger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
            "dev.nexus.service.vectors.LexicalGateProbe");
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        probeLogger.addAppender(logs);
        PerCollectionResult r;
        try {
            r = on.searchPerCollection(TENANT, query, cols, K, 1200, null, where, false, false, true,
                                       settings);
        } finally {
            probeLogger.detachAppender(logs);
            logger.detachAppender(logs);
            logs.stop();
        }
        String line = logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=search_per_collection_lexical ")).findFirst().orElseThrow();
        String fallback = logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=search_per_collection_shared_probe_fallback ")).findFirst().orElse(null);
        return new Run(r, word(line, "shared_probe="), Integer.parseInt(word(line, "shared_dense=")),
                       fallback == null ? null : word(fallback, "reason="), word(line, "probe_workers="));
    }

    /** The space-delimited value after {@code key} in a log line (to its end for the last field). */
    private static String word(String line, String key) {
        String rest = line.substring(line.indexOf(key) + key.length());
        int end = rest.indexOf(' ');
        return end < 0 ? rest : rest.substring(0, end);
    }

    /** Both paths over one request: they must agree on everything the response carries; returns the shared one. */
    private Run identical(String query, List<String> cols, Map<String, Object> where, int selectiveGateMax,
                          String expectedSharedPath) {
        return identical(repo, query, cols, where, selectiveGateMax, expectedSharedPath);
    }

    private Run identical(PgVectorRepository sharedOn, String query, List<String> cols, Map<String, Object> where,
                          int selectiveGateMax, String expectedSharedPath) {
        Run perArm = run(repo, query, cols, where, false, selectiveGateMax);
        Run shared = run(sharedOn, query, cols, where, true, selectiveGateMax);
        assertThat(perArm.sharedProbe()).as("the per-arm path ran no shared probe").isEqualTo("off");
        assertThat(shared.sharedProbe()).as("which path the shared run took (a fallback would pass the rest vacuously)")
            .isEqualTo(expectedSharedPath);
        PerCollectionResult a = perArm.result();
        PerCollectionResult b = shared.result();
        assertThat(b.lexicalRows()).as("lexical rows, in order").isEqualTo(a.lexicalRows());
        assertThat(b.lexical()).as("per-collection lexical ids and counts").isEqualTo(a.lexical());
        assertThat(b.rows()).as("vector rows").isEqualTo(a.rows());
        assertThat(b.perCollection()).as("per-collection vector stats").isEqualTo(a.perCollection());
        return shared;
    }

    private static int lexicalHits(PerCollectionResult r) {
        return r.lexical().stream().mapToInt(PgVectorRepository.LexicalStat::count).sum();
    }

    // ── under the cutoff: no collection is dense, every arm ranks its own chashes

    @Test
    void underTheCutoff_oneProbeServesEveryArm_andTheRowsAreThePerArmRows_oneToken() {
        Run shared = identical(ONE_TOKEN, ALL, null, 5000, "per_collection");
        // non-vacuity: the lexical leg returned rows from several collections, not an empty comparison
        assertThat(lexicalHits(shared.result())).isGreaterThan(20);
        assertThat(shared.result().lexical().stream().filter(s -> s.count() > 0).count()).isGreaterThanOrEqualTo(5);
    }

    @Test
    void underTheCutoff_twoWordsTakeTheTrigramBranch_andAgree() {
        Run shared = identical(TWO_WORDS, ALL, null, 5000, "per_collection");
        assertThat(lexicalHits(shared.result())).isGreaterThan(20);
    }

    @Test
    void oneChashInTwoCollectionsWithDifferentMetadata_isFilteredPerRowNotPerChash() {
        Map<String, Object> en = Map.of("lang", "en");
        Run shared = identical(ONE_TOKEN, List.of(SH1, SH2, S1), en, 5000, "per_collection");
        // SH2 holds the shared text too, but its copy is lang=fr: the chash is in the shared set (SH1's copy passes the
        // gate and the where), and SH2's arm must still not return it.
        int sh2 = shared.result().perCollection().stream().map(s -> s.collection()).toList().indexOf(SH2);
        assertThat(shared.result().lexical().get(sh2).ids()).isEmpty();
        int sh1 = shared.result().perCollection().stream().map(s -> s.collection()).toList().indexOf(SH1);
        assertThat(shared.result().lexical().get(sh1).ids()).contains(Chash.ofText(SHARED_ROW).toHex());
    }

    @Test
    void aCollectionWhoseGateAdmitsNothing_returnsNothing_asItDidBefore() {
        Run shared = identical(ONE_TOKEN, List.of(Z, S1), null, 5000, "per_collection");
        int z = shared.result().perCollection().stream().map(s -> s.collection()).toList().indexOf(Z);
        assertThat(shared.result().lexical().get(z).ids()).isEmpty();
        assertThat(shared.result().lexical().get(z).error()).isNull();
    }

    // ── past the cutoff: the dense collections rank HNSW-first ───────────────

    @Test
    void pastTheCutoff_theByCollectionProbeServesEveryArm_denseCollectionsRankHnswFirst() {
        // 22 token rows over all seven, cutoff 8: S1 (3 rows), S2 (5), S3 (2), SH1/SH2 (2 each) are selective and D1
        // (12 rows) is dense.
        Run shared = identical(ONE_TOKEN, ALL, null, 8, "per_collection");
        assertThat(lexicalHits(shared.result())).isGreaterThan(20);
    }

    @Test
    void pastTheCutoff_twoWords() {
        Run shared = identical(TWO_WORDS, ALL, null, 8, "per_collection");
        assertThat(lexicalHits(shared.result())).isGreaterThan(20);
    }

    @Test
    void pastTheCutoff_withAWhere() {
        identical(ONE_TOKEN, ALL, Map.of("lang", "en"), 8, "per_collection");
    }

    @Test
    void everyCollectionDense_everyArmRanksHnswFirst() {
        // cutoff 1: every collection with 2 or more gated rows is dense; the by-collection probe answers all dense/selective
        identical(ONE_TOKEN, ALL, null, 1, "per_collection");
    }

    // ── the cutoff boundary: a collection with exactly selectiveGateMax gated rows is selective, one more is dense ─────

    @Test
    void aCollectionWithExactlyTheCutoffsRowsIsSelective_oneMoreIsDense_atEveryBoundary() {
        // ONE_TOKEN gated rows per collection: D1 12, S2 5, S1 3, S3 2, SH1 2, SH2 2, Z 0. The per-arm probe counts a
        // collection dense when it returns more than selectiveGateMax rows; the shared probe must name exactly those.
        // A cap off by one in either direction moves a collection across the line and changes shared_dense.
        int[][] cutoffAndDense = {{12, 0}, {11, 1}, {5, 1}, {4, 2}, {3, 2}, {2, 3}, {1, 6}};
        for (int[] c : cutoffAndDense) {
            Run shared = identical(ONE_TOKEN, ALL, null, c[0], "per_collection");
            assertThat(shared.sharedDense()).as("cutoff %d: collections with more than %d gated rows", c[0], c[0])
                .isEqualTo(c[1]);
        }
    }

    // ── the shared probe fails: every arm runs its own probe, and says why ──────────────────────────────────

    @Test
    void aProbeThatRaisesItsCeiling_fallsBack_everyArmRunsItsOwnProbe_andTheRowsAreThePerArmRows() {
        // The pool behind tinyCeilingRepo sets nexus.gate_probe_max_rows = 3 and the leaf's gate admits 22 rows, so the
        // function raises SQLSTATE 54000; the arms then probe for themselves (their own probes do not read the ceiling).
        Run shared = identical(tinyCeilingRepo, ONE_TOKEN, ALL, null, 5000, "fallback");
        assertThat(shared.fallbackReason()).as("the reason the probe logged").isEqualTo("gate_ceiling");
        assertThat(shared.sharedDense()).as("a fallback names no dense collection").isZero();
        assertThat(lexicalHits(shared.result())).as("non-vacuity: the lexical leg still answered").isGreaterThan(20);
        Run again = identical(tinyCeilingRepo, ONE_TOKEN, ALL, Map.of("lang", "en"), 8, "fallback");
        assertThat(again.fallbackReason()).isEqualTo("gate_ceiling");
        // and the same leaf with the ordinary pool does not fall back
        assertThat(run(ONE_TOKEN, ALL, null, true, 5000).fallbackReason()).isNull();
    }

    // ── off switch, and the cases that never share ───────────────────────────

    @Test
    void oneCollection_runsNoSharedProbe() {
        Run run = run(ONE_TOKEN, List.of(S1), null, true, 5000);
        assertThat(run.sharedProbe()).isEqualTo("off");
        assertThat(lexicalHits(run.result())).isEqualTo(3);
    }

    // ── the log line names the workers the shared probe ran with ─────────────────

    @Test
    void theLexicalLogLine_carriesTheWorkersTheSharedProbeRanWith() {
        assertThat(run(repo, ONE_TOKEN, ALL, null, settings(true, 5000, 3)).probeWorkers()).isEqualTo("3");
        // 0 is a real setting (a serial probe), distinct from "no probe ran"
        Run serial = run(repo, ONE_TOKEN, ALL, null, settings(true, 5000, 0));
        assertThat(serial.sharedProbe()).isEqualTo("per_collection");
        assertThat(serial.probeWorkers()).isEqualTo("0");
        assertThat(lexicalHits(serial.result())).as("non-vacuity: the lexical leg answered").isGreaterThan(20);
    }

    @Test
    void theLexicalLogLine_saysNoProbeRanWhenThereWasNoSharedProbe() {
        // sharing off: every arm ran its own probe, whatever the workers setting says
        Run off = run(repo, ONE_TOKEN, ALL, null, settings(false, 5000, 3));
        assertThat(off.sharedProbe()).isEqualTo("off");
        assertThat(off.probeWorkers()).isEqualTo("none");
        // one collection: nothing to share
        Run one = run(repo, ONE_TOKEN, List.of(S1), null, settings(true, 5000, 3));
        assertThat(one.sharedProbe()).isEqualTo("off");
        assertThat(one.probeWorkers()).isEqualTo("none");
    }

    @Test
    void theEnvSwitch_readsZeroOffFalse_andDefaultsOn() {
        assertThat(PgVectorRepository.sharedLexicalProbeEnabled(null)).isTrue();
        assertThat(PgVectorRepository.sharedLexicalProbeEnabled("")).isTrue();
        assertThat(PgVectorRepository.sharedLexicalProbeEnabled("1")).isTrue();
        for (String off : new String[] {"0", "off", "OFF", " false "}) {
            assertThat(PgVectorRepository.sharedLexicalProbeEnabled(off)).as(off).isFalse();
        }
    }
}

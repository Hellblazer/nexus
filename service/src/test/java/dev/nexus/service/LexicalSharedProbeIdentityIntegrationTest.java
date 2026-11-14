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

    /** One request, and the {@code shared_probe=} word its lexical log line carries. */
    private record Run(PerCollectionResult result, String sharedProbe) {}

    private Run run(String query, List<String> cols, Map<String, Object> where, boolean shared, int selectiveGateMax) {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(PgVectorRepository.class);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        PerCollectionResult r;
        try {
            r = repo.searchPerCollection(TENANT, query, cols, K, 1200, null, where, false, false, true,
                                         settings(shared, selectiveGateMax));
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }
        String line = logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=search_per_collection_lexical ")).findFirst().orElseThrow();
        String word = line.substring(line.indexOf("shared_probe=") + "shared_probe=".length());
        return new Run(r, word.substring(0, word.indexOf(' ')));
    }

    /** Both paths over one request: they must agree on everything the response carries; returns the shared one. */
    private Run identical(String query, List<String> cols, Map<String, Object> where, int selectiveGateMax,
                          String expectedSharedPath) {
        Run perArm = run(query, cols, where, false, selectiveGateMax);
        Run shared = run(query, cols, where, true, selectiveGateMax);
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

    // ── off switch, and the cases that never share ───────────────────────────

    @Test
    void oneCollection_runsNoSharedProbe() {
        Run run = run(ONE_TOKEN, List.of(S1), null, true, 5000);
        assertThat(run.sharedProbe()).isEqualTo("off");
        assertThat(lexicalHits(run.result())).isEqualTo(3);
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

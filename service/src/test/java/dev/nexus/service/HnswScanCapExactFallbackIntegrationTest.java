/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;

import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bq06h -- the HNSW-ordered scan under a highly selective filter can
 * exhaust {@code hnsw.max_scan_tuples} admitting too few rows and return
 * EMPTY, silently (measured in production 2026-09-03: a 176-row collection
 * on the shared index, 22,932 removed by filter, 0 admitted). The repository
 * must then re-run the statement exactly.
 *
 * <p>The pathology is reproduced the way production hit it, with no planner
 * switches: the filter is a metadata predicate the planner has no statistics
 * for (a jsonb expression), so it estimates a common match and takes the
 * HNSW-ordered scan; the one matching row sits far from the query while every
 * near row fails the predicate, so the bounded walk admits nothing. Session
 * settings arrive as pgjdbc connection OPTIONS (never SQL): a small
 * {@code hnsw.max_scan_tuples} so the cap is reached inside a 6,000-row
 * fixture instead of a 373k-row corpus, and seq/bitmap scans penalised so the
 * planner prefers the index-ordered plan as it did in production under a
 * stale generic plan. The exact fallback's {@code enable_indexscan=off} with
 * bitmap/seq/sort re-enabled then yields an exact plan over the filtered rows. Non-vacuity: the fallback COUNTER moves
 * for the starved search and stays put for one the index satisfies, so a pass
 * cannot come from the planner having chosen the exact plan on its own.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HnswScanCapExactFallbackIntegrationTest {

    static final String TENANT = "scan-cap";
    static final String COLLECTION = "knowledge__scan-cap__minilm-l6-v2-384__v1";
    static final String RARE_TEXT = "the one chunk carrying the rare metadata value";
    static final String QUERY = "scan cap query";
    static final int BIG_ROWS = 6000;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    PgVectorRepository repo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    String rareChash;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        // The forced-pathology session settings (see class javadoc).
        // Cap the walk so it starves inside a 6,000-row fixture, and penalise
        // every non-HNSW plan (seq, bitmap, and the pk-prefix index scan's
        // sort) so the planner takes the HNSW-ordered scan the way it did in
        // production under a stale generic plan. The exact fallback re-enables
        // seq scan + sort itself, so these penalties cannot mask it.
        cfg.addDataSourceProperty("options",
                "-c hnsw.max_scan_tuples=16 -c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        ds = new HikariDataSource(cfg);
        var scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);

        scope.withTenant(TENANT, ctx -> {
            ctx.insertInto(CATALOG_COLLECTIONS,
                           CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                           CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                           CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
               .values(TENANT, COLLECTION, "knowledge", "scan-cap", "minilm-l6-v2-384", "v1")
               .onConflictDoNothing()
               .execute();
            return null;
        });

        // The query sits at (1,0); every common chunk is a distinct unit vector
        // within a narrow cone around it, so the HNSW walk visits them first and
        // every one fails the predicate; the rare chunk sits opposite at (-1,0).
        embedder.register(QUERY, 1f, 0f);
        List<String> ids = new ArrayList<>(BIG_ROWS);
        List<String> texts = new ArrayList<>(BIG_ROWS);
        List<Map<String, Object>> metas = new ArrayList<>(BIG_ROWS);
        for (int i = 0; i < BIG_ROWS; i++) {
            double theta = -0.3 + 0.6 * i / (BIG_ROWS - 1);
            String text = "big chunk " + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of("k", "common"));
        }
        for (int from = 0; from < BIG_ROWS; from += 300) {
            int to = Math.min(BIG_ROWS, from + 300);
            repo.upsertChunks(TENANT, COLLECTION, ids.subList(from, to), texts.subList(from, to),
                              metas.subList(from, to));
        }
        embedder.register(RARE_TEXT, -1f, 0f);
        rareChash = chash(RARE_TEXT);
        repo.upsertChunks(TENANT, COLLECTION, List.of(rareChash), List.of(RARE_TEXT),
                          List.of(Map.of("k", "rare")));
    }

    @AfterAll
    void stopAll() {
        if (ds != null) {
            ds.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void selectiveFilterUnderTheScanCapReturnsItsRowViaTheExactFallback() {
        long before = PgVectorRepository.exactFallbackCount();
        var rows = repo.searchWithTokens(TENANT, QUERY, List.of(COLLECTION), 1,
                                         Map.of("k", "rare"), false).value();
        assertThat(rows).as("the one matching row, never empty").hasSize(1);
        assertThat(rows.get(0).get("id")).isEqualTo(rareChash);
        assertThat(PgVectorRepository.exactFallbackCount() - before)
            .as("the exact fallback fired exactly once for the starved scan").isEqualTo(1);
    }

    @Test
    void unfilteredSearchSatisfiedByTheIndexDoesNotFallBack() {
        long before = PgVectorRepository.exactFallbackCount();
        var rows = repo.searchWithTokens(TENANT, QUERY, List.of(COLLECTION), 5, null, false).value();
        assertThat(rows).hasSize(5);
        assertThat(PgVectorRepository.exactFallbackCount() - before)
            .as("no fallback when the index-ordered scan admits enough rows").isZero();
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.ArmErrorKind;
import dev.nexus.service.vectors.PgVectorRepository.LexicalStat;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionStat;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-xd2rc -- the lexical leg of {@link PgVectorRepository#searchPerCollection}: with {@code lexical},
 * each collection also runs {@code hybridSearch}'s text-gated statement as its own arm, and its rows are
 * unioned in, the vector copy winning a row both arms returned, exempt from the collection's threshold and
 * from the {@code limit} cut. Runs as {@code nexus_svc} (NOBYPASSRLS), as the route does in production.
 *
 * <p>Fixture, one query vector and one rare token ({@value #QUERY}). A: twenty plain rows close to the
 * query, one token row among them ({@code near}, inside A's vector top-{@value #K}) and one token row far
 * away ({@code far}, outside it). B: three plain rows and one token row ({@code deep}) further out than
 * them. C: one token row, its catalog dimension then made to disagree with the query vector, so its
 * vector arm fails.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorSearchPerCollectionLexicalIntegrationTest {

    static final String QUERY = "zebracorn";
    static final int K = 20;
    static final String TENANT = "xd2rc-lex";
    static final String A = "knowledge__xd2rc-a__minilm-l6-v2-384__v1";
    static final String B = "knowledge__xd2rc-b__minilm-l6-v2-384__v1";
    static final String C = "knowledge__xd2rc-c__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;

    String near;
    String far;
    String deep;

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
        embedder.register(QUERY, 1f, 0f);
        repo = new PgVectorRepository(scope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(A, B, C)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        Map<String, Double> a = new LinkedHashMap<>();
        for (int i = 0; i < K; i++) {
            a.put("alpha plain filler " + i, i * 0.001);
        }
        a.put("alpha zebracorn near", 0.0105);
        a.put("alpha zebracorn far", 1.3);
        seed(A, a);
        Map<String, Double> b = new LinkedHashMap<>();
        for (int i = 0; i < 3; i++) {
            b.put("bravo plain filler " + i, 0.5 + i * 0.01);
        }
        b.put("bravo zebracorn deep", 1.0);
        seed(B, b);
        seed(C, Map.of("charlie zebracorn orphan", 0.2));
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.DIMENSION, 768)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(C)))
               .execute();
        }
        dev.nexus.service.db.CollectionRegistry.evict(TENANT, C);
        near = chash("alpha zebracorn near");
        far = chash("alpha zebracorn far");
        deep = chash("bravo zebracorn deep");
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void seed(String collection, Map<String, Double> angles) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        angles.forEach((text, theta) -> {
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        });
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private PerCollectionResult search(List<String> cols, int limit, Map<String, Double> thresholds, boolean lexical) {
        return repo.searchPerCollection(TENANT, QUERY, cols, K, limit, thresholds, null, false, false, lexical);
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    private static int slot(PerCollectionResult r, String collection) {
        for (int i = 0; i < r.perCollection().size(); i++) {
            if (r.perCollection().get(i).collection().equals(collection)) return i;
        }
        throw new AssertionError("no per_collection entry for " + collection);
    }

    private static LexicalStat lexical(PerCollectionResult r, String collection) {
        return r.lexical().get(slot(r, collection));
    }

    private static PerCollectionStat vector(PerCollectionResult r, String collection) {
        return r.perCollection().get(slot(r, collection));
    }

    @Test
    void withoutLexical_theRouteIsUnchanged() {
        PerCollectionResult off = search(List.of(A, B), 1200, null, false);
        PerCollectionResult plain = repo.searchPerCollection(TENANT, QUERY, List.of(A, B), K, 1200, null, null, false);
        assertThat(off.lexicalRows()).isEmpty();
        assertThat(off.lexical()).isEmpty();
        assertThat(off.rows()).isEqualTo(plain.rows());
        assertThat(off.perCollection()).isEqualTo(plain.perCollection());
    }

    @Test
    void eachCollectionsLexicalArm_isItsSingleCollectionHybridSearch() {
        PerCollectionResult r = search(List.of(A, B), 1200, null, true);
        for (String c : List.of(A, B)) {
            List<String> hybrid = ids(repo.hybridSearch(TENANT, QUERY, List.of(c), K, null));
            assertThat(hybrid).as("the fixture's token rows pass the text gate in %s", c).isNotEmpty();
            assertThat(lexical(r, c).ids()).as("lexical ids of %s", c).isEqualTo(hybrid);
            assertThat(lexical(r, c).count()).isEqualTo(hybrid.size());
            assertThat(lexical(r, c).error()).isNull();
        }
        assertThat(lexical(r, A).ids()).containsExactly(near, far);
        assertThat(lexical(r, B).ids()).containsExactly(deep);
    }

    @Test
    void aRowBothArmsReturned_keepsItsVectorCopy_once() {
        PerCollectionResult r = search(List.of(A, B), 1200, null, true);
        assertThat(ids(r.rows())).contains(near);
        assertThat(ids(r.lexicalRows())).doesNotContain(near);
        // The vector arm's stats are the vector arm's: the lexical arm adds no raw rows.
        assertThat(vector(r, A).rawCount()).isEqualTo(K);
    }

    @Test
    void aLexicalRowOutsideTheVectorTopK_isServed() {
        PerCollectionResult r = search(List.of(A, B), 1200, null, true);
        assertThat(ids(r.rows())).as("A's vector top-%s holds twenty nearer rows", K).doesNotContain(far);
        assertThat(ids(r.lexicalRows())).contains(far);
        assertThat(r.lexicalRows()).allSatisfy(row -> assertThat(row).containsKeys("chash", "span"));
    }

    @Test
    void aLexicalRowIsExemptFromItsCollectionsThreshold() {
        Map<String, Double> thresholds = Map.of(B, 0.2);   // B's plain rows sit at ~0.12-0.14, deep at ~0.46
        PerCollectionResult off = search(List.of(A, B), 1200, thresholds, false);
        PerCollectionResult on = search(List.of(A, B), 1200, thresholds, true);
        assertThat(ids(off.rows())).doesNotContain(deep);
        assertThat(vector(on, B)).isEqualTo(vector(off, B));
        assertThat(vector(on, B).dropped()).isEqualTo(1);
        assertThat(ids(on.lexicalRows())).contains(deep);
    }

    @Test
    void aLexicalRowIsExemptFromTheLimitCut() {
        PerCollectionResult r = search(List.of(A, B), 3, null, true);
        assertThat(r.rows()).hasSize(3);
        assertThat(ids(r.rows())).doesNotContain(near, far, deep);
        assertThat(ids(r.lexicalRows())).containsExactlyInAnyOrder(near, far, deep);
        List<Double> d = r.lexicalRows().stream().map(x -> (Double) x.get("distance")).toList();
        assertThat(d).as("the extra rows come best first").isSorted();
    }

    @Test
    void aCollectionWhoseVectorArmFailed_servesNoLexicalRows() {
        PerCollectionResult r = search(List.of(A, C), 1200, null, true);
        assertThat(vector(r, C).errorKind()).isEqualTo(ArmErrorKind.DIMENSION_MISMATCH);
        assertThat(lexical(r, C).ids()).isEmpty();
        assertThat(r.lexicalRows()).noneSatisfy(row -> assertThat(row.get("collection")).isEqualTo(C));
        assertThat(lexical(r, A).ids()).containsExactly(near, far);
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import org.jooq.DSLContext;
import org.jooq.Record;
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
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_QUALIFYING_HITS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bbtc2: a one-token lexical query gates on an exact, case-insensitive substring of the token in place of the
 * word_similarity trigram half (Sam's choice, ask linda/short-lexical-trigram option 1). The engine sets
 * {@code nexus.lexical_trgm} to {@code substring} for such a gate text; the probe (vectors-058-1) and the ten row-gating
 * functions (vectors-058-2) read it.
 *
 * <p>Fixture, one tenant and collection: {@code ident} holds the identifier {@link #TOKEN}; {@code fuzzy} holds a
 * near-spelling of it ({@code word_similarity} far above the gate's 0.6) but does not contain it, so it is the row the
 * trigram half admits and the substring match does not; {@code under}, {@code pct} and {@code bslash} hold the LIKE
 * metacharacters {@code _ % \}, each beside a decoy row an unescaped pattern would also match. Runs as nexus_svc in the
 * shared superuser-migrated container (the definer posture); {@code TextGateProbeSingleRoleGuardIntegrationTest} holds
 * the invoker posture.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TextGateSingleTokenIntegrationTest {

    static final String TENANT = "bbtc2-tok";
    static final String COLL = "knowledge__bbtc2-a__minilm-l6-v2-384__v1";
    static final String MODEL = "minilm-l6-v2-384";
    static final int K = 20;

    static final String TOKEN = "quuxlongidentifier";
    static final String IDENT_TEXT = "alpha quuxlongidentifier beta";
    /** Resembles {@link #TOKEN} (one letter changed) without containing it. */
    static final String FUZZY_TEXT = "gamma quuxlongidentifyer delta";
    static final String UNDER_TEXT = "call zz_qq here";
    static final String UNDER_DECOY = "call zzxqq here";
    static final String PCT_TEXT = "rate 51%zork now";
    static final String PCT_DECOY = "rate 51xxzork now";
    static final String BSLASH_TEXT = "path wq\\vu end";
    static final String BSLASH_DECOY = "path wqvu end";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;
    final List<String> all = new ArrayList<>();

    String ident;
    String fuzzy;

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
            PgContainerHelper.insertCollection(DSL.using(su, org.jooq.SQLDialect.POSTGRES), TENANT, COLL);
        }
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        double theta = 0.0;
        for (String t : List.of(IDENT_TEXT, FUZZY_TEXT, UNDER_TEXT, UNDER_DECOY, PCT_TEXT, PCT_DECOY, BSLASH_TEXT,
                                BSLASH_DECOY)) {
            seedRow(texts, metas, t, theta);
            theta += 0.01;
        }
        for (int i = 0; i < 12; i++) {
            seedRow(texts, metas, "plain filler row " + i + " alpha bravo charlie", 0.2 + i * 0.01);
        }
        repo.upsertChunks(TENANT, COLL, all, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, COLL, all.toArray(new String[0]));
            return null;
        });
        ident = id(IDENT_TEXT);
        fuzzy = id(FUZZY_TEXT);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void seedRow(List<String> texts, List<Map<String, Object>> metas, String text, double theta) {
        embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
        all.add(id(text));
        texts.add(text);
        metas.add(Map.of());
    }

    private static String id(String text) {
        return Chash.ofText(text).toHex();
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    /** Runs {@code body} as the tenant with {@code nexus.lexical_trgm} set to {@code guc} (and the gate's threshold). */
    private <T> T asTenant(String guc, Function<DSLContext, T> body) {
        return scope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold", "0.6");
            if (guc != null) PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, guc);
            return body.apply(ctx);
        });
    }

    private List<String> probe(String text, String guc) {
        Table<?> fn = TEXT_GATE_PROBE_384.call(text, new String[] {COLL}, null, null, 100, MODEL, TENANT);
        return asTenant(guc, ctx -> ctx.selectFrom(fn).fetch().getValues(0, byte[].class).stream()
            .map(b -> HexFormat.of().formatHex(b)).toList());
    }

    private static Vector query() {
        float[] v = new float[384];
        v[0] = 1f;
        return Vector.of(v);
    }

    private byte[][] allowAll() {
        return all.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
    }

    private static List<String> idColumn(org.jooq.Result<? extends Record> rows) {
        return rows.getValues(0, String.class);
    }

    // ── the rule ────────────────────────────────────────────────────────────

    @Test
    void theMode_isSubstringForOneToken_onForTwoToEightWords_offAboveEight() {
        assertThat(PgVectorRepository.lexicalTrgmMode(TOKEN)).isEqualTo(PgVectorRepository.LEXICAL_TRGM_SUBSTRING);
        assertThat(PgVectorRepository.lexicalTrgmMode(" \t" + TOKEN + "\n ")).as("padded one token")
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_SUBSTRING);
        assertThat(PgVectorRepository.lexicalTrgmMode("zz_qq")).as("an identifier with a LIKE metacharacter")
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_SUBSTRING);
        assertThat(PgVectorRepository.lexicalTrgmMode("advisory lock")).isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("advisory\tlock")).as("any ASCII whitespace splits")
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("advisory\u00A0lock")).as("an NBSP splits")
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("advisory\u3000lock")).as("an ideographic space splits")
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("one two three four five six seven eight"))
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("one two three four five six seven eight nine"))
            .isEqualTo(PgVectorRepository.LEXICAL_TRGM_OFF);
        assertThat(PgVectorRepository.lexicalTrgmMode("")).isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode("   ")).isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
        assertThat(PgVectorRepository.lexicalTrgmMode(null)).isEqualTo(PgVectorRepository.LEXICAL_TRGM_ON);
    }

    // ── the probe (vectors-058-1) ───────────────────────────────────────────

    @Test
    void theProbe_underSubstring_passesRowsHoldingTheToken_notItsFuzzyNeighbour() {
        assertThat(probe(TOKEN, "on")).as("non-vacuity: the trigram half admits the fuzzy neighbour")
            .containsExactlyInAnyOrder(ident, fuzzy);
        assertThat(probe(TOKEN, null)).as("GUC unset: today's trigram half").containsExactlyInAnyOrder(ident, fuzzy);
        assertThat(probe(TOKEN, "substring")).as("substring: the row holding the token only").containsExactly(ident);
        assertThat(probe(TOKEN.toUpperCase(), "substring")).as("case-insensitive").containsExactly(ident);
        assertThat(probe("identifier", "substring")).as("a fragment of the identifier finds its full form, an FTS miss")
            .containsExactly(ident);
        assertThat(probe("quuxlongidentifer", "substring")).as("known loss: a typo'd token finds nothing")
            .isEmpty();
    }

    @Test
    void theProbe_underSubstring_escapesTheLikeMetacharacters() {
        assertThat(probe("zz_qq", "substring")).as("_ is literal").containsExactly(id(UNDER_TEXT));
        assertThat(probe("51%zork", "substring")).as("% is literal").containsExactly(id(PCT_TEXT));
        assertThat(probe("wq\\vu", "substring")).as("a backslash is literal").containsExactly(id(BSLASH_TEXT));
    }

    // ── the row gates (vectors-058-2) ───────────────────────────────────────

    /**
     * One row gate under every GUC value, with cases only the substring arm can pass: the fragment "identifier" (an FTS
     * miss, contained in {@code ident} alone) and the three metacharacter tokens, whose decoy rows only an unescaped
     * pattern would match. {@code run} takes the GUC (null for unset) and the gate text.
     */
    private void assertRowGate(String what, java.util.function.BiFunction<String, String, List<String>> run) {
        assertThat(run.apply(null, TOKEN)).as("%s, unset: word_similarity admits the fuzzy row", what)
            .containsExactlyInAnyOrder(ident, fuzzy);
        assertThat(run.apply("on", TOKEN)).as("%s, on", what).containsExactlyInAnyOrder(ident, fuzzy);
        assertThat(run.apply("off", TOKEN)).as("%s, off is not read by the row gates (vectors-057 left them on word_similarity)", what)
            .containsExactlyInAnyOrder(ident, fuzzy);
        assertThat(run.apply("substring", TOKEN)).as("%s, substring", what).containsExactly(ident);
        assertThat(run.apply("substring", "identifier")).as("%s, substring: a fragment only the ILIKE arm passes", what)
            .containsExactly(ident);
        assertThat(run.apply("substring", "zz_qq")).as("%s, substring: _ is literal", what).containsExactly(id(UNDER_TEXT));
        assertThat(run.apply("substring", "51%zork")).as("%s, substring: %% is literal", what).containsExactly(id(PCT_TEXT));
        assertThat(run.apply("substring", "wq\\vu")).as("%s, substring: a backslash is literal", what)
            .containsExactly(id(BSLASH_TEXT));
    }

    @Test
    void theAllowListRank_readsTheGuc() {
        assertRowGate("text_gated_search_allowed_384", (guc, text) -> asTenant(guc, ctx -> idColumn(ctx.selectFrom(
            TEXT_GATED_SEARCH_ALLOWED_384.call(query(), text, allowAll(), new String[] {COLL}, null, null, K, MODEL,
                TENANT, false)).fetch())));
    }

    @Test
    void theHnswFirstRank_readsTheGuc() {
        assertRowGate("text_gated_search_hnsw_first_384", (guc, text) -> asTenant(guc, ctx -> idColumn(ctx.selectFrom(
            TEXT_GATED_SEARCH_HNSW_FIRST_384.call(query(), text, new String[] {COLL}, null, null, K, MODEL, TENANT,
                false)).fetch())));
    }

    @Test
    void theManifestPaths_readTheGuc() {
        assertRowGate("manifest_search_384", (guc, text) -> asTenant(guc, ctx -> idColumn(ctx.selectFrom(
            MANIFEST_SEARCH_384.call(query(), text, null, new String[] {COLL}, null, null, null, null, K, MODEL, TENANT,
                false)).fetch())));
        // manifest_qualifying_hits returns one row per (owner, chunk); every fixture chunk has one owner, so the row
        // count is the number of gated chunks.
        java.util.function.BiFunction<String, String, Integer> hits = (guc, text) -> asTenant(guc, ctx -> ctx.selectFrom(
            MANIFEST_QUALIFYING_HITS.call(new String[] {COLL}, null, null, null, null, 100, MODEL, TENANT, text, false))
            .fetch().size());
        assertThat(hits.apply(null, TOKEN)).as("manifest_qualifying_hits, unset").isEqualTo(2);
        assertThat(hits.apply("off", TOKEN)).as("manifest_qualifying_hits, off is not read here").isEqualTo(2);
        assertThat(hits.apply("substring", TOKEN)).as("manifest_qualifying_hits, substring").isEqualTo(1);
        assertThat(hits.apply("substring", "identifier")).as("manifest_qualifying_hits, substring fragment").isEqualTo(1);
        assertThat(hits.apply("substring", "51%zork")).as("manifest_qualifying_hits, % is literal").isEqualTo(1);
    }

    @Test
    void theTenRowGates_readTheGuc_andStayInlinable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var gates = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, org.jooq.SQLDialect.POSTGRES), "nexus")
                .values().stream()
                .filter(f -> Vectors058RollbackIntegrationTest.ROW_GATES.stream()
                    .anyMatch(n -> f.identity().startsWith(n + "(")))
                .toList();
            assertThat(gates).as("non-vacuity: the ten row gates, one signature each").hasSize(10);
            for (var f : gates) {
                assertThat(f.body()).as("%s reads the switch", f.identity())
                    .contains("nexus.lexical_trgm").contains("~~*");
                assertThat(f.language()).as("%s language", f.identity()).isEqualTo("sql");
                assertThat(f.securityDefiner()).as("%s posture", f.identity()).isFalse();
                assertThat(f.config()).as("%s has no SET clause, so it still inlines", f.identity()).isNull();
            }
        }
    }

    // ── the engine's use of it ──────────────────────────────────────────────

    @Test
    void hybridSearch_aOneTokenQueryMatchesBySubstring_aTwoWordQueryKeepsTheTrigramHalf() {
        assertThat(ids(repo.hybridSearch(TENANT, TOKEN, List.of(COLL), K, null)))
            .as("one token: the fuzzy neighbour is absent").containsExactly(ident);
        assertThat(ids(repo.hybridSearch(TENANT, "\t " + TOKEN + " \n", List.of(COLL), K, null)))
            .as("one token, padded: stripped before it is the pattern").containsExactly(ident);
        assertThat(ids(repo.hybridSearch(TENANT, "quuxlongidentifer", List.of(COLL), K, null)))
            .as("known loss: a typo'd single token returns nothing").isEmpty();
        assertThat(ids(repo.hybridSearch(TENANT, "alpha quuxlongidentifer", List.of(COLL), K, null)))
            .as("two words: the trigram half still rescues the typo").contains(ident);
    }

    @Test
    void theLexicalArm_withAnAllowList_appliesTheSameRule() {
        // The chash_allow branch of hybridRank returns before the probe; the GUC must already be set when it runs.
        PerCollectionResult r = repo.searchPerCollection(TENANT, TOKEN, List.of(COLL), K, 1200, null, null, false, false,
            true, allowAll());
        assertThat(r.lexical().get(0).error()).isNull();
        assertThat(r.lexical().get(0).ids()).as("chash_allow path, one token: substring, so the fuzzy row is absent")
            .containsExactly(ident);
    }

    @Test
    void theLexicalArm_aOneTokenQueryMatchesBySubstring() {
        PerCollectionResult r = repo.searchPerCollection(TENANT, TOKEN, List.of(COLL), K, 1200, null, null, false, false, true);
        assertThat(r.lexical().get(0).error()).isNull();
        assertThat(r.lexical().get(0).ids()).as("lexical arm, one token").containsExactly(ident);
    }
}

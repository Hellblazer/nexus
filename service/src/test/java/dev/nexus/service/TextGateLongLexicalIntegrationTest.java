// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
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

import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-seo4k: a lexical query of more than {@link PgVectorRepository#LEXICAL_TRGM_MAX_WORDS} words skips the trigram
 * half of the text gate (Sam's choice, ask linda/lexical-trigram-scope option 1). The engine sets the
 * transaction-local GUC {@code nexus.lexical_trgm} to {@code off} for such a gate text and
 * {@code nexus.text_gate_probe_<dim>} (vectors-057) then gates on the FTS half alone; for a shorter text, and for a
 * transaction that never set the GUC, the probe is today's FTS-or-trigram statement.
 *
 * <p>Fixture, one tenant and collection. {@code exact} carries the nine-word sentence {@link #NINE}; {@code typo}
 * carries it with its third word changed, so its FTS lexemes miss the query while its trigrams still match it
 * (word similarity about 0.9, far above the gate's 0.6); {@code ident} holds one long identifier and {@code filler}
 * rows share no trigram with either. A row only the trigram half passes is {@code typo} (for the long query) and
 * {@code ident} (for the typo'd identifier).
 *
 * <p>Runs as {@code nexus_svc} (NOBYPASSRLS) in the shared superuser-migrated container, where vectors-029's guard
 * holds and the probes are SECURITY DEFINER; the invoker posture is
 * {@code TextGateProbeSingleRoleGuardIntegrationTest}. The plan of the long branch (no trigram index) is
 * {@code Rdr192LiveCExplainEvidenceIntegrationTest#gateProbe_withTheTrigramGucOff_plansTheFtsHalfOnly_andWithItUnsetPlansBoth}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TextGateLongLexicalIntegrationTest {

    static final String TENANT = "seo4k-lex";
    static final String COLL = "knowledge__seo4k-a__minilm-l6-v2-384__v1";
    static final String MODEL = "minilm-l6-v2-384";
    static final int K = 20;

    /** Nine words, none a stop word, none sharing a stem with another. */
    static final String NINE = "zorblax quindle frumpet wibblesnort glorptang mexicane trollopy vandergriff snorkelwick";
    /** The first eight words of {@link #NINE}: the longest text that keeps the trigram half. */
    static final String EIGHT = "zorblax quindle frumpet wibblesnort glorptang mexicane trollopy vandergriff";
    /** {@link #NINE} with its third word changed: an FTS miss and a trigram hit. */
    static final String TYPO_TEXT = "zorblax quindle frumpot wibblesnort glorptang mexicane trollopy vandergriff snorkelwick";
    static final String IDENT_TEXT = "alpha quuxlongidentifier beta";
    /**
     * Two words, the second a one-letter typo of the identifier: an FTS miss and a trigram hit. Two words, because a
     * single token matches by substring since vectors-058 (nexus-bbtc2) and so loses the typo rescue.
     */
    static final String TYPO_IDENT = "alpha quuxlongidentifer";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;

    String exact;
    String typo;
    String ident;

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
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        seedRow(ids, texts, metas, NINE, 0.0);
        seedRow(ids, texts, metas, TYPO_TEXT, 0.01);
        seedRow(ids, texts, metas, IDENT_TEXT, 0.02);
        for (int i = 0; i < 12; i++) {
            seedRow(ids, texts, metas, "plain filler row " + i + " alpha bravo charlie", 0.05 + i * 0.01);
        }
        repo.upsertChunks(TENANT, COLL, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, COLL, ids.toArray(new String[0]));
            return null;
        });
        exact = Chash.ofText(NINE).toHex();
        typo = Chash.ofText(TYPO_TEXT).toHex();
        ident = Chash.ofText(IDENT_TEXT).toHex();
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void seedRow(List<String> ids, List<String> texts, List<Map<String, Object>> metas, String text, double theta) {
        embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
        ids.add(Chash.ofText(text).toHex());
        texts.add(text);
        metas.add(Map.of());
    }

    /** The probe called directly, as the tenant, with {@code nexus.lexical_trgm} set to {@code guc} when it is non-null. */
    private List<String> probe(String text, String guc) {
        Table<?> fn = TEXT_GATE_PROBE_384.call(text, new String[] {COLL}, null, null, 100, MODEL, TENANT);
        return scope.withTenant(TENANT, ctx -> {
            if (guc != null) PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, guc);
            return ctx.selectFrom(fn).fetch().getValues(0, byte[].class).stream()
                .map(b -> HexFormat.of().formatHex(b)).toList();
        });
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    // ── the word count ──────────────────────────────────────────────────────

    @Test
    void theWordCount_isWhitespaceSeparatedWords_andEightKeepsTheTrigramHalf() {
        assertThat(PgVectorRepository.LEXICAL_TRGM_MAX_WORDS).isEqualTo(8);
        assertThat(PgVectorRepository.skipsTrigramGate(EIGHT)).as("eight words").isFalse();
        assertThat(PgVectorRepository.skipsTrigramGate(NINE)).as("nine words").isTrue();
        assertThat(PgVectorRepository.skipsTrigramGate("  " + EIGHT + "\t \n")).as("eight words, padded").isFalse();
        assertThat(PgVectorRepository.skipsTrigramGate(NINE.replace(" ", "  \t\n "))).as("nine words, any whitespace").isTrue();
        assertThat(PgVectorRepository.skipsTrigramGate(TYPO_IDENT)).as("two words").isFalse();
        assertThat(PgVectorRepository.skipsTrigramGate("")).as("empty").isFalse();
        assertThat(PgVectorRepository.skipsTrigramGate("   ")).as("blank").isFalse();
        assertThat(PgVectorRepository.skipsTrigramGate(null)).as("null").isFalse();
    }

    // ── the probe's SQL contract ────────────────────────────────────────────

    @Test
    void theProbes_areThreePlpgsqlFunctions_thatReadTheGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var probes = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, org.jooq.SQLDialect.POSTGRES), "nexus")
                .values().stream().filter(f -> f.identity().startsWith("text_gate_probe_")).toList();
            assertThat(probes).as("non-vacuity: the three probes, one signature each").hasSize(3);
            for (var f : probes) {
                assertThat(f.language()).as("%s language", f.identity()).isEqualTo("plpgsql");
                assertThat(f.securityDefiner()).as("%s: this container is the definer posture", f.identity()).isTrue();
                assertThat(f.body()).as("%s reads the switch", f.identity()).contains("nexus.lexical_trgm");
                assertThat(f.result()).as("%s return type is unchanged", f.identity()).isEqualTo("SETOF bytea");
            }
        }
    }

    @Test
    void theProbe_withTheGucOff_isTheFtsHalfAlone_andWithoutIt_isFtsOrTrigram() {
        assertThat(probe(NINE, null)).as("GUC unset: both halves, the typo row passes by trigram")
            .containsExactlyInAnyOrder(exact, typo);
        assertThat(probe(NINE, "on")).as("GUC on: both halves").containsExactlyInAnyOrder(exact, typo);
        assertThat(probe(NINE, "")).as("GUC empty (what a custom GUC reads as after its transaction): both halves")
            .containsExactlyInAnyOrder(exact, typo);
        assertThat(probe(NINE, "off")).as("GUC off: the FTS half alone, a row only the trigram half would pass is absent")
            .containsExactly(exact);
    }

    @Test
    void theGuc_aloneControlsTheProbe_aShortTypodIdentifierPassesOnlyWhileTheTrigramHalfIsOn() {
        assertThat(probe(TYPO_IDENT, null)).as("short query holding a typo'd identifier, GUC unset: the trigram half passes it")
            .containsExactly(ident);
        assertThat(probe(TYPO_IDENT, "off")).as("the same text with the GUC off: nothing, the FTS half misses it")
            .isEmpty();
    }

    // ── the engine's use of it ──────────────────────────────────────────────

    @Test
    void hybridSearch_aLongQueryReturnsOnlyFtsMatches_aShortQueryWithATypodIdentifierKeepsTheTrigramHalf() {
        assertThat(ids(repo.hybridSearch(TENANT, NINE, List.of(COLL), K, null)))
            .as("nine words: the row only the trigram half would pass is absent").containsExactly(exact);
        assertThat(ids(repo.hybridSearch(TENANT, EIGHT, List.of(COLL), K, null)))
            .as("eight words: the trigram half is still on, so the typo row (an FTS miss) passes beside the exact row")
            .containsExactlyInAnyOrder(exact, typo);
        assertThat(ids(repo.hybridSearch(TENANT, TYPO_IDENT, List.of(COLL), K, null)))
            .as("a short query holding a typo'd identifier").containsExactly(ident);
    }

    @Test
    void theLexicalArm_aLongQueryReturnsOnlyFtsMatches_aShortQueryWithATypodIdentifierKeepsTheTrigramHalf() {
        PerCollectionResult longQ = repo.searchPerCollection(TENANT, NINE, List.of(COLL), K, 1200, null, null, false, false, true);
        assertThat(longQ.lexical().get(0).ids()).as("lexical arm, nine words").containsExactly(exact);
        assertThat(longQ.lexical().get(0).error()).isNull();
        PerCollectionResult shortQ = repo.searchPerCollection(TENANT, TYPO_IDENT, List.of(COLL), K, 1200, null, null, false, false, true);
        assertThat(shortQ.lexical().get(0).ids()).as("lexical arm, short query holding a typo'd identifier").containsExactly(ident);
    }

    @Test
    void aTransactionThatSetTheGuc_doesNotLeaveItOnItsConnection() {
        repo.hybridSearch(TENANT, NINE, List.of(COLL), K, null);   // sets the GUC transaction-locally
        // A pool of four: run enough short searches that every connection is reused; none may see the GUC still off.
        for (int i = 0; i < 8; i++) {
            assertThat(ids(repo.hybridSearch(TENANT, TYPO_IDENT, List.of(COLL), K, null)))
                .as("short query holding a typo'd identifier, round %d", i).containsExactly(ident);
        }
    }
}

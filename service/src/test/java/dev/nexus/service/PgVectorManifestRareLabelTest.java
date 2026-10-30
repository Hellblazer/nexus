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
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-p89h4 (absorbed into RDR-236 Phase 3, nexus-6pfgb.25): a POSITIVE label filter on a rare label returns
 * its rows, however far down the neighbourhood they sit. Since nexus-tko2w the filter ran after the search over a
 * 4x cut (10 rows asked, 40 searched), so a label held by two chunks of 2,000 that rank last came back empty. The
 * predicate now runs INSIDE the search, as an EXISTS on the manifest, so the index (or exact) scan keeps going
 * until the page is full.
 *
 * <p>Fixture. One document, 2,000 chunks laid on a quarter circle so the query (1, 0) ranks them in order. Every
 * chunk's manifest row carries {@code section_type body} and {@code page_number} = its index, except: the last
 * two (indexes 1998, 1999) are {@code appendix-x} on pages 9000 and 9001 with the title {@code Rare}; index 1500
 * is a LEGACY row ({@code labels_written} false) whose chunk row says {@code legacy-rare}; and index 1700 is a
 * WRITTEN row whose chunk row says {@code stale-row} (a last writer's label the document must not inherit).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestRareLabelTest {

    private static final String SVC_ROLE = "svc_rare_label";
    private static final String SVC_PASS = "svc_rare_label_pass";

    private static final String TENANT = "rare-label";
    private static final String COLLECTION = "docs__rare-label__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String DOC = "rare-doc";
    private static final String QUERY = "rareprobe";
    private static final int N = 2000;

    private static String chash(int i) {
        return Chash.ofText("rare-label-" + i).toHex();
    }

    private static String text(int i) {
        return "rareprobe chunk number " + i;
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;

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
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        var tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        for (int i = 0; i < N; i++) {
            double theta = (Math.PI / 2.0) * i / (N - 1);
            embedder.register(text(i), (float) Math.cos(theta), (float) Math.sin(theta));
        }
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC)
                .set(CATALOG_DOCUMENTS.TITLE, "rare.md")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///rare.md")
                .execute();
        }

        for (int from = 0; from < N; from += 500) {
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = from; i < Math.min(N, from + 500); i++) {
                ids.add(chash(i));
                texts.add(text(i));
                Map<String, Object> m = new LinkedHashMap<>();
                if (i == 1500) m.put("section_type", "legacy-rare");
                if (i == 1700) m.put("section_type", "stale-row");
                metas.add(m);
            }
            vec.upsertChunks(TENANT, COLLECTION, ids, texts, metas);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            for (int from = 0; from < N; from += 500) {
                var step = seed.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                    CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
                    CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN);
                for (int i = from; i < Math.min(N, from + 500); i++) {
                    boolean rare = i >= N - 2;
                    boolean legacy = i == 1500;
                    step = step.values(TENANT, DOC, i, HexFormat.of().parseHex(chash(i)), COLLECTION, MODEL,
                        rare ? "Rare" : null,
                        legacy ? null : rare ? "appendix-x" : "body",
                        legacy ? null : rare ? 9000 + (i - (N - 2)) : i,
                        !legacy);
                }
                step.execute();
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static Map<String, Object> where(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        return where(operator, operand);
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toList());
    }

    private List<Map<String, Object>> search(int n, Map<String, Object> where) {
        return vec.search(TENANT, QUERY, List.of(COLLECTION), n, where);
    }

    /** Every route, same answer: plain search, hybrid (selective and dense gate) and the per-collection fan-out. */
    private void expectAll(int n, Map<String, Object> where, List<String> expected) {
        assertThat(ids(search(n, where))).as("search %s", where).containsExactlyElementsOf(expected);
        assertThat(ids(vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), n, where).value()))
            .as("hybrid, selective gate %s", where).containsExactlyElementsOf(expected);
        assertThat(ids(vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), n, where, false, 1).value()))
            .as("hybrid, dense gate %s", where).containsExactlyElementsOf(expected);
        assertThat(ids(vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), n, n, null, where, false).rows()))
            .as("per-collection %s", where).containsExactlyElementsOf(expected);
    }

    @Test
    void aRareSectionType_heldByTwoOfTwoThousand_isFoundAtTenRows() {
        // Ranks 1999 and 2000 of 2000; the 4x cut saw the nearest 40.
        expectAll(10, where("section_type", "appendix-x"), List.of(chash(N - 2), chash(N - 1)));
    }

    @Test
    void aRareSectionType_isFoundOnTheHnswRouteToo() {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        try {
            expectAll(10, where("section_type", "appendix-x"), List.of(chash(N - 2), chash(N - 1)));
        } finally {
            PgSession.resetSearchExactMaxRowsForTests();
        }
    }

    @Test
    void aRarePageRange_andARareSectionTitle_areFoundToo() {
        expectAll(10, where("page_number", op("$gte", 9000)), List.of(chash(N - 2), chash(N - 1)));
        expectAll(10, where("page_number", op("$gt", 9000)), List.of(chash(N - 1)));
        expectAll(10, where("page_number", 9001), List.of(chash(N - 1)));
        expectAll(10, where("section_title", "Rare"), List.of(chash(N - 2), chash(N - 1)));
        expectAll(10, where("section_type", op("$in", List.of("appendix-x", "nothing"))),
                  List.of(chash(N - 2), chash(N - 1)));
    }

    @Test
    void aLegacyManifestRow_matchesOnTheChunkRowsLabel_aWrittenRowNeverDoes() {
        expectAll(10, where("section_type", "legacy-rare"), List.of(chash(1500)));
        // Chunk 1700's row says stale-row, but its manifest row is written and says body.
        expectAll(10, where("section_type", "stale-row"), List.of());
    }

    @Test
    void theRowsNameTheirOwner_andTheirOwnLabels() {
        var rows = search(10, where("section_type", "appendix-x"));
        assertThat(rows).hasSize(2);
        assertThat(rows.stream().map(r -> r.get("doc_id")).toList()).containsExactly(DOC, DOC);
        assertThat(rows.stream().map(r -> ((Number) r.get("page_number")).intValue()).toList())
            .containsExactly(9000, 9001);
    }

    @Test
    void aFilterEveryChunkPasses_stillStopsAtTheLimit_theCutIsBoundedByTheCeiling() {
        // 2,000 chunks pass; 300 are asked for. The search is cut at widen(300) = 1200, never the whole 2,000.
        var rows = search(300, where("section_type", "body"));
        assertThat(rows).hasSize(300);
        assertThat(ids(rows)).isEqualTo(java.util.stream.IntStream.range(0, 300).mapToObj(PgVectorManifestRareLabelTest::chash)
            .collect(Collectors.toList()));
    }

    @Test
    void aNegativeFilter_isStillAnsweredByTheStageAlone_overTheWidenedCut() {
        // $ne has no in-SQL form: it rejects little, and the stage answers it exactly over the 4x cut. The nearest
        // 40 chunks are all `body`, so nothing survives; the four non-body chunks sit past the cut. This pins the
        // documented residual (ManifestLabelStage "Known cost"), the one the in-SQL EXISTS does not close.
        assertThat(search(10, where("section_type", op("$ne", "body")))).isEmpty();
        // The same filter with a limit that reaches them (widen(500) = 1200 < 1500) still stops at the ceiling.
        assertThat(ids(search(500, where("section_type", op("$ne", "body")))))
            .doesNotContain(chash(N - 1), chash(1700));
    }
}

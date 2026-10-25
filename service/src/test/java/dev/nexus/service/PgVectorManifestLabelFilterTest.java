// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
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
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tko2w (RDR-108): the search {@code where} on {@code section_type}, {@code section_title} and
 * {@code page_number} is answered per (doc_id, position) from the catalog manifest, not from the shared T3
 * chunk row, whose metadata is whichever document wrote last.
 *
 * <p>Fixture. One chunk text X is held by two documents with different labels: document A labels it
 * {@code references} (page 9), document B labels it {@code body} (page 3). The shared T3 row carries B's
 * label ({@code body}), the stale last-writer value, so a filter that reads the chunk row would keep X for
 * document A under {@code section_type!=references}. Around it: Y (only in A, {@code body}, language tag
 * {@code en}), Z (only in B, no label anywhere), W (only in B, {@code abstract}) and V (only in A, manifest
 * label NULL, chunk row says {@code references}: a row written before catalog-046).
 *
 * <p>Ranking is pinned by registering the texts at known unit vectors: X, then Y, Z, V, W by distance.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestLabelFilterTest {

    private static final String SVC_ROLE = "svc_label_filter";
    private static final String SVC_PASS = "svc_label_filter_pass";

    private static final String TENANT = "label-filter";
    private static final String COLLECTION = "knowledge__label-filter__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String DOC_A = "label-doc-a";
    private static final String DOC_B = "label-doc-b";

    // A shared lexeme in every text so the hybrid path's text gate admits all of them.
    private static final String TEXT_X = "labelprobe shared chunk held by two documents";
    private static final String TEXT_Y = "labelprobe chunk only document a holds";
    private static final String TEXT_Z = "labelprobe chunk only document b holds, unlabelled";
    private static final String TEXT_V = "labelprobe chunk with a legacy label on the chunk row only";
    private static final String TEXT_W = "labelprobe chunk only document b holds, the abstract";
    private static final String QUERY = "labelprobe";

    private static final String X = Chash.ofText("label-filter-x").toHex();
    private static final String Y = Chash.ofText("label-filter-y").toHex();
    private static final String Z = Chash.ofText("label-filter-z").toHex();
    private static final String V = Chash.ofText("label-filter-v").toHex();
    private static final String W = Chash.ofText("label-filter-w").toHex();

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
        // Query is (1, 0): distance grows X < Y < Z < V < W.
        embedder.register(TEXT_X, 1.0f, 0.0f);
        embedder.register(TEXT_Y, 0.9539392f, 0.3f);
        embedder.register(TEXT_Z, 0.8f, 0.6f);
        embedder.register(TEXT_V, 0.6f, 0.8f);
        embedder.register(TEXT_W, 0.3f, 0.9539392f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            insertDocument(seed, DOC_A, "Document A");
            insertDocument(seed, DOC_B, "Document B");
        }

        // The shared row carries B's label: the last writer's. V's row carries a legacy label.
        Map<String, Object> mx = new LinkedHashMap<>();
        mx.put("section_type", "body");
        mx.put("section_title", "Methods");
        mx.put("page_number", 3);
        Map<String, Object> my = new LinkedHashMap<>();
        my.put("section_type", "body");
        my.put("lang", "en");
        Map<String, Object> mv = new LinkedHashMap<>();
        mv.put("section_type", "references");
        vec.upsertChunks(TENANT, COLLECTION, List.of(X, Y, Z, V, W),
            List.of(TEXT_X, TEXT_Y, TEXT_Z, TEXT_V, TEXT_W), List.of(mx, my, Map.of(), mv, Map.of()));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, DOC_A, 0, X, "References", "references", 9);
            manifest(seed, DOC_A, 1, Y, "Intro", "body", 1);
            manifest(seed, DOC_A, 2, V, null, null, null);
            manifest(seed, DOC_B, 0, X, "Methods", "body", 3);
            manifest(seed, DOC_B, 1, Z, null, null, null);
            manifest(seed, DOC_B, 2, W, "Abstract", "abstract", 1);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void insertDocument(DSLContext seed, String tumbler, String title) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + tumbler + ".md")
            .execute();
    }

    private static void manifest(DSLContext seed, String doc, int position, String chash,
                                 String title, String type, Integer page) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, title)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, type)
            .set(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, page)
            .execute();
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private static String label(Map<String, Object> row) {
        String chash = (String) row.get("id");
        String name = chash.equals(X) ? "X" : chash.equals(Y) ? "Y" : chash.equals(Z) ? "Z"
                    : chash.equals(V) ? "V" : chash.equals(W) ? "W" : chash;
        return name + "@" + row.get("doc_id");
    }

    private static List<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(PgVectorManifestLabelFilterTest::label).collect(Collectors.toList());
    }

    private static Map<String, Object> where(String key, Object value) {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put(key, value);
        return w;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    private static Map<String, Object> rowOf(List<Map<String, Object>> rows, String label) {
        return rows.stream().filter(r -> label(r).equals(label)).findFirst().orElseThrow();
    }

    // ── plain search ────────────────────────────────────────────────────────

    @Test
    void negativeFilter_dropsOnlyTheReferencesOccurrence_keepsTheOtherOwnersOfAShared_chunk() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_type", op("$ne", "references")));

        // X survives for B (body) and not for A (references). The shared chunk row says "body": a filter
        // reading it would have kept X@A too. Z is unlabelled anywhere and survives; V's manifest label
        // is NULL, so it takes the chunk row's legacy label (references) and is dropped, as it was before.
        assertThat(labels(rows)).containsExactly("X@" + DOC_B, "Y@" + DOC_A, "Z@" + DOC_B, "W@" + DOC_B);
    }

    @Test
    void positiveFilter_mirrorsTheNegativeOne_perOccurrence() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_type", "references"));

        // X@A (manifest: references) and V@A (legacy chunk-row label: references); never X@B.
        assertThat(labels(rows)).containsExactly("X@" + DOC_A, "V@" + DOC_A);
    }

    @Test
    void eachOwnerKeepsItsOwnLabel_onTheRowItIsReturnedOn() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10,
            where("section_type", op("$nin", List.of("nothing"))));

        var xa = rowOf(rows, "X@" + DOC_A);
        var xb = rowOf(rows, "X@" + DOC_B);
        assertThat(xa.get("section_type")).isEqualTo("references");
        assertThat(xa.get("section_title")).isEqualTo("References");
        assertThat(((Number) xa.get("page_number")).intValue()).isEqualTo(9);
        assertThat(xb.get("section_type")).isEqualTo("body");
        assertThat(xb.get("section_title")).isEqualTo("Methods");
        assertThat(((Number) xb.get("page_number")).intValue()).isEqualTo(3);
        assertThat(xa.get("position")).isEqualTo(0);
    }

    @Test
    void includeSourceUri_namesTheDocumentEachExpandedRowSpeaksFor() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10,
            where("section_type", op("$nin", List.of("nothing"))), true).value();

        assertThat(rowOf(rows, "X@" + DOC_A).get("source_uri")).isEqualTo("file:///" + DOC_A + ".md");
        assertThat(rowOf(rows, "X@" + DOC_B).get("source_uri")).isEqualTo("file:///" + DOC_B + ".md");
    }

    @Test
    void unlabelledChunk_survivesTheNegativeFilter_andIsDroppedByAPositiveOne() {
        var neg = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_type", op("$ne", "abstract")));
        assertThat(labels(neg)).contains("Z@" + DOC_B);

        var pos = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_type", "body"));
        assertThat(labels(pos)).containsExactly("X@" + DOC_B, "Y@" + DOC_A);
    }

    @Test
    void limit_countsAfterExpansion() {
        // Every occurrence passes. X expands to two rows, so a cut of 3 taken before the expansion
        // (X, Y, Z -> four rows) or applied per hit would not give three rows.
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 3,
            where("section_type", op("$ne", "nothing")));

        assertThat(labels(rows)).containsExactly("X@" + DOC_A, "X@" + DOC_B, "Y@" + DOC_A);
    }

    @Test
    void pageNumber_isFilteredPerOccurrence_numerically() {
        var ge = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("page_number", op("$gte", 3)));
        assertThat(labels(ge)).containsExactly("X@" + DOC_A, "X@" + DOC_B);

        var eq = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("page_number", 1));
        assertThat(labels(eq)).containsExactly("Y@" + DOC_A, "W@" + DOC_B);

        // The stored 9 equals the operand "9": the planner's tolerant typing is kept.
        var str = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("page_number", "9"));
        assertThat(labels(str)).containsExactly("X@" + DOC_A);
    }

    @Test
    void sectionTitle_isFilteredPerOccurrence() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_title", "Methods"));
        assertThat(labels(rows)).containsExactly("X@" + DOC_B);
    }

    @Test
    void aLabelFilterComposesWithAnOrdinaryMetadataKey() {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put("lang", "en");
        w.put("section_type", op("$ne", "references"));
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, w);

        assertThat(labels(rows)).containsExactly("Y@" + DOC_A);
    }

    @Test
    void aWhereWithoutLabelKeys_isUntouched_oneRowPerChunk_noDocId() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, null);

        assertThat(rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toList()))
            .containsExactly(X, Y, Z, V, W);
        assertThat(rows).allSatisfy(r -> assertThat(r).doesNotContainKey("doc_id"));
    }

    @Test
    void anUnsupportedLabelOperator_isRefusedLikeAnyOtherWhere() {
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class, () ->
            vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where("section_type", op("$regex", "x"))));
    }

    // ── hybrid and fan-out ──────────────────────────────────────────────────

    @Test
    void hybridSearch_answersTheSameFilter() {
        var rows = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10,
            where("section_type", op("$ne", "references"))).value();

        assertThat(labels(rows)).containsExactlyInAnyOrder(
            "X@" + DOC_B, "Y@" + DOC_A, "Z@" + DOC_B, "W@" + DOC_B);
    }

    @Test
    void searchPerCollection_answersTheSameFilter_andCutsAfterExpansion() {
        var all = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null,
            where("section_type", op("$ne", "references")), false).rows();
        assertThat(labels(all)).containsExactly("X@" + DOC_B, "Y@" + DOC_A, "Z@" + DOC_B, "W@" + DOC_B);

        var cut = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 3, null,
            where("section_type", op("$ne", "nothing")), false).rows();
        assertThat(labels(cut)).containsExactly("X@" + DOC_A, "X@" + DOC_B, "Y@" + DOC_A);
    }

    @Test
    void aTombstonedOwner_isNotAnOccurrence() throws Exception {
        // Document B is tombstoned: X must then be answered by A alone, and B's other chunks vanish.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(DOC_B)))
                .execute();
            try {
                var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10,
                    where("section_type", op("$ne", "nothing")));
                assertThat(labels(rows)).containsExactly("X@" + DOC_A, "Y@" + DOC_A, "V@" + DOC_A);
            } finally {
                DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                    .setNull(CATALOG_DOCUMENTS.DELETED_AT)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(DOC_B)))
                    .execute();
            }
        }
    }
}

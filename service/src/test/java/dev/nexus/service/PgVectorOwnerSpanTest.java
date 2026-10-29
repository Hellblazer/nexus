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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.26 (RDR-236 Phase 3, engine half): a chunk-level search row's citation address, its
 * {@code source_uri} and its span ({@code line_start}, {@code line_end}, {@code chunk_start_char},
 * {@code chunk_end_char}), comes from ONE manifest owner, chosen deterministically inside the searched
 * collection, not from the tenant-wide last writer and not from the shared chunk row.
 *
 * <p>Fixture. Chunk X is held by document P (lines 10-20) and document P2 (lines 30-35) in collection C,
 * and by document Q (lines 40-50) in collection D, written in that order, so Q is the LAST manifest row of
 * the tenant. C's chunk row carries P2's span (the last writer inside C). A search in C must answer P: the
 * first owner by (doc_id, position), whatever the write order. Around it: N (owner P, every manifest span
 * NULL, a legacy row: keeps the chunk row's), M (owner P, lines only: the chars the manifest lacks stay the
 * chunk row's) and K (P at two positions, the lower one wins). A chunk with no manifest row at all is never
 * a search hit (the live-owner predicate drops it), so that fallback is pinned on the pure half in
 * {@code ManifestLabelStageTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorOwnerSpanTest {

    private static final String SVC_ROLE = "svc_owner_span";
    private static final String SVC_PASS = "svc_owner_span_pass";

    private static final String TENANT = "owner-span";
    private static final String COLL_C = "knowledge__owner-span-c__minilm-l6-v2-384__v1";
    private static final String COLL_D = "knowledge__owner-span-d__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String DOC_P = "span-doc-p";
    private static final String DOC_P2 = "span-doc-p2";
    private static final String DOC_Q = "span-doc-q";
    /** Sorts after {@link #DOC_P}; holds chunk H with every manifest span field NULL. */
    private static final String DOC_P3 = "span-doc-p3";
    /** A tumbler-shaped pair: "1.10" sorts before "1.2" as text, after it as numbers. */
    private static final String DOC_T10 = "1.10";
    private static final String DOC_T2 = "1.2";

    /** A second tenant holding X in the same collection name, under a doc_id that sorts before every other. */
    private static final String OTHER_TENANT = "owner-span-other";
    private static final String DOC_OTHER = "000-other-tenant";

    private static final String TEXT_X = "spanprobe shared chunk held across two collections";
    private static final String TEXT_N = "spanprobe chunk whose manifest row carries no span";
    private static final String TEXT_M = "spanprobe chunk whose manifest row carries lines only";
    private static final String TEXT_K = "spanprobe chunk one document holds at two positions";
    private static final String TEXT_H = "spanprobe chunk a later document holds with no span of its own";
    private static final String TEXT_T = "spanprobe chunk two tumbler-shaped documents hold";
    private static final String QUERY = "spanprobe";

    private static final String X = Chash.ofText("owner-span-x").toHex();
    private static final String N = Chash.ofText("owner-span-n").toHex();
    private static final String M = Chash.ofText("owner-span-m").toHex();
    private static final String K = Chash.ofText("owner-span-k").toHex();
    private static final String H = Chash.ofText("owner-span-h").toHex();
    private static final String T = Chash.ofText("owner-span-t").toHex();

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
        // Query is (1, 0): distance grows X < N < M < K.
        embedder.register(TEXT_X, 1.0f, 0.0f);
        embedder.register(TEXT_N, 0.9539392f, 0.3f);
        embedder.register(TEXT_M, 0.8f, 0.6f);
        embedder.register(TEXT_K, 0.6f, 0.8f);
        embedder.register(TEXT_H, 0.28f, 0.96f);
        embedder.register(TEXT_T, 0.0f, 1.0f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLL_C);
            PgContainerHelper.insertCollection(seed, TENANT, COLL_D);
            insertDocument(seed, DOC_P, COLL_C);
            insertDocument(seed, DOC_P2, COLL_C);
            insertDocument(seed, DOC_Q, COLL_D);
            insertDocument(seed, DOC_P3, COLL_C);
            insertDocument(seed, DOC_T10, COLL_C);
            insertDocument(seed, DOC_T2, COLL_C);
            PgContainerHelper.insertCollection(seed, OTHER_TENANT, COLL_C);
            insertDocument(seed, OTHER_TENANT, DOC_OTHER, COLL_C);
        }

        // C's chunk rows: X carries P2's span (the last writer inside C); N, M, K carry the span the
        // fallback must keep. D's X row carries Q's.
        Map<String, Object> xInC = span(30, 35, 300, 350);
        xInC.put("chunk_text_hash", X);
        Map<String, Object> xInD = span(40, 50, 400, 500);
        xInD.put("chunk_text_hash", X);
        vec.upsertChunks(TENANT, COLL_C, List.of(X, N, M, K, H, T),
            List.of(TEXT_X, TEXT_N, TEXT_M, TEXT_K, TEXT_H, TEXT_T),
            List.of(xInC, span(7, 8, 70, 80), span(61, 71, 601, 701),
                    span(99, 99, 990, 999), span(5, 6, 50, 60), span(3, 4, 30, 40)));
        vec.upsertChunks(TENANT, COLL_D, List.of(X), List.of(TEXT_X), List.of(xInD));
        // The same text in the same collection name under another tenant (a chunk row is tenant-keyed).
        Map<String, Object> xOther = span(900, 901, 9000, 9010);
        xOther.put("chunk_text_hash", X);
        vec.upsertChunks(OTHER_TENANT, COLL_C, List.of(X), List.of(TEXT_X), List.of(xOther));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            // Q's row is written LAST: a tenant-wide, last-row-wins join answers Q for a search in C.
            manifest(seed, DOC_P, COLL_C, 0, X, "body", 10, 20, 100, 200);
            manifest(seed, DOC_P2, COLL_C, 0, X, "references", 30, 35, 300, 350);
            manifest(seed, DOC_P, COLL_C, 1, N, "body", null, null, null, null);
            manifest(seed, DOC_P, COLL_C, 2, M, "body", 60, 70, null, null);
            // K at position 4 is written before position 2; the lower position must win.
            manifest(seed, DOC_P, COLL_C, 4, K, "body", 80, 81, 800, 810);
            manifest(seed, DOC_P, COLL_C, 3, K, "body", 82, 83, 820, 830);
            manifest(seed, DOC_Q, COLL_D, 0, X, "body", 40, 50, 400, 500);
            // H: P holds it with a span, P3 (a higher doc_id) with none; the chunk row carries 5-6 / 50-60.
            manifest(seed, DOC_P, COLL_C, 5, H, "body", 110, 120, 1100, 1200);
            manifest(seed, DOC_P3, COLL_C, 0, H, "body", null, null, null, null);
            // T: written "1.2" first; the text-order owner is "1.10".
            manifest(seed, DOC_T2, COLL_C, 0, T, "body", 200, 201, 2000, 2010);
            manifest(seed, DOC_T10, COLL_C, 0, T, "body", 300, 301, 3000, 3010);
            // Another tenant holds X in the same collection name under the lowest doc_id there is.
            manifest(seed, OTHER_TENANT, DOC_OTHER, COLL_C, 0, X, "body", 900, 901, 9000, 9010);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static Map<String, Object> span(int lineStart, int lineEnd, int charStart, int charEnd) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("line_start", lineStart);
        m.put("line_end", lineEnd);
        m.put("chunk_start_char", charStart);
        m.put("chunk_end_char", charEnd);
        return m;
    }

    private static void insertDocument(DSLContext seed, String tumbler, String collection) {
        insertDocument(seed, TENANT, tumbler, collection);
    }

    private static void insertDocument(DSLContext seed, String tenant, String tumbler, String collection) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, tumbler)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, uri(tumbler))
            .execute();
    }

    private static String uri(String doc) {
        return "file:///" + doc + ".md";
    }

    private static void manifest(DSLContext seed, String doc, String collection, int position, String chash,
                                 String type, Integer lineStart, Integer lineEnd, Integer charStart,
                                 Integer charEnd) {
        manifest(seed, TENANT, doc, collection, position, chash, type, lineStart, lineEnd, charStart, charEnd);
    }

    private static void manifest(DSLContext seed, String tenant, String doc, String collection, int position,
                                 String chash, String type, Integer lineStart, Integer lineEnd,
                                 Integer charStart, Integer charEnd) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, type)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .set(CATALOG_DOCUMENT_CHUNKS.LINE_START, lineStart)
            .set(CATALOG_DOCUMENT_CHUNKS.LINE_END, lineEnd)
            .set(CATALOG_DOCUMENT_CHUNKS.CHAR_START, charStart)
            .set(CATALOG_DOCUMENT_CHUNKS.CHAR_END, charEnd)
            .execute();
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private static Map<String, Object> rowFor(List<Map<String, Object>> rows, String chash, String collection) {
        return rows.stream()
            .filter(r -> chash.equals(r.get("id")) && collection.equals(r.get("collection")))
            .findFirst().orElseThrow(() -> new AssertionError("no row for " + chash + " in " + collection));
    }

    private static void assertSpan(Map<String, Object> row, int ls, int le, int cs, int ce) {
        assertThat(((Number) row.get("line_start")).intValue()).as("line_start").isEqualTo(ls);
        assertThat(((Number) row.get("line_end")).intValue()).as("line_end").isEqualTo(le);
        assertThat(((Number) row.get("chunk_start_char")).intValue()).as("chunk_start_char").isEqualTo(cs);
        assertThat(((Number) row.get("chunk_end_char")).intValue()).as("chunk_end_char").isEqualTo(ce);
    }

    private static Map<String, Object> where(String key, Object value) {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put(key, value);
        return w;
    }

    private static Map<String, Object> anyLabel() {
        return where("section_type", where("$ne", "nothing"));
    }

    // ── the pin: one collection-scoped owner, deterministically ─────────────

    @Test
    void searchInC_namesPsSourceUriAndPsSpan_notQsAndNotTheChunkRows() {
        // Repeated: the old join returned whichever row Postgres happened to emit last.
        for (int run = 0; run < 5; run++) {
            var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value();
            var x = rowFor(rows, X, COLL_C);
            assertThat(x.get("source_uri")).as("run " + run).isEqualTo(uri(DOC_P));
            assertSpan(x, 10, 20, 100, 200);
        }
    }

    @Test
    void searchInD_namesQ_andASearchOverBothCollectionsAnswersEachRowFromItsOwn() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C, COLL_D), 20, null, true).value();

        var inC = rowFor(rows, X, COLL_C);
        assertThat(inC.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(inC, 10, 20, 100, 200);
        var inD = rowFor(rows, X, COLL_D);
        assertThat(inD.get("source_uri")).isEqualTo(uri(DOC_Q));
        assertSpan(inD, 40, 50, 400, 500);
    }

    @Test
    void theSpanIsTheManifests_evenWhenSourceUriIsNotAsked() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, false).value();
        var x = rowFor(rows, X, COLL_C);

        assertThat(x).doesNotContainKey("source_uri");
        assertSpan(x, 10, 20, 100, 200);
        // The citation span string is text-determined and untouched.
        assertThat(x.get("span")).isEqualTo("chash:" + X);
    }

    @Test
    void hybridSearch_answersTheSameOwner() {
        var rows = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value();
        var x = rowFor(rows, X, COLL_C);

        assertThat(x.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(x, 10, 20, 100, 200);
    }

    @Test
    void searchPerCollection_answersTheSameOwner() {
        var rows = vec.searchPerCollection(TENANT, QUERY, List.of(COLL_C, COLL_D), 10, 20, null, null, true).rows();

        var inC = rowFor(rows, X, COLL_C);
        assertThat(inC.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(inC, 10, 20, 100, 200);
        var inD = rowFor(rows, X, COLL_D);
        assertThat(inD.get("source_uri")).isEqualTo(uri(DOC_Q));
        assertSpan(inD, 40, 50, 400, 500);
    }

    @Test
    void theLowerPositionOfTheFirstOwnerWins() {
        var k = rowFor(vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value(), K, COLL_C);

        assertThat(k.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(k, 82, 83, 820, 830);
    }

    // ── legacy fallbacks ────────────────────────────────────────────────────

    @Test
    void aManifestRowWithoutSpans_keepsTheChunkRowsSpan_butStillNamesItsOwner() {
        var n = rowFor(vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value(), N, COLL_C);

        assertThat(n.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(n, 7, 8, 70, 80);
    }

    @Test
    void aManifestRowWithOnlyLines_replacesTheLines_andKeepsTheChunkRowsChars() {
        var m = rowFor(vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value(), M, COLL_C);

        assertSpan(m, 60, 70, 601, 701);
    }

    // ── an expanded row speaks for one owner in every position field ────────

    @Test
    void underALabelFilter_eachExpandedRowCarriesItsOwnersSpanAndUri() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), true).value();

        var xs = rows.stream().filter(r -> X.equals(r.get("id"))).toList();
        assertThat(xs).hasSize(2);
        var p = xs.stream().filter(r -> DOC_P.equals(r.get("doc_id"))).findFirst().orElseThrow();
        var p2 = xs.stream().filter(r -> DOC_P2.equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertThat(p.get("source_uri")).isEqualTo(uri(DOC_P));
        assertSpan(p, 10, 20, 100, 200);
        assertThat(p2.get("source_uri")).isEqualTo(uri(DOC_P2));
        assertSpan(p2, 30, 35, 300, 350);
    }

    @Test
    void underALabelFilter_theSpanIsTheOwnersEvenWhenSourceUriIsNotAsked() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), false).value();

        var p = rows.stream().filter(r -> X.equals(r.get("id")) && DOC_P.equals(r.get("doc_id")))
            .findFirst().orElseThrow();
        assertSpan(p, 10, 20, 100, 200);
        assertThat(p).doesNotContainKey("source_uri");
    }

    @Test
    void underALabelFilter_aLegacyOwnerKeepsTheChunkRowsSpan() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), true).value();

        var n = rows.stream().filter(r -> N.equals(r.get("id"))).findFirst().orElseThrow();
        assertThat(n.get("doc_id")).isEqualTo(DOC_P);
        assertSpan(n, 7, 8, 70, 80);
    }

    @Test
    void hybridUnderALabelFilter_expandedRowsCarryTheirOwnersSpan() {
        var rows = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), true).value();

        var p2 = rows.stream().filter(r -> X.equals(r.get("id")) && DOC_P2.equals(r.get("doc_id")))
            .findFirst().orElseThrow();
        assertSpan(p2, 30, 35, 300, 350);
        assertThat(p2.get("source_uri")).isEqualTo(uri(DOC_P2));
    }

    // ── an unfiltered row names its owner (the engine, not the client, picks the document) ──

    @Test
    void anUnfilteredRow_namesItsOwner_withThatOwnersSpanAndUri() {
        for (int run = 0; run < 5; run++) {
            var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C, COLL_D), 20, null, true).value();
            var inC = rowFor(rows, X, COLL_C);
            assertThat(inC.get("doc_id")).as("run " + run).isEqualTo(DOC_P);
            assertThat(((Number) inC.get("position")).intValue()).isZero();
            assertThat(inC.get("source_uri")).isEqualTo(uri(DOC_P));
            assertSpan(inC, 10, 20, 100, 200);
            var inD = rowFor(rows, X, COLL_D);
            assertThat(inD.get("doc_id")).isEqualTo(DOC_Q);
            assertThat(inD.get("source_uri")).isEqualTo(uri(DOC_Q));
            assertSpan(inD, 40, 50, 400, 500);
        }
    }

    @Test
    void anUnfilteredRow_namesItsOwner_whenSourceUriIsNotAsked_onEveryRoute() {
        var plain = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, false).value();
        var hybrid = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, false).value();
        var fan = vec.searchPerCollection(TENANT, QUERY, List.of(COLL_C), 10, 20, null, null, false).rows();
        for (var rows : List.of(plain, hybrid, fan)) {
            var x = rowFor(rows, X, COLL_C);
            assertThat(x.get("doc_id")).isEqualTo(DOC_P);
            assertThat(x).doesNotContainKey("source_uri");
            assertSpan(x, 10, 20, 100, 200);
            // K is held by P at positions 3 and 4: the row names the lower one.
            var k = rowFor(rows, K, COLL_C);
            assertThat(k.get("doc_id")).isEqualTo(DOC_P);
            assertThat(((Number) k.get("position")).intValue()).isEqualTo(3);
        }
    }

    @Test
    void theOwnerIsTheLowestDocIdInTheDatabasesTextOrder_notTheNumericTumblerOrder() {
        var t = rowFor(vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value(), T, COLL_C);

        // "1.10" < "1.2" as text; numerically 1.2 comes first. The wire promises the text order.
        assertThat(t.get("doc_id")).isEqualTo(DOC_T10);
        assertThat(t.get("source_uri")).isEqualTo(uri(DOC_T10));
        assertSpan(t, 300, 301, 3000, 3010);
    }

    @Test
    void anotherTenantsOwner_isNeverNamed_evenWithTheLowestDocId() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value();
        var x = rowFor(rows, X, COLL_C);

        assertThat(x.get("doc_id")).isEqualTo(DOC_P);
        assertThat(x.get("source_uri")).isEqualTo(uri(DOC_P));
    }

    // ── a label filter: an owner with no recorded span shows the chunk row's OWN, not owner #1's ──

    @Test
    void underALabelFilter_anOwnerWithNullSpans_showsTheChunkRowsOwnSpan_notTheFirstOwners() {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), true).value();

        var hs = rows.stream().filter(r -> H.equals(r.get("id"))).toList();
        assertThat(hs).hasSize(2);
        var p = hs.stream().filter(r -> DOC_P.equals(r.get("doc_id"))).findFirst().orElseThrow();
        var p3 = hs.stream().filter(r -> DOC_P3.equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertSpan(p, 110, 120, 1100, 1200);
        assertSpan(p3, 5, 6, 50, 60);
        assertThat(p3.get("source_uri")).isEqualTo(uri(DOC_P3));
    }

    @Test
    void hybridAndFanOutUnderALabelFilter_anOwnerWithNullSpans_showsTheChunkRowsOwnSpan() {
        var hybrid = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, anyLabel(), true).value();
        var fan = vec.searchPerCollection(TENANT, QUERY, List.of(COLL_C), 10, 20, null, anyLabel(), true).rows();
        for (var rows : List.of(hybrid, fan)) {
            var p3 = rows.stream().filter(r -> H.equals(r.get("id")) && DOC_P3.equals(r.get("doc_id")))
                .findFirst().orElseThrow();
            assertSpan(p3, 5, 6, 50, 60);
        }
    }

    // ── get and get_where: source_uris take the same collection-scoped owner ──

    @Test
    @SuppressWarnings("unchecked")
    void getById_namesTheOwnerInTheCollection_andTheMetadatasKeepTheChunkRowsOwnSpan() {
        var env = vec.get(TENANT, COLL_C, List.of(X, K), 10, 0, true);

        List<String> ids = (List<String>) env.get("ids");
        List<String> uris = (List<String>) env.get("source_uris");
        assertThat(uris.get(ids.indexOf(X))).isEqualTo(uri(DOC_P));
        assertThat(uris.get(ids.indexOf(K))).isEqualTo(uri(DOC_P));
        // The envelope returns the chunk rows' metadata as stored: C's X row carries P2's span.
        var metas = (List<Map<String, Object>>) env.get("metadatas");
        assertThat(((Number) metas.get(ids.indexOf(X)).get("line_start")).intValue()).isEqualTo(30);

        var inD = vec.get(TENANT, COLL_D, List.of(X), 10, 0, true);
        assertThat((List<String>) inD.get("source_uris")).containsExactly(uri(DOC_Q));
    }

    @Test
    @SuppressWarnings("unchecked")
    void getWhere_namesTheOwnerInTheCollection_notTheTenantWideLastRow() {
        for (int run = 0; run < 5; run++) {
            var env = vec.getWhere(TENANT, COLL_C, null, 100, 0, true);
            List<String> ids = (List<String>) env.get("ids");
            List<String> uris = (List<String>) env.get("source_uris");
            assertThat(uris.get(ids.indexOf(X))).as("run " + run).isEqualTo(uri(DOC_P));
            assertThat(uris.get(ids.indexOf(T))).isEqualTo(uri(DOC_T10));
        }
        var inD = vec.getWhere(TENANT, COLL_D, null, 100, 0, true);
        assertThat((List<String>) inD.get("source_uris")).containsExactly(uri(DOC_Q));
    }

    // ── a tombstoned owner owns nothing ─────────────────────────────────────

    @Test
    void aTombstonedFirstOwner_passesTheCitationToTheNext() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(DOC_P)))
                .execute();
            try {
                var x = rowFor(vec.searchWithTokens(TENANT, QUERY, List.of(COLL_C), 10, null, true).value(),
                    X, COLL_C);
                assertThat(x.get("source_uri")).isEqualTo(uri(DOC_P2));
                assertSpan(x, 30, 35, 300, 350);
            } finally {
                dsl.update(CATALOG_DOCUMENTS)
                    .setNull(CATALOG_DOCUMENTS.DELETED_AT)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(DOC_P)))
                    .execute();
            }
        }
    }
}

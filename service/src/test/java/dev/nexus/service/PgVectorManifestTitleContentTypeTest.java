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
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.25 (RDR-236 Phase 3, Sam's rulings nexus-6pfgb.2 and nexus-6pfgb.3): the search {@code where} on
 * {@code title} and {@code content_type} is answered per owning document from the catalog, not from the shared
 * T3 chunk row, whose metadata is whichever document wrote last.
 *
 * <p>Fixture. Chunk S1 is shared by A (a code file, title {@code alpha.py}) and B (a note, title {@code B note});
 * the shared row carries B's title and B's {@code content_type}, the last writer's. Chunk S2 is shared by C (a PDF,
 * catalog type {@code paper}, {@code c.pdf}) and D (a markdown file, catalog type {@code prose}, {@code d.md});
 * its row carries D's. P1 is A's alone, PC is C's alone, PD is D's alone. E is a legacy document: an empty catalog
 * title, no catalog content type and no file path, so every answer falls back to its chunk row
 * ({@code title legacy-title}, {@code content_type pdf}).
 *
 * <p>Rules under test: {@code title} is the catalog title and falls back to the row ONLY while the catalog title is
 * empty; {@code content_type=pdf} / {@code markdown} answer from the format the catalog derives (the file
 * extension first, else the catalog type) with the row as the fallback when it derives none; every other value
 * ({@code code}, {@code prose}, {@code paper}, ...) answers from {@code catalog_documents.content_type}, so
 * {@code prose} includes a markdown file.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestTitleContentTypeTest {

    private static final String SVC_ROLE = "svc_title_ct";
    private static final String SVC_PASS = "svc_title_ct_pass";

    private static final String TENANT = "title-ct";
    private static final String COLLECTION = "docs__title-ct__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String DOC_A = "tct-doc-a";
    private static final String DOC_B = "tct-doc-b";
    private static final String DOC_C = "tct-doc-c";
    private static final String DOC_D = "tct-doc-d";
    private static final String DOC_E = "tct-doc-e";
    private static final String DOC_F = "tct-doc-f";

    private static final String TEXT_S1 = "tctprobe shared chunk held by code file a and note b";
    private static final String TEXT_P1 = "tctprobe chunk only code file a holds";
    private static final String TEXT_S2 = "tctprobe shared chunk held by pdf c and markdown d";
    private static final String TEXT_PC = "tctprobe chunk only pdf c holds";
    private static final String TEXT_PD = "tctprobe chunk only markdown d holds";
    private static final String TEXT_PE = "tctprobe chunk only legacy document e holds";
    private static final String TEXT_PF = "tctprobe chunk only the answerless document f holds";
    private static final String QUERY = "tctprobe";

    private static final String S1 = Chash.ofText("tct-s1").toHex();
    private static final String P1 = Chash.ofText("tct-p1").toHex();
    private static final String S2 = Chash.ofText("tct-s2").toHex();
    private static final String PC = Chash.ofText("tct-pc").toHex();
    private static final String PD = Chash.ofText("tct-pd").toHex();
    private static final String PE = Chash.ofText("tct-pe").toHex();
    private static final String PF = Chash.ofText("tct-pf").toHex();

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
        // Query is (1, 0): distance grows S1 < P1 < S2 < PC < PD < PE.
        embedder.register(TEXT_S1, 1.0f, 0.0f);
        embedder.register(TEXT_P1, 0.9539392f, 0.3f);
        embedder.register(TEXT_S2, 0.8f, 0.6f);
        embedder.register(TEXT_PC, 0.6f, 0.8f);
        embedder.register(TEXT_PD, 0.3f, 0.9539392f);
        embedder.register(TEXT_PE, 0.0f, 1.0f);
        embedder.register(TEXT_PF, -0.6f, 0.8f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            doc(seed, DOC_A, "alpha.py", "code", "src/alpha.py");
            doc(seed, DOC_B, "B note", "knowledge", "");
            doc(seed, DOC_C, "c.pdf", "paper", "papers/c.pdf");
            doc(seed, DOC_D, "d.md", "prose", "notes/d.md");
            // E: legacy. An empty catalog title (the columns are NOT NULL, so ''), no content type, no path.
            doc(seed, DOC_E, "", "", "");
            // F: no catalog answer at all, and (below) a chunk row that carries no title and no content_type.
            doc(seed, DOC_F, "", "", "");
        }

        vec.upsertChunks(TENANT, COLLECTION, List.of(S1, P1, S2, PC, PD, PE, PF),
            List.of(TEXT_S1, TEXT_P1, TEXT_S2, TEXT_PC, TEXT_PD, TEXT_PE, TEXT_PF),
            List.of(
                row("title", "B note", "content_type", "knowledge"),
                row("title", "stale-p1", "content_type", "prose"),
                row("title", "d.md", "content_type", "markdown"),
                row("title", "stale-pc", "content_type", "markdown"),
                row("title", "stale-pd", "content_type", "pdf"),
                row("title", "legacy-title", "content_type", "pdf"),
                row()));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, DOC_A, 0, S1);
            manifest(seed, DOC_A, 1, P1);
            manifest(seed, DOC_B, 0, S1);
            manifest(seed, DOC_C, 0, S2);
            manifest(seed, DOC_C, 1, PC);
            manifest(seed, DOC_D, 0, S2);
            manifest(seed, DOC_D, 1, PD);
            manifest(seed, DOC_E, 0, PE);
            manifest(seed, DOC_F, 0, PF);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void doc(DSLContext seed, String tumbler, String title, String contentType, String filePath) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.CONTENT_TYPE, contentType)
            .set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + tumbler)
            .execute();
    }

    private static void manifest(DSLContext seed, String doc, int position, String chash) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false)
            .execute();
    }

    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    private static String label(Map<String, Object> r) {
        String chash = (String) r.get("id");
        String name = chash.equals(S1) ? "S1" : chash.equals(P1) ? "P1" : chash.equals(S2) ? "S2"
                    : chash.equals(PC) ? "PC" : chash.equals(PD) ? "PD" : chash.equals(PE) ? "PE" : chash.equals(PF) ? "PF" : chash;
        String doc = (String) r.get("doc_id");
        return name + "@" + (doc == null ? "none" : doc.substring(doc.length() - 1).toUpperCase());
    }

    private static List<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(PgVectorManifestTitleContentTypeTest::label).collect(Collectors.toList());
    }

    private List<Map<String, Object>> search(Map<String, Object> where) {
        return vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where);
    }

    private void expect(Map<String, Object> where, String... expected) {
        assertThat(labels(search(where))).as("search where %s", where).containsExactly(expected);
        assertThat(labels(vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10, where).value()))
            .as("hybrid where %s", where).containsExactlyInAnyOrder(expected);
        assertThat(labels(vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null, where, false)
            .rows())).as("per-collection where %s", where).containsExactly(expected);
    }

    // ── title ────────────────────────────────────────────────────────────────

    @Test
    void title_answersFromTheCatalogTitle_aSharedChunkIsTheCodeFilesNotTheNotes() {
        // S1's row says "B note" (B wrote last). A's catalog title is its filename: A owns S1 and P1, B does not.
        expect(Map.of("title", "alpha.py"), "S1@A", "P1@A");
        // B's catalog title is the note's: the row's value is the same text, but only B owns it.
        expect(Map.of("title", "B note"), "S1@B");
        // A row's stale value is not a title.
        expect(Map.of("title", "stale-p1"));
    }

    @Test
    void title_fallsBackToTheRowOnlyWhileTheCatalogTitleIsEmpty() {
        expect(Map.of("title", "legacy-title"), "PE@E");
        // The fallback is the row's value for the legacy document alone, never for a document with a title.
        expect(Map.of("title", "d.md"), "S2@D", "PD@D");
    }

    @Test
    void title_operators() {
        expect(Map.of("title", op("$in", List.of("alpha.py", "c.pdf"))), "S1@A", "P1@A", "S2@C", "PC@C");
        expect(Map.of("title", op("$ne", "alpha.py")), "S1@B", "S2@C", "S2@D", "PC@C", "PD@D", "PE@E", "PF@F");
    }

    // ── content_type ─────────────────────────────────────────────────────────

    @Test
    void contentTypePdf_aPdfAndAMarkdownShareAChunk_thePdfIsTheOnlyOwnerReturned() {
        // S2's row says markdown. C's file is a .pdf (catalog type paper), D's a .md (catalog type prose).
        // PE has no catalog answer at all, so its row's "pdf" stands.
        expect(Map.of("content_type", "pdf"), "S2@C", "PC@C", "PE@E");
    }

    @Test
    void contentTypeMarkdown_isTheMarkdownFile() {
        expect(Map.of("content_type", "markdown"), "S2@D", "PD@D");
    }

    @Test
    void theCatalogWords_answerFromTheCatalogContentType_proseIncludesAMarkdownFile() {
        expect(Map.of("content_type", "code"), "S1@A", "P1@A");
        expect(Map.of("content_type", "prose"), "S2@D", "PD@D");
        expect(Map.of("content_type", "paper"), "S2@C", "PC@C");
        expect(Map.of("content_type", "knowledge"), "S1@B");
        expect(Map.of("content_type", op("$in", List.of("code", "paper"))), "S1@A", "P1@A", "S2@C", "PC@C");
        expect(Map.of("content_type", op("$ne", "code")), "S1@B", "S2@C", "S2@D", "PC@C", "PD@D", "PE@E", "PF@F");
    }

    @Test
    void contentTypeEmpty_aDocumentWithNoAnswerAnywhereIsTheEmptyString_asTheStageReadsIt() {
        // F has a blank catalog type, no derivable format and a chunk row without content_type. The stage treats a
        // missing value as "", so the pushed EXISTS must admit it too (a superset of the stage's keep set).
        expect(Map.of("content_type", ""), "PF@F");
        expect(Map.of("content_type", op("$in", List.of("", "code"))), "S1@A", "P1@A", "PF@F");
    }

    @Test
    void expandedRows_speakForTheirOwner() {
        var rows = search(Map.of("title", op("$ne", "zzz")));
        var s1a = rows.stream().filter(r -> label(r).equals("S1@A")).findFirst().orElseThrow();
        var s1b = rows.stream().filter(r -> label(r).equals("S1@B")).findFirst().orElseThrow();
        assertThat(s1a.get("title")).isEqualTo("alpha.py");
        assertThat(s1b.get("title")).isEqualTo("B note");
        // The legacy document keeps the row's title.
        assertThat(rows.stream().filter(r -> label(r).equals("PE@E")).findFirst().orElseThrow().get("title"))
            .isEqualTo("legacy-title");
    }

    @Test
    void sameAnswersOnTheHnswRoute() {
        // The cardinality router sends a fixture this small down the exact plan; pin it off so the HNSW walk runs.
        PgSession.overrideSearchExactMaxRowsForTests(0);
        try {
            expect(Map.of("title", "alpha.py"), "S1@A", "P1@A");
            expect(Map.of("content_type", "pdf"), "S2@C", "PC@C", "PE@E");
        } finally {
            PgSession.resetSearchExactMaxRowsForTests();
        }
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.24 (epic nexus-6pfgb): the search {@code where} on a DOCUMENT-level key (author, bib_*,
 * indexed_at, and the catalog meta keys) is answered per owning document from the catalog, not from the
 * shared T3 chunk row, whose metadata is whichever document wrote last. The positional label keys are
 * pinned in {@link PgVectorManifestLabelFilterTest}.
 *
 * <p>Fixture. Three documents. A: author Alice, indexed 2026-03, enriched (bib_year 2021, 40 citations), a
 * full meta ({@code tags=alpha}, {@code category=cat-a}, ttl 30, overridden, mineru, ...). B: the markdown
 * shape: empty author, indexed 2025-01, unenriched, meta {@code tags=beta} only. C: legacy: no author, no
 * meta at all, unenriched. Chunks: S (A and B), P (A only), Q (B only), L (C only). Every chunk row
 * carries LAST-WRITER or STALE values that disagree with the catalog (S's row says {@code tags=beta}
 * and A's bib; P's row says {@code tags=stale-row}, bib_year 1999 and a 2030 indexed_at; Q's and L's rows carry a bib_year of their own although B and C are unenriched), so a reader
 * that fell back to the row where the catalog answers fails a case below.
 *
 * <p>The three rules under test: a document with the meta key answers from it; a document WITHOUT the key
 * (or with an empty author, or no indexed_at) falls back to the chunk row, exactly as {@code labels_written
 * = false} does; bib_* is authoritative, so an unenriched document has no bib and its chunk row's is never
 * read.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestDocKeyFilterTest {

    private static final String SVC_ROLE = "svc_dockey_filter";
    private static final String SVC_PASS = "svc_dockey_filter_pass";

    private static final String TENANT = "dockey-filter";
    private static final String COLLECTION = "knowledge__dockey-filter__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String DOC_A = "dockey-doc-a";
    private static final String DOC_B = "dockey-doc-b";
    private static final String DOC_C = "dockey-doc-c";

    private static final String TEXT_S = "dockeyprobe shared chunk held by documents a and b";
    private static final String TEXT_P = "dockeyprobe chunk only document a holds";
    private static final String TEXT_Q = "dockeyprobe chunk only document b holds";
    private static final String TEXT_L = "dockeyprobe chunk only legacy document c holds";
    private static final String QUERY = "dockeyprobe";

    private static final String S = Chash.ofText("dockey-filter-s").toHex();
    private static final String P = Chash.ofText("dockey-filter-p").toHex();
    private static final String Q = Chash.ofText("dockey-filter-q").toHex();
    private static final String L = Chash.ofText("dockey-filter-l").toHex();

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
        // Query is (1, 0): distance grows S < P < Q < L.
        embedder.register(TEXT_S, 1.0f, 0.0f);
        embedder.register(TEXT_P, 0.9539392f, 0.3f);
        embedder.register(TEXT_Q, 0.8f, 0.6f);
        embedder.register(TEXT_L, 0.6f, 0.8f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            // A: enriched, full meta.
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_A)
                .set(CATALOG_DOCUMENTS.TITLE, "Document A")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + DOC_A + ".md")
                .set(CATALOG_DOCUMENTS.AUTHOR, "Alice")
                .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse("2026-03-01T00:00:00Z"))
                .set(CATALOG_DOCUMENTS.BIB_YEAR, 2021)
                .set(CATALOG_DOCUMENTS.BIB_AUTHORS, "Alice A")
                .set(CATALOG_DOCUMENTS.BIB_VENUE, "NeurIPS")
                .set(CATALOG_DOCUMENTS.BIB_CITATION_COUNT, 40)
                .set(CATALOG_DOCUMENTS.BIB_DOI, "10.1/a")
                .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT, OffsetDateTime.parse("2026-03-02T00:00:00Z"))
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(
                    "{\"tags\":\"alpha\",\"category\":\"cat-a\",\"ttl_days\":30,\"quality_gate_overridden\":true,"
                    + "\"extraction_method\":\"mineru\",\"extraction_source\":\"dt_a\",\"source_agent\":\"agent-a\","
                    + "\"frecency_score\":0.9}"))
                .execute();
            // B: the markdown shape, one meta key.
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_B)
                .set(CATALOG_DOCUMENTS.TITLE, "Document B")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + DOC_B + ".md")
                .set(CATALOG_DOCUMENTS.AUTHOR, "")
                .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse("2025-01-01T00:00:00Z"))
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{\"tags\":\"beta\"}"))
                .execute();
            // C: legacy, no author, no meta.
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_C)
                .set(CATALOG_DOCUMENTS.TITLE, "Document C")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + DOC_C + ".md")
                .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse("2024-06-01T00:00:00Z"))
                .execute();
        }

        Map<String, Object> ms = row("tags", "beta", "category", "cat-b", "ttl_days", 0,
            "quality_gate_overridden", false, "extraction_method", "docling", "source_agent", "agent-b",
            "source_author", "Row Author", "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 2021,
            "bib_citation_count", 40, "frecency_score", 0.1);
        Map<String, Object> mp = row("tags", "stale-row", "category", "stale-row", "source_author", "Stale Row",
            "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 1999);
        Map<String, Object> mq = row("tags", "beta", "category", "cat-b", "ttl_days", 0,
            "quality_gate_overridden", false, "extraction_method", "docling", "source_agent", "agent-b",
            "source_author", "Row Author", "bib_year", 2033);
        Map<String, Object> ml = row("tags", "legacy", "category", "legacy-cat", "ttl_days", 7,
            "quality_gate_overridden", true, "extraction_method", "legacy-x", "source_author", "Legacy Author",
            "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 2030);
        vec.upsertChunks(TENANT, COLLECTION, List.of(S, P, Q, L),
            List.of(TEXT_S, TEXT_P, TEXT_Q, TEXT_L), List.of(ms, mp, mq, ml));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, DOC_A, 0, S, "body", true);
            manifest(seed, DOC_A, 1, P, "references", true);
            manifest(seed, DOC_B, 0, S, null, false);
            manifest(seed, DOC_B, 1, Q, null, false);
            manifest(seed, DOC_C, 0, L, null, false);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static void manifest(DSLContext seed, String doc, int position, String chash, String type,
                                 boolean labelsWritten) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, type)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, labelsWritten)
            .execute();
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private static String label(Map<String, Object> r) {
        String chash = (String) r.get("id");
        String name = chash.equals(S) ? "S" : chash.equals(P) ? "P" : chash.equals(Q) ? "Q"
                    : chash.equals(L) ? "L" : chash;
        String doc = (String) r.get("doc_id");
        String owner = doc == null ? "none" : doc.substring(doc.length() - 1).toUpperCase();
        return name + "@" + owner;
    }

    private static List<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(PgVectorManifestDocKeyFilterTest::label).collect(Collectors.toList());
    }

    private static Map<String, Object> where(Object... kv) {
        return row(kv);
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    private List<Map<String, Object>> search(Map<String, Object> where) {
        return vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where);
    }

    private void expect(Map<String, Object> where, String... expected) {
        assertThat(labels(search(where))).as("where %s", where).containsExactly(expected);
    }

    private static Map<String, Object> rowOf(List<Map<String, Object>> rows, String label) {
        return rows.stream().filter(r -> label(r).equals(label)).findFirst().orElseThrow();
    }

    // ── the bead's three failing-first pins ─────────────────────────────────

    @Test
    void sharedChunk_withDifferentMetaTags_isReturnedOnlyForTheOwnerWhoseTagsMatch() {
        // S's chunk row says tags=beta (the last writer, B). A's catalog meta says alpha.
        expect(where("tags", "alpha"), "S@A", "P@A");
        expect(where("tags", "beta"), "S@B", "Q@B");
    }

    @Test
    void bibYear_isAuthoritative_anUnenrichedDocumentHasNoBibAndItsChunkRowIsNeverRead() {
        // A enriched 2021; B and C unenriched although the chunk rows S (A's bib), Q (2033) and L (2030)
        // carry a bib_year. P's own row says 1999 and A's catalog says 2021.
        expect(where("bib_year", op("$gte", 2020)), "S@A", "P@A");
        expect(where("bib_year", op("$lte", 2000))); // an unenriched document is not year 0
        // The negative operator keeps an unenriched document (unlabelled).
        expect(where("bib_year", op("$ne", 2021)), "S@B", "Q@B", "L@C");
    }

    @Test
    void aDocumentWithoutTheMetaKey_fallsBackToTheChunkRowsValue() {
        // B has meta tags only: its category, ttl_days, extraction_method fall back to the row (S, Q).
        expect(where("category", "cat-b"), "S@B", "Q@B");
        expect(where("category", "cat-a"), "S@A", "P@A");
        expect(where("extraction_method", "docling"), "S@B", "Q@B");
        expect(where("extraction_method", "mineru"), "S@A", "P@A");
        // C has no meta at all: every meta key falls back to its row.
        expect(where("tags", "legacy"), "L@C");
        expect(where("quality_gate_overridden", true), "S@A", "P@A", "L@C");
        expect(where("ttl_days", op("$gt", 0)), "S@A", "P@A", "L@C");
        // An owner that HAS the key never reads the stale row beside it.
        expect(where("tags", "stale-row"));
        expect(where("category", "stale-row"));
    }

    // ── the rest of the document keys ───────────────────────────────────────

    @Test
    void negativeOperators_keepTheOtherOwners() {
        expect(where("tags", op("$ne", "alpha")), "S@B", "Q@B", "L@C");
        expect(where("tags", op("$nin", List.of("alpha", "beta"))), "L@C");
    }

    @Test
    void sourceAuthor_answersFromTheCatalogAuthor_andAnEmptyOrAbsentAuthorFallsBackToTheRow() {
        expect(where("source_author", "Alice"), "S@A", "P@A");
        // B's author is empty (markdown authors predate their home) and C's is null: both read the row.
        expect(where("source_author", "Row Author"), "S@B", "Q@B");
        expect(where("source_author", "Legacy Author"), "L@C");
        // A's catalog author is authoritative: P's row says "Stale Row".
        expect(where("source_author", "Stale Row"));
    }

    @Test
    void indexedAt_answersFromTheCatalogColumn_neverTheRowsLastWriteTime() {
        // Every chunk row that carries one says 2030; the documents were indexed in 2026, 2025 and 2024.
        expect(where("indexed_at", op("$gte", "2026-01-01")), "S@A", "P@A");
        expect(where("indexed_at", op("$lt", "2025-06-01")), "S@B", "Q@B", "L@C");
    }

    @Test
    void bibColumns_answerPerDocument() {
        expect(where("bib_citation_count", op("$lt", 100)), "S@A", "P@A");
        expect(where("bib_doi", "10.1/a"), "S@A", "P@A");
        expect(where("bib_venue", "NeurIPS"), "S@A", "P@A");
        expect(where("bib_authors", op("$ne", "Alice A")), "S@B", "Q@B", "L@C");
    }

    @Test
    void metaScalars_keepTheirTypes() {
        expect(where("frecency_score", op("$gte", 0.5)), "S@A", "P@A");
        expect(where("ttl_days", 30), "S@A", "P@A");
        expect(where("source_agent", "agent-a"), "S@A", "P@A");
        expect(where("extraction_source", "dt_a"), "S@A", "P@A");
        expect(where("quality_gate_overridden", false), "S@B", "Q@B");
    }

    @Test
    void aDocumentKeyComposesWithALabelKey_perOccurrence() {
        expect(where("tags", "alpha", "section_type", "body"), "S@A");
        expect(where("tags", "alpha", "section_type", op("$ne", "body")), "P@A");
    }

    @Test
    void eachExpandedRow_speaksForItsOwner_onTheFilteredDocumentKeys() {
        var rows = search(where("tags", op("$ne", "zzz"), "bib_year", op("$ne", 1)));

        var sa = rowOf(rows, "S@A");
        var sb = rowOf(rows, "S@B");
        assertThat(sa.get("tags")).isEqualTo("alpha");
        assertThat(((Number) sa.get("bib_year")).intValue()).isEqualTo(2021);
        assertThat(sb.get("tags")).isEqualTo("beta");
        // B is unenriched: the row carries no bib_year of another document's.
        assertThat(sb).doesNotContainKey("bib_year");
    }

    @Test
    void hybridSearch_answersTheSameFilter() {
        var rows = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10,
            where("tags", "alpha", "bib_year", op("$gte", 2020))).value();

        assertThat(labels(rows)).containsExactlyInAnyOrder("S@A", "P@A");
    }

    @Test
    void searchPerCollection_answersTheSameFilter_andCutsAfterExpansion() {
        var all = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null,
            where("bib_year", op("$ne", 2021)), false).rows();
        assertThat(labels(all)).containsExactly("S@B", "Q@B", "L@C");

        var cut = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 3, null,
            where("tags", op("$ne", "zzz")), false).rows();
        assertThat(labels(cut)).containsExactly("S@A", "S@B", "P@A");
    }

    @Test
    void aWhereWithoutDocumentOrLabelKeys_isUntouched_oneRowPerChunk_noDocId() {
        var rows = search(null);

        assertThat(rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toList()))
            .containsExactly(S, P, Q, L);
        assertThat(rows).allSatisfy(r -> assertThat(r).doesNotContainKey("doc_id"));
    }
}

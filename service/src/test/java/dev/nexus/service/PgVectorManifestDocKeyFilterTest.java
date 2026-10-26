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
 * indexed_at) is answered per owning document from the catalog, not from the shared T3 chunk row, whose
 * metadata is whichever document wrote last. The positional label keys are pinned in {@link
 * PgVectorManifestLabelFilterTest}. The catalog meta keys ({@code tags} and the rest) have no writer yet and
 * are NOT answered here: they stay predicates on the chunk row, pinned below.
 *
 * <p>Fixture. Four documents. A: author Alice, indexed 2026-03, enriched (bib_year 2021, 40 citations,
 * s2 id {@code 0042}, openalex id {@code oa-a}). B: the markdown shape: empty author, indexed 2025-01,
 * unenriched. C: legacy: no author, unenriched. D: no author, indexed 2023-01, enriched ONLY by a set
 * {@code bib_enriched_at} (every bib column at its default). Chunks: S (A and B), P (A only), Q (B only),
 * L (C only), R (D only). Every chunk row carries LAST-WRITER or STALE values that disagree with the catalog
 * (S's row says A's bib and {@code tags=beta}; P's says bib_year 1999, a 2030 indexed_at; Q's, L's and R's
 * rows carry a bib_year of their own although B, C and D have none), so a reader that fell back to the row
 * where the catalog answers fails a case below.
 *
 * <p>The rules under test: {@code source_author} and {@code indexed_at} answer from the document and fall
 * back to the chunk row when the author is empty or there is no index time; bib_* is authoritative, so an
 * unenriched document has no bib and its chunk row's is never read.
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
    private static final String DOC_D = "dockey-doc-d";

    private static final String TEXT_S = "dockeyprobe shared chunk held by documents a and b";
    private static final String TEXT_P = "dockeyprobe chunk only document a holds";
    private static final String TEXT_Q = "dockeyprobe chunk only document b holds";
    private static final String TEXT_L = "dockeyprobe chunk only legacy document c holds";
    private static final String TEXT_R = "dockeyprobe chunk only enriched-stamp document d holds";
    private static final String QUERY = "dockeyprobe";

    private static final String S = Chash.ofText("dockey-filter-s").toHex();
    private static final String P = Chash.ofText("dockey-filter-p").toHex();
    private static final String Q = Chash.ofText("dockey-filter-q").toHex();
    private static final String L = Chash.ofText("dockey-filter-l").toHex();
    private static final String R = Chash.ofText("dockey-filter-r").toHex();

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
        // Query is (1, 0): distance grows S < P < Q < L < R.
        embedder.register(TEXT_S, 1.0f, 0.0f);
        embedder.register(TEXT_P, 0.9539392f, 0.3f);
        embedder.register(TEXT_Q, 0.8f, 0.6f);
        embedder.register(TEXT_L, 0.6f, 0.8f);
        embedder.register(TEXT_R, 0.3f, 0.9539392f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            // A: enriched. Its catalog meta says tags=alpha, which no search consults (not an answered key).
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
                .set(CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, "0042")
                .set(CATALOG_DOCUMENTS.BIB_OPENALEX_ID, "oa-a")
                .set(CATALOG_DOCUMENTS.BIB_DOI, "10.1/a")
                .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT, OffsetDateTime.parse("2026-03-02T00:00:00Z"))
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{\"tags\":\"alpha\"}"))
                .execute();
            // B: the markdown shape, empty author, unenriched.
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
            // C: legacy, no author, unenriched.
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_C)
                .set(CATALOG_DOCUMENTS.TITLE, "Document C")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + DOC_C + ".md")
                .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse("2024-06-01T00:00:00Z"))
                .execute();
            // D: enriched by bib_enriched_at alone: every bib column stays at its default (0 / '').
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_D)
                .set(CATALOG_DOCUMENTS.TITLE, "Document D")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + DOC_D + ".md")
                .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse("2023-01-01T00:00:00Z"))
                .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT, OffsetDateTime.parse("2026-03-03T00:00:00Z"))
                .execute();
        }

        Map<String, Object> ms = row("tags", "beta", "source_author", "Row Author",
            "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 2021, "bib_citation_count", 40);
        Map<String, Object> mp = row("tags", "stale-row", "source_author", "Stale Row",
            "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 1999);
        Map<String, Object> mq = row("tags", "beta", "source_author", "Row Author", "bib_year", 2033);
        Map<String, Object> ml = row("tags", "legacy", "source_author", "Legacy Author",
            "indexed_at", "2030-01-01T00:00:00+00:00", "bib_year", 2030);
        Map<String, Object> mr = row("source_author", "Row D", "indexed_at", "2030-01-01T00:00:00+00:00",
            "bib_year", 2040, "bib_citation_count", 99);
        vec.upsertChunks(TENANT, COLLECTION, List.of(S, P, Q, L, R),
            List.of(TEXT_S, TEXT_P, TEXT_Q, TEXT_L, TEXT_R), List.of(ms, mp, mq, ml, mr));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, DOC_A, 0, S, "body", true);
            manifest(seed, DOC_A, 1, P, "references", true);
            manifest(seed, DOC_B, 0, S, null, false);
            manifest(seed, DOC_B, 1, Q, null, false);
            manifest(seed, DOC_C, 0, L, null, false);
            manifest(seed, DOC_D, 0, R, null, false);
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
                    : chash.equals(L) ? "L" : chash.equals(R) ? "R" : chash;
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

    // ── the pins ────────────────────────────────────────────────────────────

    @Test
    void theMetaKeysAreStillAChunkRowFilter_theCatalogMetaIsNotConsulted_noDocId() {
        // No writer puts tags on a catalog document yet, so tags is not an answered key: it is the chunk
        // row's predicate inside the search, one row per chunk, no owner named. A's catalog meta says alpha
        // and B's beta; the rows say beta (S, Q), stale-row (P) and legacy (L).
        expect(where("tags", "beta"), "S@none", "Q@none");
        expect(where("tags", "legacy"), "L@none");
        expect(where("tags", "alpha"));
        assertThat(search(where("tags", "beta"))).allSatisfy(r -> assertThat(r).doesNotContainKey("doc_id"));
    }

    @Test
    void bibYear_isAuthoritative_anUnenrichedDocumentHasNoBibAndItsChunkRowIsNeverRead() {
        // A enriched 2021; B and C unenriched although the chunk rows S (A's bib), Q (2033) and L (2030)
        // carry a bib_year, and D's row says 2040 although D's year is not recorded. P's own row says 1999.
        expect(where("bib_year", op("$gte", 2020)), "S@A", "P@A");
        expect(where("bib_year", op("$lte", 2000))); // an unenriched document is not year 0
        // The negative operator keeps an unenriched document (unlabelled).
        expect(where("bib_year", op("$ne", 2021)), "S@B", "Q@B", "L@C", "R@D");
    }

    @Test
    void semanticScholarAndOpenAlexIds_areEachReadFromTheirOwnColumn_andCompareAsExactStrings() {
        expect(where("bib_semantic_scholar_id", "0042"), "S@A", "P@A");
        expect(where("bib_openalex_id", "oa-a"), "S@A", "P@A");
        // A swap of the two columns would answer these the other way round.
        expect(where("bib_semantic_scholar_id", "oa-a"));
        expect(where("bib_openalex_id", "0042"));
        // An id is a string: 0042 is not 42.
        expect(where("bib_semantic_scholar_id", "42"));
        expect(where("bib_semantic_scholar_id", op("$in", List.of("42", "7"))));
        expect(where("bib_semantic_scholar_id", op("$ne", "42")), "S@A", "S@B", "P@A", "Q@B", "L@C", "R@D");
    }

    @Test
    void aDocumentEnrichedByBibEnrichedAtAlone_isEnriched_itsDefaultsAreNotRecorded_itsZeroCitationsAreACount() {
        // D: bib_enriched_at set, year 0, citations 0, the rest ''. Not recorded: no year, no doi. A count: 0.
        expect(where("bib_citation_count", op("$lte", 0)), "R@D");
        expect(where("bib_citation_count", op("$lt", 100)), "S@A", "P@A", "R@D");
        expect(where("bib_year", op("$gte", 1)), "S@A", "P@A");
        expect(where("bib_doi", "10.1/a"), "S@A", "P@A");
        // B and C are unenriched: no count at all, so a negative operator keeps them and a range drops them.
        expect(where("bib_citation_count", op("$ne", 40)), "S@B", "Q@B", "L@C", "R@D");
    }

    @Test
    void anExplicitEmptyStringOperand_matchesAnUnrecordedValue_asTheChunkRowFilterDid() {
        // A has a venue; B and C are unenriched and D's venue is not recorded: all four are ''.
        expect(where("bib_venue", ""), "S@B", "Q@B", "L@C", "R@D");
        expect(where("bib_venue", op("$in", List.of("", "x"))), "S@B", "Q@B", "L@C", "R@D");
        expect(where("bib_venue", op("$ne", "")), "S@A", "P@A");
        expect(where("bib_venue", op("$nin", List.of(""))), "S@A", "P@A");
        // Any other operand keeps the unlabelled rule.
        expect(where("bib_venue", op("$ne", "NeurIPS")), "S@B", "Q@B", "L@C", "R@D");
        expect(where("bib_venue", "NeurIPS"), "S@A", "P@A");
    }

    @Test
    void aNumericKeyAcceptsTheNumberAsText_aStringKeyDoesNot() {
        expect(where("bib_year", "2021"), "S@A", "P@A");
        expect(where("bib_citation_count", "40"), "S@A", "P@A");
        expect(where("bib_doi", "10.1/a"), "S@A", "P@A");
    }

    @Test
    void sourceAuthor_answersFromTheCatalogAuthor_andAnEmptyOrAbsentAuthorFallsBackToTheRow() {
        expect(where("source_author", "Alice"), "S@A", "P@A");
        // B's author is empty (markdown authors predate their home), C's and D's are null: all read the row.
        expect(where("source_author", "Row Author"), "S@B", "Q@B");
        expect(where("source_author", "Legacy Author"), "L@C");
        expect(where("source_author", "Row D"), "R@D");
        // A's catalog author is authoritative: P's row says "Stale Row".
        expect(where("source_author", "Stale Row"));
    }

    @Test
    void indexedAt_answersFromTheCatalogColumn_neverTheRowsLastWriteTime() {
        // Every chunk row that carries one says 2030; the documents were indexed in 2026, 2025, 2024 and 2023.
        expect(where("indexed_at", op("$gte", "2026-01-01")), "S@A", "P@A");
        expect(where("indexed_at", op("$lt", "2025-06-01")), "S@B", "Q@B", "L@C", "R@D");
    }

    @Test
    void bibColumns_answerPerDocument() {
        expect(where("bib_venue", "NeurIPS"), "S@A", "P@A");
        expect(where("bib_authors", op("$ne", "Alice A")), "S@B", "Q@B", "L@C", "R@D");
    }

    @Test
    void aDocumentKeyComposesWithALabelKey_perOccurrence() {
        expect(where("source_author", "Alice", "section_type", "body"), "S@A");
        expect(where("source_author", "Alice", "section_type", op("$ne", "body")), "P@A");
    }

    @Test
    void eachExpandedRow_speaksForItsOwner_onTheFilteredDocumentKeys() {
        var rows = search(where("source_author", op("$ne", "zzz"), "bib_year", op("$ne", 1)));

        var sa = rowOf(rows, "S@A");
        var sb = rowOf(rows, "S@B");
        assertThat(sa.get("source_author")).isEqualTo("Alice");
        assertThat(((Number) sa.get("bib_year")).intValue()).isEqualTo(2021);
        // B has no author of its own: the shared row's stays. B is unenriched: no bib_year of another document's.
        assertThat(sb.get("source_author")).isEqualTo("Row Author");
        assertThat(sb).doesNotContainKey("bib_year");
        // D is enriched with no year recorded: the row's 2040 is dropped too.
        assertThat(rowOf(rows, "R@D")).doesNotContainKey("bib_year");
    }

    @Test
    void hybridSearch_answersTheSameFilter() {
        var rows = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10,
            where("source_author", "Alice", "bib_year", op("$gte", 2020))).value();

        assertThat(labels(rows)).containsExactlyInAnyOrder("S@A", "P@A");
    }

    @Test
    void searchPerCollection_answersTheSameFilter_andCutsAfterExpansion() {
        var all = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null,
            where("bib_year", op("$ne", 2021)), false).rows();
        assertThat(labels(all)).containsExactly("S@B", "Q@B", "L@C", "R@D");

        var cut = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 3, null,
            where("source_author", op("$ne", "zzz")), false).rows();
        assertThat(labels(cut)).containsExactly("S@A", "S@B", "P@A");
    }

    @Test
    void aWhereWithoutDocumentOrLabelKeys_isUntouched_oneRowPerChunk_noDocId() {
        var rows = search(null);

        assertThat(rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toList()))
            .containsExactly(S, P, Q, L, R);
        assertThat(rows).allSatisfy(r -> assertThat(r).doesNotContainKey("doc_id"));
    }
}

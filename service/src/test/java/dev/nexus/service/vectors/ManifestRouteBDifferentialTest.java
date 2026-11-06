// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
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
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-qpt52: THE GUARANTEE that route (b) answers every key {@link ManifestLabelStage} answers with the stage's own
 * semantics. Route (b) has no stage after the search, so the SQL is the answer; this test holds it to the stage.
 *
 * <p>One fixture, many predicates. For each predicate of a matrix (every answered key, every operator, operands drawn
 * from absent, null, empty, numbers, numeric strings, booleans, sets and ranges) the same fixture is judged three ways:
 * <ol>
 *   <li>by {@link ManifestLabelStage#apply}, the Java evaluation route (a) runs after its search: the set of admitted
 *       (document, chunk) pairs;</li>
 *   <li>by the real route (b) function through {@link PgVectorRepository#searchMetadataScoped}: its set of (document,
 *       chunk) pairs must EQUAL the stage's, for every predicate, AND its rows must have the multiplicity route (b) is
 *       documented to have (see below);</li>
 *   <li>by route (a)'s {@code manifest_search_384}, which admits a chunk when any live occurrence passes: with the
 *       predicate route (a) really pushes ({@link ManifestPushdown#of}) its chunk set must be a SUPERSET of the stage's
 *       chunks, and with the exact form ({@link ManifestPushdown#ofExact}) it must EQUAL them (content_type, whose
 *       route (a) array is deliberately wider, is held to the superset rule only).</li>
 * </ol>
 *
 * <p><b>Multiplicity.</b> The stage returns a (document, chunk) pair once, at the document's lowest passing position.
 * Route (b) returns one row per PASSING OCCURRENCE (manifest row), so a document holding one chunk at two positions
 * yields two rows when both positions pass, one when only one does (by design, documented on {@code
 * searchMetadataScopedWithTokens} and {@code searchAspectScopedWithTokens}: the callers keep the best row per
 * document). The fixture therefore holds documents with one chunk at two positions whose labels differ (written
 * against legacy, so the position's label and the chunk row's both matter), and each predicate is judged per
 * occurrence: {@link ManifestLabelStage#passes} over {@link ManifestLabelStage#rowLabels} of each live occurrence is
 * the oracle, and route (b)'s rows, counted per (document, chunk), must equal the number of occurrences that pass. A
 * route (b) function that read one position's labels for the pair (the first, the lowest, any aggregate) admits the
 * wrong set on the predicates where the two positions disagree.
 *
 * <p>The fixture is built to reach every branch of the stage's {@code testValue}: written and legacy manifest rows,
 * blank and non-blank titles and authors, catalog types with and without a derived format, documents with no type, a
 * soft-deleted owner, enriched and unenriched bib columns (a year of 0, a citation count of 0), an indexed_at that is
 * NULL (so the chunk row answers) and one that is not, catalog meta present, JSON null and absent, tags as an array
 * and as a comma string, numbers stored as text, chunk rows that disagree with the catalog, and one chunk held at two
 * positions of one document (two documents do), written at one and legacy at the other, or written at both.
 *
 * <p><b>Route (c) leg (nexus-u9dc5).</b> The same fixture and the same matrix are also judged through the get routes
 * ({@link PgVectorRepository#getAllMetadata} and {@link PgVectorRepository#getWhere}, both modes), which answer the
 * stage-answered keys through {@code nexus.manifest_admits_chunk} and have no stage after the read. They are compared on
 * the set of CHUNKS admitted (a get route returns chunks, not documents), against {@link ManifestLabelStage#apply}'s
 * chunk-level admission, and the fixture adds the chunks route (b) cannot see: four ORPHANS (no manifest row, and one
 * whose only owner is tombstoned), which the stage judges on the chunk row's own values. The physical-scan mode
 * ({@code includeNonLive}) must return exactly the stage's chunks, orphans included; the live mode exactly those among
 * them that have a live owner. A chunk key (not answered by the stage) must keep its row comparison.
 *
 * <p>Non-vacuity: the matrix has a floor in size, each of the eight operators has predicates that admit something and
 * predicates that reject something, the five keys of nexus-qpt52 (bib_year, bib_authors, bib_venue,
 * bib_citation_count, indexed_at) are each admitted and rejected by a positive and by a negative operator, and the
 * repeated chunks are seen both passing at both positions (two rows) and at one position only (one row).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestRouteBDifferentialTest {

    private static final String SVC_ROLE = "svc_diff_b";
    private static final String SVC_PASS = "svc_diff_b_pass";
    private static final String TENANT = "diff-b";
    private static final String COLL = "knowledge__diff-b__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final ObjectMapper JSON = new ObjectMapper();

    private static final List<String> OPERATORS = List.of("$eq", "$ne", "$in", "$nin", "$gte", "$gt", "$lte", "$lt");

    /**
     * Checks (a repeated pair against a predicate) that must see the pair pass at both positions, and at one only;
     * measured 2,899 and 313 over the 5,778 predicates and the two pairs.
     */
    private static final int REPEAT_BOTH_FLOOR = 1000;
    private static final int REPEAT_ONE_FLOOR = 200;

    /** The five keys nexus-qpt52 added to the object. */
    private static final List<String> NEW_KEYS =
        List.of("bib_year", "bib_authors", "bib_venue", "bib_citation_count", "indexed_at");

    /** All unit vectors (1, 0, ...): the ranking is irrelevant, every page is complete. */
    private static final class Flat implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (String t : texts) {
                float[] v = new float[384];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }
    }

    // ── the fixture ─────────────────────────────────────────────────────────

    private record Doc(String id, String title, String author, String contentType, String filePath, String metadata,
                       int bibYear, String bibAuthors, String bibVenue, int bibCitations, String s2, String openAlex,
                       String doi, String enrichedAt, String indexedAt, boolean deleted) {}

    private static Doc doc(String id, String title, String author, String contentType, String filePath, String metadata) {
        return new Doc(id, title, author, contentType, filePath, metadata, 0, "", "", 0, "", "", "", null, null, false);
    }

    private static final List<Doc> DOCS = List.of(
        // plain code file, indexed_at with microseconds, no meta
        new Doc("d01", "Alpha", "Alice", "code", "a.py", null, 0, "", "", 0, "", "", "", null,
            "2026-01-15T10:00:00.123456Z", false),
        // markdown registered as prose: fully enriched, rich meta
        new Doc("d02", "Beta", "Bob", "prose", "b.md",
            "{\"tags\":[\"arch\",\"decision\"],\"category\":\"decision\",\"ttl_days\":7,\"frecency_score\":0.5,"
                + "\"source_agent\":\"claude\",\"extraction_method\":\"docling\",\"quality_gate_overridden\":true,"
                + "\"extraction_source\":\"pdf\"}",
            2020, "Smith, J.", "NeurIPS", 42, "0042", "W1", "10.1/x", "2026-02-01T00:00:00Z",
            "2025-12-31T23:59:59.999999Z", false),
        // blank title and author, pdf, enriched by stamp with zero citations, text-stored numbers, a comma tag string
        new Doc("d03", "", "", "pdf", "c.pdf",
            "{\"tags\":\"x, y ,x\",\"category\":5,\"ttl_days\":\"7\",\"frecency_score\":\"0.50\",\"quality_gate_overridden\":\"true\"}",
            0, "", "ICML", 0, "", "", "", "2026-03-01T00:00:00Z", null, false),
        // enriched by value (year, citations), no stamp; meta keys present but null or empty
        new Doc("d04", "Delta", "Dee", "paper", "d.pdf",
            "{\"tags\":[],\"category\":null,\"ttl_days\":null,\"source_agent\":\"\"}",
            2021, "", "", 3, "", "", "", null, "2026-06-01T00:00:00Z", false),
        // a note: no extension, no format; unenriched; boolean in the other spelling
        new Doc("d05", "Echo", "Eve", "knowledge", null,
            "{\"tags\":[\"arch\"],\"quality_gate_overridden\":\"True\"}", 0, "", "", 0, "", "", "", null, null, false),
        // no type, no extension: the chunk row answers content_type; whitespace title
        doc("d06", "  ", null, null, null, null),
        // rdr text file: prose format; enriched by year only, venue empty
        new Doc("d07", "Foxtrot", "Fay", "rdr", "r.txt", "{\"frecency_score\":1000,\"ttl_days\":7.0}",
            1999, "Zed", "", 0, "", "", "", null, "2024-02-29T12:30:45.5Z", false),
        // upper-case extension: pdf format whatever the catalog type
        new Doc("d08", "Golf", "Gus", "code", "x.PDF", "{\"tags\":\"a,b\"}", 0, "", "", 0, "", "", "10.2/y", null,
            "2026-01-15T10:00:00.123456Z", false),
        // typed prose with no extension; ids as text with leading zeros
        new Doc("d09", "Hotel", "Hal", "prose", "noext", "{\"category\":\"decision\",\"ttl_days\":30}",
            0, "", "", 0, "007", "W2", "", null, null, false),
        // markdown by extension under a code type
        new Doc("d10", "India", "Ivy", "code", "readme.md", "{}", 0, "", "", 0, "", "", "", null,
            "2026-11-03T00:00:00Z", false),
        // soft-deleted: owns nothing
        new Doc("d11", "Gamma", "Gil", "prose", "g.md", null, 2020, "Ghost", "Boo", 9, "", "", "", null, null, true),
        // a second holder of the shared chunks, labels all legacy
        new Doc("d12", "Juliet", "Jo", "prose", "j.txt", "{\"tags\":[\"b\",\"c\"]}", 0, "", "", 0, "", "", "", null,
            "2026-01-15T10:00:00Z", false),
        // holds chunk 1 at TWO positions (written body at one, legacy at the other); enriched, no indexed_at
        new Doc("d13", "Kilo", "Kim", "prose", "k.md", "{\"tags\":[\"a\",\"b\"],\"category\":\"decision\"}",
            2020, "Kay, K.", "NeurIPS", 5, "", "", "", "2026-04-01T00:00:00Z", null, false),
        // a second holder of chunk 1 at two positions (legacy at one, written with empty labels at the other)
        new Doc("d14", "Lima", "Lee", "code", "l.py", null, 0, "", "", 0, "", "", "", null,
            "2026-02-02T02:02:02.000001Z", false));

    /** Chunk-row metadata, as stored (the last writer's, and sometimes disagreeing with the catalog). */
    private static final List<String> CHUNK_META = List.of(
        /* 00 */ "{}",
        /* 01 */ "{\"source_key\":\"sk-shared\",\"section_type\":\"references\",\"section_title\":\"Intro\",\"page_number\":3,\"source_author\":\"Eve\","
                  + "\"title\":\"Row Title\",\"content_type\":\"prose\",\"indexed_at\":\"2026-03-03T03:03:03.000000+00:00\","
                  + "\"category\":\"row-cat\",\"tags\":\"r1,r2\"}",
        /* 02 */ "{\"source_key\":\"sk-shared\",\"section_type\":null,\"page_number\":\"5\",\"content_type\":\"pdf\",\"ttl_days\":\"9\","
                  + "\"frecency_score\":0.25,\"bib_year\":1990}",
        /* 03 */ "{\"content_type\":\"markdown\",\"quality_gate_overridden\":\"true\",\"source_agent\":\"row-agent\","
                  + "\"extraction_method\":\"m\",\"extraction_source\":\"e\",\"tags\":[\"rt\"]}",
        /* 04 */ "{\"content_type\":\"\",\"section_title\":\"\",\"indexed_at\":\"\"}",
        /* 05 */ "{\"content_type\":null,\"page_number\":5.0,\"section_type\":\"body\"}",
        /* 06 */ "{\"section_type\":\"body\",\"section_title\":\"Methods\",\"page_number\":12,\"content_type\":\"code\","
                  + "\"source_author\":\"\",\"title\":\"\",\"indexed_at\":\"2026-01-15T10:00:00.123456+00:00\"}",
        /* 07 */ "{\"page_number\":\"abc\",\"section_type\":\"appendix\",\"ttl_days\":7,\"frecency_score\":\"1000\","
                  + "\"quality_gate_overridden\":false,\"tags\":\"\"}",
        /* 08 */ "{\"content_type\":\"prose\",\"category\":\"decision\",\"source_agent\":\"claude\"}");

    /** First chunk index of the chunks route (b) cannot see (nexus-u9dc5): ORPHANS, judged on their own row. */
    private static final int FIRST_EXTRA_CHUNK = 20;

    /**
     * Chunk-row metadata of the route (c) extras. Three have no manifest row at all; the fourth is held only by the
     * tombstoned document d11, which owns nothing. Every answered key appears on at least one of them, in the forms
     * the stage reads (text, numbers, numeric text, a boolean, a list, null and empty), and the first and the fourth
     * carry values a live owner's catalog would contradict, so a catalog read of a non-owner would be seen.
     */
    private static final List<String> EXTRA_META = List.of(
        /* 20 */ "{\"source_key\":\"sk-shared\",\"section_type\":\"references\",\"section_title\":\"Intro\",\"page_number\":3,"
                  + "\"source_author\":\"Eve\",\"title\":\"Row Title\",\"content_type\":\"pdf\","
                  + "\"indexed_at\":\"2026-03-03T03:03:03.000000+00:00\",\"category\":\"row-cat\",\"tags\":\"r1,r2\","
                  + "\"bib_year\":1990,\"bib_authors\":\"Row A\",\"bib_venue\":\"Row V\",\"bib_citation_count\":4,"
                  + "\"bib_doi\":\"10.1/x\",\"bib_openalex_id\":\"W1\",\"bib_semantic_scholar_id\":\"0042\",\"ttl_days\":\"9\","
                  + "\"frecency_score\":0.25,\"quality_gate_overridden\":\"true\",\"source_agent\":\"row-agent\","
                  + "\"extraction_method\":\"m\",\"extraction_source\":\"e\"}",
        /* 21 */ "{}",
        /* 22 */ "{\"content_type\":\"markdown\",\"section_type\":\"\",\"page_number\":\"abc\",\"tags\":[\"a\",\"b\"],"
                  + "\"bib_year\":\"2021\",\"quality_gate_overridden\":false,\"indexed_at\":\"2026-06-01T00:00:00.000000+00:00\","
                  + "\"title\":\"Alpha\",\"source_author\":\"Alice\",\"category\":null,\"ttl_days\":7,\"frecency_score\":\"1000\"}",
        /* 23 */ "{\"source_key\":\"sk-shared\",\"section_type\":\"body\",\"section_title\":\"Methods\",\"page_number\":\"12\","
                  + "\"content_type\":\"prose\",\"title\":\"Gamma\",\"source_author\":\"Gil\",\"bib_year\":2020,"
                  + "\"bib_authors\":\"Ghost\",\"bib_venue\":\"Boo\",\"tags\":\"arch\",\"category\":\"decision\"}");

    /** A manifest occurrence: (document, position, chunk, written, section_title, section_type, page_number). */
    private record Occ(String doc, int position, int chunk, boolean written, String sectionTitle, String sectionType,
                       Integer page) {}

    private static Occ w(String doc, int pos, int chunk, String title, String type, Integer page) {
        return new Occ(doc, pos, chunk, true, title, type, page);
    }

    private static Occ l(String doc, int pos, int chunk) {
        return new Occ(doc, pos, chunk, false, null, null, null);
    }

    private static final List<Occ> OCCS = List.of(
        w("d01", 0, 0, "Intro", "body", 1), w("d01", 1, 6, "", "references", null), l("d01", 2, 1),
        w("d02", 0, 1, "Intro", "body", 3), w("d02", 1, 2, null, "", 5), w("d02", 2, 3, "Methods", "appendix", 7),
        w("d03", 0, 2, "", null, 5), w("d03", 1, 4, null, null, 0), l("d03", 2, 5),
        w("d04", 0, 6, "Intro", "references", 12), l("d04", 1, 7), w("d04", 2, 8, null, "", null),
        w("d05", 0, 3, "Notes", "body", null), l("d05", 1, 0),
        w("d06", 0, 4, null, null, null), l("d06", 1, 5), l("d06", 2, 8),
        w("d07", 0, 7, "Abstract", "appendix", 2), w("d07", 1, 1, "Intro", "tail", 3),
        l("d08", 0, 2), w("d08", 1, 6, "Methods", "body", 12),
        w("d09", 0, 8, "Intro", "body", 4), l("d09", 1, 4),
        w("d10", 0, 3, "Readme", "docs", 1), w("d10", 1, 0, "Intro", "references", 1),
        w("d11", 0, 1, "Intro", "body", 3), w("d11", 1, 6, "Methods", "body", 12),
        l("d12", 0, 1), l("d12", 1, 6), l("d12", 2, 8),
        // nexus-qpt52 review H1: one chunk at two positions of one document, labels differing between the positions.
        // d13 holds chunk 1 written (Methods, body, page 12) and legacy (the chunk row says Intro, references, 3).
        w("d13", 0, 1, "Methods", "body", 12), l("d13", 1, 1), w("d13", 2, 6, null, "references", null),
        // d14 holds chunk 1 legacy and written empty (no title, no type, no page); chunk 3 is legacy at position 2.
        l("d14", 0, 1), w("d14", 1, 1, "", "", null), l("d14", 2, 3),
        // nexus-u9dc5: chunk 23 is held only by the tombstoned d11 (its catalog row, labels and tags own nothing).
        w("d11", 2, 23, "Tomb", "references", 99));

    /** The (document, chunk) pairs the fixture holds at more than one live position: the multiplicity probes. */
    private static final List<String> REPEATED = List.of("d13@" + chash(1), "d14@" + chash(1));

    private static String chash(int chunk) {
        return Chash.ofText("diff-b-chunk-" + chunk).toHex();
    }

    // ── harness ─────────────────────────────────────────────────────────────

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository vec;
    ManifestLabelStage stage;
    List<Map<String, Object>> chunkRows;

    /** The route (c) extras as the stage receives them: four chunks with no live owner (nexus-u9dc5). */
    List<Map<String, Object>> extraRows;

    @BeforeAll
    void start() throws Exception {
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
        tenantScope = new TenantScope(svcDs);
        var embedder = new Flat();
        vec = new PgVectorRepository(tenantScope, embedder, embedder);
        stage = new ManifestLabelStage(tenantScope);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLL);
            chunkRows = new ArrayList<>();
            for (int i = 0; i < CHUNK_META.size(); i++) {
                seed.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COLL)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash(i)))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "diff-b chunk " + i)
                    .set(CHUNKS.METADATA, JSONB.valueOf(CHUNK_META.get(i)))
                    .set(CHUNKS.EMBEDDING_384, VectorSeeds.vecXY(384, 1f, 0f))
                    .execute();
                // The row the stage receives for this chunk: id, collection and the metadata flattened, as a search gives it.
                Map<String, Object> row = new LinkedHashMap<>(
                    JSON.readValue(CHUNK_META.get(i), new TypeReference<LinkedHashMap<String, Object>>() {}));
                row.put("id", chash(i));
                row.put("collection", COLL);
                chunkRows.add(row);
            }
            extraRows = new ArrayList<>();
            for (int i = 0; i < EXTRA_META.size(); i++) {
                int chunk = FIRST_EXTRA_CHUNK + i;
                seed.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COLL)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash(chunk)))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "diff-b chunk " + chunk)
                    .set(CHUNKS.METADATA, JSONB.valueOf(EXTRA_META.get(i)))
                    .set(CHUNKS.EMBEDDING_384, VectorSeeds.vecXY(384, 1f, 0f))
                    .execute();
                Map<String, Object> row = new LinkedHashMap<>(
                    JSON.readValue(EXTRA_META.get(i), new TypeReference<LinkedHashMap<String, Object>>() {}));
                row.put("id", chash(chunk));
                row.put("collection", COLL);
                extraRows.add(row);
            }
            for (Doc d : DOCS) {
                var insert = seed.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, d.id())
                    .set(CATALOG_DOCUMENTS.TITLE, d.title())
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLL)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + d.id() + ".md")
                    .set(CATALOG_DOCUMENTS.BIB_YEAR, d.bibYear())
                    .set(CATALOG_DOCUMENTS.BIB_AUTHORS, d.bibAuthors())
                    .set(CATALOG_DOCUMENTS.BIB_VENUE, d.bibVenue())
                    .set(CATALOG_DOCUMENTS.BIB_CITATION_COUNT, d.bibCitations())
                    .set(CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, d.s2())
                    .set(CATALOG_DOCUMENTS.BIB_OPENALEX_ID, d.openAlex())
                    .set(CATALOG_DOCUMENTS.BIB_DOI, d.doi());
                if (d.author() != null) insert = insert.set(CATALOG_DOCUMENTS.AUTHOR, d.author());
                if (d.contentType() != null) insert = insert.set(CATALOG_DOCUMENTS.CONTENT_TYPE, d.contentType());
                if (d.filePath() != null) insert = insert.set(CATALOG_DOCUMENTS.FILE_PATH, d.filePath());
                if (d.metadata() != null) insert = insert.set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(d.metadata()));
                if (d.enrichedAt() != null) {
                    insert = insert.set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT, OffsetDateTime.parse(d.enrichedAt()));
                }
                if (d.indexedAt() != null) insert = insert.set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse(d.indexedAt()));
                if (d.deleted()) insert = insert.set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(1));
                insert.execute();
            }
            for (Occ o : OCCS) {
                seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, o.doc())
                    .set(CATALOG_DOCUMENT_CHUNKS.POSITION, o.position())
                    .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash(o.chunk())))
                    .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLL)
                    .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, o.written() ? o.sectionTitle() : null)
                    .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, o.written() ? o.sectionType() : null)
                    .set(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, o.written() ? o.page() : null)
                    .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, o.written())
                    .execute();
            }
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── the three judges ────────────────────────────────────────────────────

    /** The (document, chunk) pairs the stage keeps; {@code doc@chash}. */
    private Set<String> stageDocChunks(List<ManifestLabelStage.Predicate> labels) {
        Set<String> out = new TreeSet<>();
        for (Map<String, Object> row : stage.apply(TENANT, chunkRows, labels, Integer.MAX_VALUE)) {
            out.add(row.get("doc_id") + "@" + row.get("id"));
        }
        return out;
    }

    /**
     * The rows route (b) returns, through the real repository path, as {@code doc@chash}, sorted, one entry per row (a
     * chunk held at two passing positions of one document comes back twice).
     */
    private List<String> routeBRows(Map<String, Object> where) {
        List<String> out = new ArrayList<>();
        for (Map<String, Object> r : vec.searchMetadataScoped(TENANT, "q", List.of(COLL), null, null, null, null, null,
                where, 500)) {
            out.add(r.get("id") + "@" + r.get("chash"));
        }
        Collections.sort(out);
        return out;
    }

    /** The live occurrences per docKeys, read once: the lookup does not depend on the predicate's operator or operand. */
    private final Map<Set<String>, Map<String, List<ManifestLabelStage.Occurrence>>> occurrenceCache = new HashMap<>();

    /**
     * The per-occurrence oracle: every live manifest occurrence of every chunk that passes the predicates on ITS OWN
     * labels ({@link ManifestLabelStage#passes} over {@link ManifestLabelStage#rowLabels}), as {@code doc@chash},
     * sorted, one entry per passing occurrence. A chunk at two passing positions of one document is listed twice.
     */
    private List<String> passingOccurrences(List<ManifestLabelStage.Predicate> labels) {
        Set<String> docKeys = ManifestLabelStage.docKeysOf(labels);
        Map<String, List<ManifestLabelStage.Occurrence>> owners = occurrenceCache.computeIfAbsent(docKeys,
            k -> stage.occurrences(TENANT, chunkRows, k));
        List<String> out = new ArrayList<>();
        for (Map<String, Object> row : chunkRows) {
            for (ManifestLabelStage.Occurrence o : owners.getOrDefault(
                    ManifestLabelStage.ownerKey(COLL, (String) row.get("id")), List.of())) {
                if (ManifestLabelStage.passes(labels, ManifestLabelStage.rowLabels(row, o, docKeys))) {
                    out.add(o.docId() + "@" + row.get("id"));
                }
            }
        }
        Collections.sort(out);
        return out;
    }

    /** The chunks {@code manifest_search_384} admits for a compiled plan. */
    private Set<String> routeAChunks(ManifestPushdown.Plan plan) {
        Vector q = VectorSeeds.vecXY(384, 1f, 0f);
        return tenantScope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            Set<String> out = new TreeSet<>();
            for (var r : ctx.selectFrom(MANIFEST_SEARCH_384.call(q, null, null, new String[] {COLL}, null, null,
                    plan.containment(), plan.jsonPath(), 1000, MODEL, TENANT, false)).fetch()) {
                out.add(r.get("id", String.class));
            }
            return out;
        });
    }

    private static Set<String> chunksOf(Set<String> docChunks) {
        Set<String> out = new TreeSet<>();
        for (String s : docChunks) out.add(s.substring(s.indexOf('@') + 1));
        return out;
    }

    // ── the matrix ──────────────────────────────────────────────────────────

    private static Map<String, Object> where(String key, Object value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(key, value);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    /** Operands every key is tried with: absent, null, empty, numbers in several forms, text, booleans. */
    private static final List<Object> GENERIC = java.util.Arrays.asList(
        "", null, "no-such-value", 5, 5.0, "5", "5.0", " 5 ", 0, 7, "7", -1, 12, 2020, "2020", 42, true, "true", "True",
        "x", "body", "references", "Alice", "decision", "prose", "pdf", "markdown", "code", "knowledge", "arch");

    /** Values the fixture holds for a key, which make the positive and negative operators admit and reject. */
    private static final Map<String, List<Object>> SPECIFIC = new TreeMap<>(Map.ofEntries(
        Map.entry("section_title", List.of("Intro", "Methods", "Notes", "Abstract", "Readme")),
        Map.entry("section_type", List.of("body", "references", "appendix", "tail", "docs")),
        Map.entry("page_number", List.of(1, 3, 12, "12", 2.5, "abc", "0")),
        Map.entry("source_author", List.of("Alice", "Bob", "Eve", "Dee", "Zed")),
        Map.entry("title", List.of("Alpha", "Beta", "Delta", "Row Title", "Hotel")),
        Map.entry("indexed_at", List.of("2026-01-15T10:00:00.123456+00:00", "2025-12-31T23:59:59.999999+00:00",
            "2026-03-03T03:03:03.000000+00:00", "2026-01-01", "2026-06-01T00:00:00.000000+00:00", "2024")),
        Map.entry("bib_year", List.of(2020, 2021, 1999, "2021", 2020.0)),
        Map.entry("bib_authors", List.of("Smith, J.", "Zed", "Ghost")),
        Map.entry("bib_venue", List.of("NeurIPS", "ICML", "Boo")),
        Map.entry("bib_citation_count", List.of(0, 3, 42, "42", 0.0)),
        Map.entry("bib_doi", List.of("10.1/x", "10.2/y")),
        Map.entry("bib_openalex_id", List.of("W1", "W2")),
        Map.entry("bib_semantic_scholar_id", List.of("0042", "42", "007", 42)),
        Map.entry("content_type", List.of("code", "prose", "knowledge", "pdf", "markdown", "rdr", "paper")),
        Map.entry("tags", List.of("arch", "decision", "x", "y", "a", "b", "c", "r1", "rt")),
        Map.entry("category", List.of("decision", "row-cat", 5, "5")),
        Map.entry("ttl_days", List.of(7, 30, 9, "9", 7.0)),
        Map.entry("source_agent", List.of("claude", "row-agent")),
        Map.entry("extraction_method", List.of("docling", "m")),
        Map.entry("quality_gate_overridden", List.of(true, false, "true", "True", "false")),
        Map.entry("extraction_source", List.of("pdf", "e")),
        Map.entry("frecency_score", List.of(0.5, 0.25, 1000, "1000", "0.5"))));

    /** One matrix entry: the where map and the stage predicates it splits into. */
    private record Entry(String name, Map<String, Object> where, List<ManifestLabelStage.Predicate> labels) {}

    private List<Entry> matrix() {
        List<Entry> out = new ArrayList<>();
        for (String key : new TreeSet<>(ManifestLabelStage.ANSWERED_KEYS)) {
            List<Object> operands = new ArrayList<>(GENERIC);
            operands.addAll(SPECIFIC.getOrDefault(key, List.of()));
            for (String operator : OPERATORS) {
                List<Map<String, Object>> wheres = new ArrayList<>();
                switch (operator) {
                    case "$eq" -> {
                        for (Object operand : operands) {
                            wheres.add(where(key, operand));  // the scalar shorthand
                            wheres.add(where(key, op("$eq", operand)));
                        }
                    }
                    case "$ne" -> operands.forEach(o -> wheres.add(where(key, op("$ne", o))));
                    case "$in", "$nin" -> {
                        List<List<Object>> sets = new ArrayList<>();
                        sets.add(List.of());
                        sets.add(List.of(""));
                        sets.add(java.util.Arrays.asList((Object) null));
                        sets.add(List.of("no-such-value", "x"));
                        sets.add(List.of(5, "7"));
                        for (Object specific : SPECIFIC.getOrDefault(key, List.of())) {
                            sets.add(List.of(specific));
                            sets.add(List.of(specific, ""));
                        }
                        if (SPECIFIC.getOrDefault(key, List.of()).size() > 1) {
                            sets.add(SPECIFIC.get(key).subList(0, 2));
                        }
                        sets.forEach(s -> wheres.add(where(key, op(operator, s))));
                    }
                    default -> {
                        for (Object operand : operands) {
                            if (operand instanceof Number || operand instanceof String) {
                                wheres.add(where(key, op(operator, operand)));
                            }
                        }
                    }
                }
                for (Map<String, Object> w : wheres) {
                    try {
                        out.add(new Entry(key + " " + w.get(key), w, ManifestLabelStage.split(w).labels()));
                    } catch (IllegalArgumentException refused) {
                        // an operand shape the stage refuses (a null range operand, say): not a predicate to compare
                    }
                }
            }
        }
        return out;
    }

    /** Per-operator counts of predicates that admitted something and that rejected something. */
    private final Map<String, int[]> admitReject = new TreeMap<>();

    private void tally(String bucket, int admitted, int total) {
        int[] c = admitReject.computeIfAbsent(bucket, k -> new int[2]);
        if (admitted > 0) c[0]++;
        if (admitted < total) c[1]++;
    }

    @Test
    void routeBAdmitsExactlyWhatTheStageKeeps_onEveryPredicateOfTheMatrix() {
        List<Entry> matrix = matrix();
        int total = stageDocChunks(List.of()).size();  // no predicate: every live (document, chunk) pair
        int liveOccurrences = passingOccurrences(List.of()).size();  // no predicate: every live manifest row
        List<String> mismatches = new ArrayList<>();
        List<String> multiplicity = new ArrayList<>();
        int repeatedBothPass = 0;
        int repeatedOnePasses = 0;
        List<String> routeAViolations = new ArrayList<>();
        int pushedA = 0;
        int exactA = 0;
        int examined = 0;

        for (Entry e : matrix) {
            Set<String> expected = stageDocChunks(e.labels());
            List<String> actualRows = routeBRows(e.where());
            Set<String> actual = new TreeSet<>(actualRows);
            List<String> passing = passingOccurrences(e.labels());
            examined++;
            String op = e.labels().get(0).operator();
            String key = e.labels().get(0).key();
            tally(op, expected.size(), total);
            tally(key + " " + op, expected.size(), total);
            if (!expected.equals(actual)) {
                Set<String> missing = new TreeSet<>(expected);
                missing.removeAll(actual);
                Set<String> extra = new TreeSet<>(actual);
                extra.removeAll(expected);
                mismatches.add(e.name() + " [" + e.where() + "] route (b) misses " + missing + " and adds " + extra);
            }
            // The oracle itself: the pairs holding a passing occurrence are the pairs the stage keeps.
            if (!expected.equals(new TreeSet<>(passing))) {
                mismatches.add(e.name() + " [" + e.where() + "] the per-occurrence oracle " + new TreeSet<>(passing)
                    + " disagrees with the stage " + expected);
            }
            // Multiplicity: route (b) returns one row per passing occurrence, so a pair held at two positions comes back
            // once for each position that passes on its own labels.
            if (!actualRows.equals(passing)) {
                multiplicity.add(e.name() + " [" + e.where() + "] route (b) rows " + actualRows
                    + " but the passing occurrences are " + passing);
            }
            for (String repeated : REPEATED) {
                int rows = Collections.frequency(passing, repeated);
                if (rows == 2) repeatedBothPass++;
                if (rows == 1) repeatedOnePasses++;
            }
            // route (a): what it really pushes admits a superset of the stage's chunks
            ManifestPushdown.Plan planA = null;
            try {
                planA = ManifestPushdown.of(e.labels());
            } catch (IllegalArgumentException refused) {
                // the search's own planner refused the operand: nothing is pushed, the stage alone answers
            }
            if (planA != null) {
                pushedA++;
                Set<String> admittedChunks = routeAChunks(planA);
                if (!admittedChunks.containsAll(chunksOf(expected))) {
                    Set<String> lost = new TreeSet<>(chunksOf(expected));
                    lost.removeAll(admittedChunks);
                    routeAViolations.add(e.name() + " [" + e.where() + "] route (a) EXISTS drops " + lost);
                }
            }
            // route (a)'s object with the exact form: equal chunks (content_type's wider array is held to the superset rule)
            boolean touchesContentType = e.labels().stream()
                .anyMatch(p -> ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key()));
            if (!touchesContentType) {
                Set<String> chunks = routeAChunks(ManifestPushdown.ofExact(e.labels()));
                exactA++;
                if (!chunks.equals(chunksOf(expected))) {
                    routeAViolations.add(e.name() + " [" + e.where() + "] manifest_search with the exact form admits "
                        + chunks + " but the stage keeps " + chunksOf(expected));
                }
            }
        }

        System.out.println("QPT52-DIFF predicates=" + examined + " live_occurrences=" + liveOccurrences + " live_pairs=" + total
            + " route_a_pushed=" + pushedA + " route_a_exact_compared=" + exactA + " route_b_mismatches=" + mismatches.size()
            + " route_b_multiplicity_mismatches=" + multiplicity.size() + " route_a_violations=" + routeAViolations.size()
            + " repeated_pair_checks_both_pass=" + repeatedBothPass + " repeated_pair_checks_one_passes=" + repeatedOnePasses);
        admitReject.forEach((bucket, c) -> {
            if (!bucket.contains(" ")) {
                System.out.println("QPT52-DIFF operator " + bucket + " admits_something=" + c[0] + " rejects_something=" + c[1]);
            }
        });
        assertThat(mismatches).as("route (b) vs the stage: %d of %d predicates differ", mismatches.size(), examined)
            .isEmpty();
        assertThat(multiplicity).as("route (b) rows vs the passing occurrences: %d of %d predicates differ",
            multiplicity.size(), examined).isEmpty();
        assertThat(routeAViolations).as("route (a) EXISTS vs the stage").isEmpty();

        // Non-vacuity. The matrix is large, every operator admits something and rejects something, and the five keys of
        // nexus-qpt52 each have a positive and a negative operator that both admit and reject.
        assertThat(examined).as("matrix size").isGreaterThanOrEqualTo(1500);
        long liveInFixture = OCCS.stream()
            .filter(o -> DOCS.stream().anyMatch(d -> d.id().equals(o.doc()) && !d.deleted())).count();
        assertThat(liveOccurrences).as("live manifest occurrences in the fixture").isEqualTo(34).isEqualTo((int) liveInFixture);
        assertThat(total).as("live (document, chunk) pairs: fewer than the occurrences, a chunk is held twice").isEqualTo(32);
        assertThat(liveOccurrences - total).as("occurrences beyond the pairs: the repeated chunks")
            .isEqualTo(REPEATED.size());
        assertThat(repeatedBothPass).as("repeated-pair checks where both positions pass (two route (b) rows)")
            .isGreaterThanOrEqualTo(REPEAT_BOTH_FLOOR);
        assertThat(repeatedOnePasses).as("repeated-pair checks where one position passes (one route (b) row)")
            .isGreaterThanOrEqualTo(REPEAT_ONE_FLOOR);
        assertThat(pushedA).as("predicates route (a) pushes").isGreaterThanOrEqualTo(300);
        assertThat(exactA).as("exact-form manifest_search comparisons").isGreaterThanOrEqualTo(1000);
        for (String operator : OPERATORS) {
            int[] c = admitReject.get(operator);
            assertThat(c).as("operator %s examined", operator).isNotNull();
            assertThat(c[0]).as("%s: predicates that admit something", operator).isGreaterThanOrEqualTo(100);
            assertThat(c[1]).as("%s: predicates that reject something", operator).isGreaterThanOrEqualTo(100);
        }
        for (String key : NEW_KEYS) {
            for (String operator : List.of("$eq", "$ne", "$in", "$nin")) {
                int[] c = admitReject.get(key + " " + operator);
                assertThat(c).as("%s %s examined", key, operator).isNotNull();
                assertThat(c[0]).as("%s %s admits something", key, operator).isPositive();
                assertThat(c[1]).as("%s %s rejects something", key, operator).isPositive();
            }
        }
        for (String key : List.of("bib_year", "bib_citation_count", "indexed_at")) {
            for (String operator : List.of("$gte", "$gt", "$lte", "$lt")) {
                int[] c = admitReject.get(key + " " + operator);
                assertThat(c).as("%s %s examined", key, operator).isNotNull();
                assertThat(c[0]).as("%s %s admits something", key, operator).isPositive();
                assertThat(c[1]).as("%s %s rejects something", key, operator).isPositive();
            }
        }
    }

    @Test
    void theRepeatedChunksAreHeldAtTwoLivePositionsWithDifferingEffectiveLabels() {
        // The premise the multiplicity check rests on, read back from the database: each repeated pair has two live
        // manifest rows, and the three labels the stage would judge (section_title, section_type, page_number) all
        // differ between them. One of the pairs is written at one position and legacy at the other.
        Map<String, List<ManifestLabelStage.Occurrence>> owners = stage.occurrences(TENANT, chunkRows, Set.of());
        int mixed = 0;
        for (String repeated : REPEATED) {
            String doc = repeated.substring(0, repeated.indexOf('@'));
            String chash = repeated.substring(repeated.indexOf('@') + 1);
            Map<String, Object> chunkRow = chunkRows.stream().filter(r -> chash.equals(r.get("id"))).findFirst().orElseThrow();
            List<ManifestLabelStage.Occurrence> mine = owners.get(ManifestLabelStage.ownerKey(COLL, chash)).stream()
                .filter(o -> o.docId().equals(doc)).toList();
            assertThat(mine).as(repeated).hasSize(2);
            if (mine.get(0).labelsWritten() != mine.get(1).labelsWritten()) mixed++;
            Map<String, Object> first = ManifestLabelStage.rowLabels(chunkRow, mine.get(0), Set.of());
            Map<String, Object> second = ManifestLabelStage.rowLabels(chunkRow, mine.get(1), Set.of());
            for (String label : List.of("section_title", "section_type", "page_number")) {
                assertThat(java.util.Objects.equals(first.get(label), second.get(label)))
                    .as("%s: %s is the same at both positions (%s, %s)", repeated, label, first.get(label), second.get(label))
                    .isFalse();
            }
        }
        assertThat(mixed).as("repeated pairs written at one position and legacy at the other").isGreaterThanOrEqualTo(1);
    }

    // ── route (c): the get routes (nexus-u9dc5) ─────────────────────────────

    /** The chunks the stage keeps among {@code rows}, document-blind: the ids of its output rows. */
    private Set<String> stageChunkIds(List<Map<String, Object>> rows, List<ManifestLabelStage.Predicate> labels) {
        Set<String> out = new TreeSet<>();
        for (Map<String, Object> row : stage.apply(TENANT, rows, labels, Integer.MAX_VALUE)) {
            out.add((String) row.get("id"));
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static Set<String> idsOf(Map<String, Object> envelope) {
        return new TreeSet<>((List<String>) envelope.get("ids"));
    }

    private List<Map<String, Object>> allRows() {
        List<Map<String, Object>> all = new ArrayList<>(chunkRows);
        all.addAll(extraRows);
        return all;
    }

    private Set<String> liveChunkIds() {
        Set<String> ids = new TreeSet<>();
        chunkRows.forEach(r -> ids.add((String) r.get("id")));
        return ids;
    }

    @Test
    void routeCAdmitsExactlyTheChunksTheStageKeeps_orphansIncluded_onEveryPredicateOfTheMatrix() {
        List<Entry> matrix = matrix();
        List<Map<String, Object>> all = allRows();
        Set<String> live = liveChunkIds();
        Set<String> orphans = new TreeSet<>();
        extraRows.forEach(r -> orphans.add((String) r.get("id")));
        List<String> mismatches = new ArrayList<>();
        int examined = 0;
        int orphanAdmitted = 0;
        int orphanRejected = 0;
        int liveAdmitted = 0;
        int liveRejected = 0;
        int stageDiffersFromLiveOnly = 0;
        Map<String, int[]> perOperator = new TreeMap<>();

        for (Entry e : matrix) {
            Set<String> expected = stageChunkIds(all, e.labels());
            Set<String> expectedLive = new TreeSet<>(expected);
            expectedLive.retainAll(live);
            examined++;
            Set<String> physical = idsOf(vec.getAllMetadata(TENANT, COLL, e.where(), true));
            Set<String> liveRead = idsOf(vec.getAllMetadata(TENANT, COLL, e.where(), false));
            Set<String> getLive = idsOf(vec.getWhere(TENANT, COLL, e.where(), 1000, 0, false, false));
            Set<String> getPhysical = idsOf(vec.getWhere(TENANT, COLL, e.where(), 1000, 0, false, true));
            if (!expected.equals(physical)) {
                mismatches.add(e.name() + " [" + e.where() + "] get-all-metadata (include_non_live) returns " + physical
                    + " but the stage keeps " + expected);
            }
            if (!expectedLive.equals(liveRead)) {
                mismatches.add(e.name() + " [" + e.where() + "] get-all-metadata returns " + liveRead
                    + " but the stage keeps, among live chunks, " + expectedLive);
            }
            if (!expectedLive.equals(getLive)) {
                mismatches.add(e.name() + " [" + e.where() + "] get returns " + getLive + " but the stage keeps " + expectedLive);
            }
            if (!expected.equals(getPhysical)) {
                mismatches.add(e.name() + " [" + e.where() + "] get (include_non_live) returns " + getPhysical
                    + " but the stage keeps " + expected);
            }
            Set<String> keptOrphans = new TreeSet<>(expected);
            keptOrphans.retainAll(orphans);
            if (!keptOrphans.isEmpty()) orphanAdmitted++;
            if (keptOrphans.size() < orphans.size()) orphanRejected++;
            if (!expectedLive.isEmpty()) liveAdmitted++;
            if (expectedLive.size() < live.size()) liveRejected++;
            if (!expected.equals(expectedLive)) stageDiffersFromLiveOnly++;
            int[] c = perOperator.computeIfAbsent(e.labels().get(0).operator(), k -> new int[2]);
            if (!expected.isEmpty()) c[0]++;
            if (expected.size() < all.size()) c[1]++;
        }

        System.out.println("U9DC5-DIFF route (c) predicates=" + examined + " chunks=" + all.size() + " live_chunks=" + live.size()
            + " orphan_chunks=" + orphans.size() + " mismatches=" + mismatches.size()
            + " predicates_admitting_an_orphan=" + orphanAdmitted + " predicates_rejecting_an_orphan=" + orphanRejected
            + " predicates_admitting_a_live_chunk=" + liveAdmitted + " predicates_rejecting_a_live_chunk=" + liveRejected
            + " predicates_where_orphans_change_the_answer=" + stageDiffersFromLiveOnly);
        perOperator.forEach((op, c) -> System.out.println("U9DC5-DIFF route (c) operator " + op + " admits_something=" + c[0]
            + " rejects_something=" + c[1]));
        assertThat(mismatches).as("route (c) vs the stage: %d of %d predicates differ", mismatches.size(), examined).isEmpty();

        // Non-vacuity: the same matrix as route (b), the fixture holds the orphans, and the orphans are told apart.
        assertThat(examined).as("matrix size").isGreaterThanOrEqualTo(1500);
        assertThat(live).as("chunks with a live owner").hasSize(9);
        assertThat(orphans).as("orphan chunks, one of them held only by a tombstoned document").hasSize(4);
        assertThat(orphanAdmitted).as("predicates that admit an orphan").isGreaterThanOrEqualTo(300);
        assertThat(orphanRejected).as("predicates that reject an orphan").isGreaterThanOrEqualTo(300);
        assertThat(stageDiffersFromLiveOnly).as("predicates where the orphans change the answer").isGreaterThanOrEqualTo(300);
        assertThat(liveAdmitted).as("predicates that admit a live chunk").isGreaterThanOrEqualTo(300);
        assertThat(liveRejected).as("predicates that reject a live chunk").isGreaterThanOrEqualTo(300);
        for (String operator : OPERATORS) {
            int[] c = perOperator.get(operator);
            assertThat(c).as("operator %s examined", operator).isNotNull();
            assertThat(c[0]).as("%s: predicates that admit something", operator).isGreaterThanOrEqualTo(100);
            // measured: $ne rejects something in 89 predicates (a negative operator rejects little over 13 chunks)
            assertThat(c[1]).as("%s: predicates that reject something", operator).isGreaterThanOrEqualTo(50);
        }
    }

    @Test
    void routeCSeesTheOrphansOnlyInThePhysicalScan_andTheTombstonedOnlyChunkIsOne() {
        // The premise of the leg above, read back from the database: with no predicate the live read lists the chunks
        // that have a live owner and the physical scan lists those and the four that have none.
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, null, false))).isEqualTo(liveChunkIds());
        Set<String> all = new TreeSet<>();
        allRows().forEach(r -> all.add((String) r.get("id")));
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, null, true))).isEqualTo(all);
        // the tombstoned-only chunk: its manifest row is there, its document is deleted, and the stage reads it as an
        // orphan (no live occurrence), judged on the chunk row: the row says section_type body where the manifest row
        // of d11 says references
        String tomb = chash(FIRST_EXTRA_CHUNK + 3);
        assertThat(stage.occurrences(TENANT, extraRows, Set.of()).getOrDefault(ManifestLabelStage.ownerKey(COLL, tomb), List.of()))
            .as("a tombstoned owner owns nothing").isEmpty();
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, where("section_type", "body"), true))).contains(tomb);
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, where("section_type", "references"), true))).doesNotContain(tomb);
    }

    @Test
    void routeCLeavesAChunkKeyOnTheRow_aloneAndBesideAnAnsweredKey() {
        // source_key is not a key the stage answers: it stays the row comparison, on live chunks and on orphans.
        Set<String> withKey = new TreeSet<>(List.of(chash(1), chash(2), chash(FIRST_EXTRA_CHUNK), chash(FIRST_EXTRA_CHUNK + 3)));
        assertThat(ManifestLabelStage.split(where("source_key", "sk-shared")).active()).isFalse();
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, where("source_key", "sk-shared"), true))).isEqualTo(withKey);
        Set<String> liveWithKey = new TreeSet<>(withKey);
        liveWithKey.retainAll(liveChunkIds());
        assertThat(liveWithKey).hasSize(2);
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, where("source_key", "sk-shared"), false))).isEqualTo(liveWithKey);
        assertThat(idsOf(vec.getAllMetadata(TENANT, COLL, where("source_key", "no-such-key"), true))).isEmpty();
        // beside an answered key: the two are ANDed, and each is judged by its own rule
        int examined = 0;
        int admitting = 0;
        List<String> mismatches = new ArrayList<>();
        List<Map<String, Object>> keyed = new ArrayList<>();
        for (Map<String, Object> r : allRows()) {
            if (withKey.contains((String) r.get("id"))) keyed.add(r);
        }
        for (Entry e : matrix()) {
            Map<String, Object> mixed = new LinkedHashMap<>(e.where());
            mixed.put("source_key", "sk-shared");
            Set<String> expected = stageChunkIds(keyed, e.labels());
            Set<String> actual = idsOf(vec.getAllMetadata(TENANT, COLL, mixed, true));
            examined++;
            if (!expected.isEmpty()) admitting++;
            if (!expected.equals(actual)) {
                mismatches.add(e.name() + " [" + mixed + "] returns " + actual + " but the stage keeps " + expected);
            }
        }
        assertThat(mismatches).as("an answered key beside a chunk key: %d of %d predicates differ", mismatches.size(), examined)
            .isEmpty();
        assertThat(examined).isGreaterThanOrEqualTo(1500);
        assertThat(admitting).as("predicates that admit a chunk beside the chunk key").isGreaterThanOrEqualTo(100);
    }

    @Test
    void routeCRefusesAnOperatorTheStageRefuses_asTheSearchesDo() {
        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> vec.getAllMetadata(TENANT, COLL, where("section_type", op("$regex", "x")), false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("section_type");
        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> vec.getWhere(TENANT, COLL, where("tags", op("$in", "arch")), 10, 0, false, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("tags");
    }

    @Test
    void routeBLeavesNoStageAnsweredKeyInTheContainment() {
        Set<String> keys = new LinkedHashSet<>();
        int examined = 0;
        for (Entry e : matrix()) {
            var rb = PgVectorRepository.routeBWhere(e.where());
            examined++;
            if (rb.containment() != null) {
                keys.add(e.name() + " -> " + rb.containment().data());
            }
            assertThat(rb.hasManifest()).as(e.name()).isTrue();
        }
        assertThat(keys).as("answered keys left in the chunk-row containment").isEmpty();
        assertThat(examined).isGreaterThanOrEqualTo(1500);
    }
}

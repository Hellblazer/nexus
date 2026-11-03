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
 *       chunk) pairs must EQUAL the stage's, for every predicate;</li>
 *   <li>by route (a)'s {@code manifest_search_384}, which admits a chunk when any live occurrence passes: with the
 *       predicate route (a) really pushes ({@link ManifestPushdown#of}) its chunk set must be a SUPERSET of the stage's
 *       chunks, and with the exact form ({@link ManifestPushdown#ofExact}) it must EQUAL them (content_type, whose
 *       route (a) array is deliberately wider, is held to the superset rule only).</li>
 * </ol>
 *
 * <p>The fixture is built to reach every branch of the stage's {@code testValue}: written and legacy manifest rows,
 * blank and non-blank titles and authors, catalog types with and without a derived format, documents with no type, a
 * soft-deleted owner, enriched and unenriched bib columns (a year of 0, a citation count of 0), an indexed_at that is
 * NULL (so the chunk row answers) and one that is not, catalog meta present, JSON null and absent, tags as an array
 * and as a comma string, numbers stored as text, and chunk rows that disagree with the catalog.
 *
 * <p>Non-vacuity: the matrix has a floor in size, each of the eight operators has predicates that admit something and
 * predicates that reject something, and the five keys of nexus-qpt52 (bib_year, bib_authors, bib_venue,
 * bib_citation_count, indexed_at) are each admitted and rejected by a positive and by a negative operator.
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
            "2026-01-15T10:00:00Z", false));

    /** Chunk-row metadata, as stored (the last writer's, and sometimes disagreeing with the catalog). */
    private static final List<String> CHUNK_META = List.of(
        /* 00 */ "{}",
        /* 01 */ "{\"section_type\":\"references\",\"section_title\":\"Intro\",\"page_number\":3,\"source_author\":\"Eve\","
                  + "\"title\":\"Row Title\",\"content_type\":\"prose\",\"indexed_at\":\"2026-03-03T03:03:03.000000+00:00\","
                  + "\"category\":\"row-cat\",\"tags\":\"r1,r2\"}",
        /* 02 */ "{\"section_type\":null,\"page_number\":\"5\",\"content_type\":\"pdf\",\"ttl_days\":\"9\","
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
        l("d12", 0, 1), l("d12", 1, 6), l("d12", 2, 8));

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

    /** The (document, chunk) pairs route (b) returns, through the real repository path. */
    private Set<String> routeB(Map<String, Object> where) {
        Set<String> out = new TreeSet<>();
        for (Map<String, Object> r : vec.searchMetadataScoped(TENANT, "q", List.of(COLL), null, null, null, null, null,
                where, 500)) {
            out.add(r.get("id") + "@" + r.get("chash"));
        }
        return out;
    }

    /** The chunks {@code manifest_search_384} admits for a compiled plan. */
    private Set<String> routeAChunks(ManifestPushdown.Plan plan) {
        Vector q = VectorSeeds.vecXY(384, 1f, 0f);
        return tenantScope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            Set<String> out = new TreeSet<>();
            for (var r : ctx.selectFrom(MANIFEST_SEARCH_384.call(q, null, null, new String[] {COLL}, null, null,
                    plan.containment(), plan.jsonPath(), 1000, MODEL, TENANT)).fetch()) {
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
        int total = stageDocChunks(List.of()).size();  // no predicate: every live occurrence
        List<String> mismatches = new ArrayList<>();
        List<String> routeAViolations = new ArrayList<>();
        int pushedA = 0;
        int exactA = 0;
        int examined = 0;

        for (Entry e : matrix) {
            Set<String> expected = stageDocChunks(e.labels());
            Set<String> actual = routeB(e.where());
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

        System.out.println("QPT52-DIFF predicates=" + examined + " live_occurrences=" + total + " route_a_pushed=" + pushedA
            + " route_a_exact_compared=" + exactA + " route_b_mismatches=" + mismatches.size()
            + " route_a_violations=" + routeAViolations.size());
        admitReject.forEach((bucket, c) -> {
            if (!bucket.contains(" ")) {
                System.out.println("QPT52-DIFF operator " + bucket + " admits_something=" + c[0] + " rejects_something=" + c[1]);
            }
        });
        assertThat(mismatches).as("route (b) vs the stage: %d of %d predicates differ", mismatches.size(), examined)
            .isEmpty();
        assertThat(routeAViolations).as("route (a) EXISTS vs the stage").isEmpty();

        // Non-vacuity. The matrix is large, every operator admits something and rejects something, and the five keys of
        // nexus-qpt52 each have a positive and a negative operator that both admit and reject.
        assertThat(examined).as("matrix size").isGreaterThanOrEqualTo(1500);
        assertThat(total).as("live occurrences in the fixture").isEqualTo(28);
        assertThat(pushedA).as("predicates route (a) pushes").isGreaterThanOrEqualTo(300);
        assertThat(exactA).as("exact-form manifest_search comparisons").isGreaterThanOrEqualTo(1000);
        for (String operator : OPERATORS) {
            int[] c = admitReject.get(operator);
            assertThat(c).as("operator %s examined", operator).isNotNull();
            assertThat(c[0]).as("%s: predicates that admit something", operator).isGreaterThanOrEqualTo(20);
            assertThat(c[1]).as("%s: predicates that reject something", operator).isGreaterThanOrEqualTo(20);
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

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
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
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
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.41 (RDR-236 Phase 3; the route (b) half of nexus-u9dc5): the nine route (b) functions
 * ({@code search_metadata_scoped_<dim>}, {@code search_aspect_scoped_<dim>}, {@code search_graph_hop_<dim>}) answer
 * a document key or a label key of the {@code where} per OWNING DOCUMENT from the catalog, on the joined row's own
 * per-occurrence object, and not from the shared T3 chunk row.
 *
 * <p>Fixture. Two documents, A (author Alice, title "Document A") and B (author Bob). Chunks: S held by A and B,
 * P held by A only, Q held by B only; the query ranks them S, P, Q. The manifest labels (written): A holds S and P as
 * {@code body}; B holds S as {@code appendix} and Q as {@code tail}. The chunk rows are the stale or sparse rows the
 * catalog exists to replace: S's row says {@code source_author=Alice, section_type=tail} (the last writer's), P's row
 * carries neither key (the Phase 4 Step 1 lag case: a document written after the writers stopped stamping), Q's row
 * carries {@code lang=en} and no document key.
 *
 * <p>Each case below would return a different list on the retired {@code c.metadata @> p_where} predicate; the class
 * header of each test says how.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RouteBManifestPreFilterTest {

    private static final String SVC_ROLE = "svc_routeb_prefilter";
    private static final String SVC_PASS = "svc_routeb_prefilter_pass";

    private static final String TENANT = "routeb-prefilter";
    private static final String COLLECTION = "knowledge__routeb-prefilter__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String DOC_A = "rb-doc-a";
    private static final String DOC_B = "rb-doc-b";

    private static final String TEXT_S = "routebprobe shared chunk held by documents a and b";
    private static final String TEXT_P = "routebprobe chunk only document a holds";
    private static final String TEXT_Q = "routebprobe chunk only document b holds";
    private static final String QUERY = "routebprobe";

    // content_type and tags fixture (its own tenant and collection): see the cases at the end of the class.
    private static final String CT_TENANT = "routeb-ct";
    private static final String CT_COLLECTION = "knowledge__routeb-ct__minilm-l6-v2-384__v1";
    private static final String DOC_N = "rb-doc-n";
    private static final String DOC_PR = "rb-doc-p";
    private static final String DOC_M = "rb-doc-m";
    private static final String DOC_D = "rb-doc-d";
    private static final String TEXT_X = "routebprobe ct chunk held by a note and a prose file";
    private static final String TEXT_Y = "routebprobe ct chunk held by a markdown file";
    private static final String TEXT_Z = "routebprobe ct chunk held by a document with no catalog type";
    private static final String X = Chash.ofText("routeb-ct-x").toHex();
    private static final String Y = Chash.ofText("routeb-ct-y").toHex();
    private static final String Z = Chash.ofText("routeb-ct-z").toHex();

    private static final String S = Chash.ofText("routeb-prefilter-s").toHex();
    private static final String P = Chash.ofText("routeb-prefilter-p").toHex();
    private static final String Q = Chash.ofText("routeb-prefilter-q").toHex();

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
        embedder.register(TEXT_S, 1.0f, 0.0f);
        embedder.register(TEXT_P, 0.9539392f, 0.3f);
        embedder.register(TEXT_Q, 0.8f, 0.6f);
        embedder.register(TEXT_X, 1.0f, 0.0f);
        embedder.register(TEXT_Y, 0.9539392f, 0.3f);
        embedder.register(TEXT_Z, 0.8f, 0.6f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            seedDocument(seed, TENANT, COLLECTION, DOC_A, "Document A", "Alice");
            seedDocument(seed, TENANT, COLLECTION, DOC_B, "Document B", "Bob");
            seedAspect(seed, TENANT, COLLECTION, DOC_A);
            seedAspect(seed, TENANT, COLLECTION, DOC_B);
        }
        vec.upsertChunks(TENANT, COLLECTION, List.of(S, P, Q), List.of(TEXT_S, TEXT_P, TEXT_Q), List.of(
            row("source_author", "Alice", "section_type", "tail"),
            row(),
            row("lang", "en")));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, TENANT, COLLECTION, MODEL, DOC_A, 0, S, "body");
            manifest(seed, TENANT, COLLECTION, MODEL, DOC_A, 1, P, "body");
            manifest(seed, TENANT, COLLECTION, MODEL, DOC_B, 0, S, "appendix");
            manifest(seed, TENANT, COLLECTION, MODEL, DOC_B, 1, Q, "tail");
        }
        seedContentTypeFixture();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static void seedDocument(DSLContext seed, String tenant, String collection, String doc, String title,
                                     String author) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + doc + ".md")
            .set(CATALOG_DOCUMENTS.AUTHOR, author)
            .execute();
    }

    private static void seedAspect(DSLContext seed, String tenant, String collection, String doc) {
        seed.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, tenant)
            .set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, doc + ".md")
            .set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "gradient descent")
            .set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.parse("2026-03-01T00:00:00Z"))
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "rb-test-model")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "rb-test-extractor")
            .set(DOCUMENT_ASPECTS.DOC_ID, doc)
            .set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///" + doc + ".md")
            .execute();
    }

    private static void manifest(DSLContext seed, String tenant, String collection, String model, String doc,
                                 int position, String chash, String sectionType) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, sectionType)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .execute();
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** The family's route (b) search over the fixture collection. */
    private List<Map<String, Object>> run(String family, Map<String, Object> where) {
        return switch (family) {
            case "metadata" -> vec.searchMetadataScoped(TENANT, QUERY, List.of(COLLECTION), null, null, null, null,
                null, where, 10);
            case "aspect" -> vec.searchAspectScopedWithTokens(TENANT, QUERY, List.of(COLLECTION), null, null, null,
                where, 10).value();
            case "graph" -> vec.searchGraphHop(TENANT, QUERY, List.of(DOC_A, DOC_B), List.of(COLLECTION), null, 1,
                "both", where, 10);
            default -> throw new IllegalArgumentException(family);
        };
    }

    private static String chashName(String chash) {
        return chash.equals(S) ? "S" : chash.equals(P) ? "P" : chash.equals(Q) ? "Q" : chash;
    }

    /** {@code S@A}: the chunk, then the owning document the row names (the id of a route (b) row is the tumbler). */
    private static List<String> names(List<Map<String, Object>> rows) {
        return rows.stream()
            .map(r -> chashName((String) r.get("chash")) + "@" + ((String) r.get("id")).substring("rb-doc-".length()).toUpperCase())
            .sorted()
            .collect(Collectors.toList());
    }

    private static Map<String, Object> where(Object... kv) {
        return row(kv);
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    // ── the four required cases, each over all three families ────────────────

    /**
     * RDR-236 line 1735. S has two owners and the filter matches one of them: only that owner's row is returned.
     * The retired predicate read S's chunk row ({@code source_author=Alice}), so {@code Alice} returned S for BOTH
     * owners (S@B is wrong) and {@code Bob} returned nothing.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void aSharedChunkWithTwoOwners_returnsOnlyTheOwnerThatPassesTheDocumentKey(String family) {
        assertThat(names(run(family, where("source_author", "Alice")))).containsExactly("P@A", "S@A");
        assertThat(names(run(family, where("source_author", "Bob")))).containsExactly("Q@B", "S@B");
        assertThat(names(run(family, where("title", "Document B")))).containsExactly("Q@B", "S@B");
    }

    /**
     * A document key on a chunk whose row LACKS the key still matches, through the catalog (the Phase 4 Step 1 lag
     * case). No chunk row carries a {@code title}; the retired predicate returned nothing for either document.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void aDocumentKey_matchesARowWhoseChunkMetadataLacksIt_throughTheCatalog(String family) {
        assertThat(names(run(family, where("title", "Document A")))).containsExactly("P@A", "S@A");
        assertThat(names(run(family, where("title", op("$in", List.of("Document A", "Document B"))))))
            .containsExactly("P@A", "Q@B", "S@A", "S@B");
    }

    /**
     * A label key is answered from the manifest row of the owner. S's chunk row says {@code section_type=tail}; the
     * retired predicate returned S@A and S@B for {@code tail} and nothing for {@code body}.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void aLabelKey_isAnsweredFromTheManifestRowOfTheOwner(String family) {
        assertThat(names(run(family, where("section_type", "body")))).containsExactly("P@A", "S@A");
        assertThat(names(run(family, where("section_type", "appendix")))).containsExactly("S@B");
        assertThat(names(run(family, where("section_type", "tail")))).containsExactly("Q@B");
        assertThat(names(run(family, where("section_type", op("$in", List.of("appendix", "tail"))))))
            .containsExactly("Q@B", "S@B");
    }

    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void aChunkRowKey_andAManifestKey_composeOnTheSameRow(String family) {
        // lang is a chunk-row key (Q's row only) and stays a containment; section_type tail is B's label on Q.
        assertThat(names(run(family, where("lang", "en", "section_type", "tail")))).containsExactly("Q@B");
        assertThat(names(run(family, where("lang", "en", "section_type", "body")))).isEmpty();
        // The chunk-row key alone is the retired behaviour, untouched: every owner of the row that carries it.
        assertThat(names(run(family, where("lang", "en")))).containsExactly("Q@B");
    }

    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void noWhere_returnsEveryOwnerRow(String family) {
        assertThat(names(run(family, null))).containsExactly("P@A", "Q@B", "S@A", "S@B");
    }

    /**
     * PINS RETIRED BEHAVIOUR, ON PURPOSE (nexus-qpt52). A negative operator is not pushable, so on these routes it
     * stays in the chunk-row containment, where an operator map is never contained: nothing matches. That is what
     * these routes did before nexus-6pfgb.41 and it is NOT the design of record (RDR-236 wants {@code !=} and the
     * keys with no in-search form answered per owning document). nexus-qpt52 answers them and inverts this test:
     * {@code section_type != body} then returns the rows whose owner's label is not body. Until then the case guards
     * only that the key is neither dropped (all four rows) nor widened.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void aKeyTheSqlDoesNotCompile_staysARowContainment_neitherDroppedNorWidened(String family) {
        // $ne on a label key is not pushable (ManifestPushdown compiles positive operators only). Route (b) has no
        // stage after the search, so the key stays in the containment as before: an operator map is never contained
        // in a chunk row, so nothing matches. It must not be silently dropped (which would return all four rows).
        assertThat(names(run(family, where("section_type", op("$ne", "body"))))).isEmpty();
    }

    // ── content_type and tags: the stage's exact rule, on a route with no stage ────────────────────────────────────────

    /**
     * Four documents over three chunks, in a tenant of their own. N is a note (catalog type {@code knowledge}, no file
     * extension), P a prose file ({@code p.txt}), M a markdown file registered as prose ({@code m.md}), D a document
     * with NO catalog type. X is held by N and P and its chunk row says {@code content_type=prose} and {@code
     * tags=decision} (the last writer's); Y is held by M, Z by D, whose row says {@code prose}. The document meta: N
     * tags {@code [arch, decision]}, P none (so the row's tags answer), M {@code []}.
     */
    private void seedContentTypeFixture() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, CT_TENANT, CT_COLLECTION);
            seedTypedDocument(seed, DOC_N, "Note N", "knowledge", null, "{\"tags\":[\"arch\",\"decision\"]}");
            seedTypedDocument(seed, DOC_PR, "Prose P", "prose", "p.txt", null);
            seedTypedDocument(seed, DOC_M, "Markdown M", "prose", "m.md", "{\"tags\":[]}");
            seedTypedDocument(seed, DOC_D, "Untyped D", null, null, null);
            for (String doc : List.of(DOC_N, DOC_PR, DOC_M, DOC_D)) {
                seedAspect(seed, CT_TENANT, CT_COLLECTION, doc);
            }
        }
        vec.upsertChunks(CT_TENANT, CT_COLLECTION, List.of(X, Y, Z), List.of(TEXT_X, TEXT_Y, TEXT_Z), List.of(
            row("content_type", "prose", "tags", "decision"),
            row(),
            row("content_type", "prose")));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, CT_TENANT, CT_COLLECTION, MODEL, DOC_N, 0, X, "body");
            manifest(seed, CT_TENANT, CT_COLLECTION, MODEL, DOC_PR, 0, X, "body");
            manifest(seed, CT_TENANT, CT_COLLECTION, MODEL, DOC_M, 0, Y, "body");
            manifest(seed, CT_TENANT, CT_COLLECTION, MODEL, DOC_D, 0, Z, "body");
        }
    }

    private static void seedTypedDocument(DSLContext seed, String doc, String title, String contentType,
                                          String filePath, String metadataJson) {
        var insert = seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, CT_TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CT_COLLECTION)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + doc + ".md");
        if (contentType != null) insert = insert.set(CATALOG_DOCUMENTS.CONTENT_TYPE, contentType);
        if (filePath != null) insert = insert.set(CATALOG_DOCUMENTS.FILE_PATH, filePath);
        if (metadataJson != null) insert = insert.set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(metadataJson));
        insert.execute();
    }

    private List<Map<String, Object>> runCt(String family, Map<String, Object> where) {
        return switch (family) {
            case "metadata" -> vec.searchMetadataScoped(CT_TENANT, QUERY, List.of(CT_COLLECTION), null, null, null,
                null, null, where, 10);
            case "aspect" -> vec.searchAspectScopedWithTokens(CT_TENANT, QUERY, List.of(CT_COLLECTION), null, null,
                null, where, 10).value();
            case "graph" -> vec.searchGraphHop(CT_TENANT, QUERY, List.of(DOC_N, DOC_PR, DOC_M, DOC_D),
                List.of(CT_COLLECTION), null, 1, "both", where, 10);
            default -> throw new IllegalArgumentException(family);
        };
    }

    /** {@code X@N}: the chunk, then the owning document (the id of a route (b) row is the tumbler). */
    private static List<String> ctNames(List<Map<String, Object>> rows) {
        return rows.stream()
            .map(r -> {
                String c = (String) r.get("chash");
                String chunk = c.equals(X) ? "X" : c.equals(Y) ? "Y" : c.equals(Z) ? "Z" : c;
                return chunk + "@" + ((String) r.get("id")).substring("rb-doc-".length()).toUpperCase();
            })
            .sorted()
            .collect(Collectors.toList());
    }

    /**
     * nexus-6pfgb.41 review S2. Route (a)'s stage judges {@code content_type} exactly after the search; route (b) has no
     * stage, so the SQL must. X is held by a note (catalog type {@code knowledge}) and by a prose file, and its chunk
     * row says {@code prose}. {@code content_type=prose} returns the prose file's row, the markdown file registered as
     * prose and the document with no catalog type whose chunk row says prose, and NOT the note: its catalog type is
     * not blank, so the row's value is not consulted. The superset the pushdown was built around (catalog type, format
     * and, for a document with no format, the row's value) returned X@N here.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void contentTypeProse_doesNotReturnTheNoteThatSharesAChunkWithAProseFile(String family) {
        assertThat(ctNames(runCt(family, where("content_type", "prose"))))
            .containsExactly("X@P", "Y@M", "Z@D");
        assertThat(ctNames(runCt(family, where("content_type", "knowledge")))).containsExactly("X@N");
        assertThat(ctNames(runCt(family, where("content_type", op("$in", List.of("prose", "knowledge"))))))
            .containsExactly("X@N", "X@P", "Y@M", "Z@D");
    }

    /**
     * {@code pdf} and {@code markdown} are formats: the derived one when the catalog derives one (M's {@code m.md}), the
     * row's value only when it derives none. The note and the untyped document derive none, and their chunk rows say
     * {@code prose}, so neither is {@code markdown} or {@code pdf}.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void contentTypeMarkdownAndPdf_readTheDerivedFormat(String family) {
        assertThat(ctNames(runCt(family, where("content_type", "markdown")))).containsExactly("Y@M");
        assertThat(ctNames(runCt(family, where("content_type", "pdf")))).isEmpty();
    }

    /**
     * {@code tags} is a set answered from the owning document's meta where the meta HAS the key, else from the chunk
     * row. X's row says {@code decision}: the retired predicate returned nothing for {@code arch} and X for both
     * owners for {@code decision}. Now N (meta {@code [arch, decision]}) answers {@code arch}; P (no meta tags) falls
     * back to the row, so {@code decision} is held by both; M's empty meta array answers for M.
     */
    @ParameterizedTest
    @ValueSource(strings = {"metadata", "aspect", "graph"})
    void tags_areASetAnsweredFromTheOwningDocument(String family) {
        assertThat(ctNames(runCt(family, where("tags", "arch")))).containsExactly("X@N");
        assertThat(ctNames(runCt(family, where("tags", "decision")))).containsExactly("X@N", "X@P");
        assertThat(ctNames(runCt(family, where("tags", op("$in", List.of("arch", "nothing")))))).containsExactly("X@N");
        assertThat(ctNames(runCt(family, where("tags", "nothing")))).isEmpty();
    }

    // ── the other dims, at the function ──────────────────────────────────────

    /**
     * The 768 and 1024 bodies carry the same predicate. A direct call of each function at each dim over a two-owner
     * chunk: the manifest containment {@code {"section_type":"body"}} returns the owner whose manifest row says body.
     */
    @Test
    void theOtherDimBodies_answerTheSamePredicate() throws Exception {
        for (int dim : new int[] {768, 1024}) {
            String model = dim == 768 ? "bge-base-en-v15-768" : "voyage-context-3";
            String collection = "knowledge__routeb-dim" + dim + "__" + model + "__v1";
            String tenant = "routeb-dim" + dim;
            String chash = Chash.ofText("routeb-dim-" + dim).toHex();
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.insertCollection(seed, tenant, collection, model);
                seedDocument(seed, tenant, collection, DOC_A, "Document A", "Alice");
                seedDocument(seed, tenant, collection, DOC_B, "Document B", "Bob");
                seedAspect(seed, tenant, collection, DOC_A);
                seedAspect(seed, tenant, collection, DOC_B);
                var insert = seed.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, tenant)
                    .set(CHUNKS.COLLECTION, collection)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash))
                    .set(CHUNKS.EMBEDDING_MODEL, model)
                    .set(CHUNKS.CHUNK_TEXT, "dim chunk")
                    .set(CHUNKS.METADATA, JSONB.valueOf("{\"section_type\":\"tail\"}"));
                if (dim == 768) {
                    insert.set(CHUNKS.EMBEDDING_768, VectorSeeds.vecXY(768, 1f, 0f)).execute();
                } else {
                    insert.set(CHUNKS.EMBEDDING_1024, VectorSeeds.vecXY(1024, 1f, 0f)).execute();
                }
                manifest(seed, tenant, collection, model, DOC_A, 0, chash, "body");
                manifest(seed, tenant, collection, model, DOC_B, 0, chash, "appendix");

                JSONB body = JSONB.valueOf("{\"section_type\":\"body\"}");
                var q = VectorSeeds.vecXY(dim, 1f, 0f);
                var colls = new String[] {collection};
                var seeds = new String[] {DOC_A, DOC_B};
                org.jooq.Table<?> meta = dim == 768
                    ? SEARCH_METADATA_SCOPED_768.call(q, colls, null, null, null, null, null, null, body, null, 10, model, tenant)
                    : SEARCH_METADATA_SCOPED_1024.call(q, colls, null, null, null, null, null, null, body, null, 10, model, tenant);
                org.jooq.Table<?> aspect = dim == 768
                    ? SEARCH_ASPECT_SCOPED_768.call(q, colls, null, null, null, null, body, null, 10, model, tenant)
                    : SEARCH_ASPECT_SCOPED_1024.call(q, colls, null, null, null, null, body, null, 10, model, tenant);
                org.jooq.Table<?> graph = dim == 768
                    ? SEARCH_GRAPH_HOP_768.call(q, seeds, colls, null, 1, "both", null, body, null, 10, model, tenant)
                    : SEARCH_GRAPH_HOP_1024.call(q, seeds, colls, null, 1, "both", null, body, null, 10, model, tenant);
                for (var fn : List.of(meta, aspect, graph)) {
                    var ids = seed.select(fn.field("id", String.class)).from(fn).fetch(fn.field("id", String.class));
                    assertThat(ids).as("dim %d %s", dim, fn.getName()).containsExactly(DOC_A);
                }
                // the same call with the predicate's jsonpath form, on the label the other owner holds
                var path = "exists($.\"section_type\" ? (@ == \"appendix\"))";
                var metaPath = dim == 768
                    ? SEARCH_METADATA_SCOPED_768.call(q, colls, null, null, null, null, null, null, null, path, 10, model, tenant)
                    : SEARCH_METADATA_SCOPED_1024.call(q, colls, null, null, null, null, null, null, null, path, 10, model, tenant);
                assertThat(seed.select(metaPath.field("id", String.class)).from(metaPath).fetch(metaPath.field("id", String.class)))
                    .as("dim %d jsonpath form", dim).containsExactly(DOC_B);
            }
        }
    }
}

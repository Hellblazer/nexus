// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
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
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 6 Step 1 (bead nexus-0khi2): the engine-internal per-document MOVE into the model
 * sibling, {@link CombinedWriteService#moveDocsCombined}, on a real Postgres (Testcontainers, the product
 * schema, the service role, so RLS applies to every engine read and write).
 *
 * <p>Embedders: two seeded-random fakes with the real tokens and widths, {@code bge-base-en-v15-768} for
 * the source and {@code minilm-l6-v2-384} for the target, each behind its own {@link EmbedderRouter}
 * (a router holds one local embedder). The source is seeded through the source router's combined write,
 * the move runs through the target router's, so the target embedder's call count IS the number of chunks
 * the move embedded. No real model is loaded; the spike (210-research-18) measured the real ones.
 *
 * <p>Every read of a table goes through typed jOOQ over a superuser connection; no raw SQL.
 *
 * <p>Run: {@code -Dtest=DocumentMoveTest,CrossModelDispositionParityTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DocumentMoveTest {

    private static final String SVC_ROLE = "svc_doc_move_test";
    private static final String SVC_PASS = "svc_doc_move_test_pass";
    private static final String TENANT = "doc-move-tenant";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";
    private static final String MOVE_PRECONDITION_FAILED = "move_precondition_failed";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    Fake srcEmbed;
    Fake tgtEmbed;
    CombinedWriteService srcSvc;
    CombinedWriteService moveSvc;
    PgVectorRepository srcSearch;
    PgVectorRepository tgtSearch;
    final AtomicInteger seq = new AtomicInteger();
    /** doc id -> the chunk texts it was last written with (the test's memory of "what the client wrote"). */
    final Map<String, List<String>> docTexts = new HashMap<>();

    record Fx(String tag, String src, String tgt) {}

    record Plan(List<Map<String, Object>> chunks, List<Map<String, Object>> docs,
                Map<String, List<String>> expected) {}

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
        cfg.setMaximumPoolSize(8);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
        srcEmbed = new Fake(BGE, 768);
        tgtEmbed = new Fake(MINI, 384);
        var srcRouter = new EmbedderRouter(srcEmbed, "document");
        var tgtRouter = new EmbedderRouter(tgtEmbed, "document");
        srcSvc = new CombinedWriteService(scope, catalog, srcRouter);
        moveSvc = new CombinedWriteService(scope, catalog, tgtRouter);
        srcSearch = new PgVectorRepository(scope, srcRouter, srcRouter);
        tgtSearch = new PgVectorRepository(scope, tgtRouter, tgtRouter);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────────

    /** Deterministic unit vectors seeded by the text, counting every text embedded. */
    static final class Fake implements Embedder {
        final String token;
        final int dim;
        final AtomicInteger calls = new AtomicInteger();

        Fake(String token, int dim) {
            this.token = token;
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.addAndGet(texts.size());
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                Random r = new Random(t.hashCode());
                float[] v = new float[dim];
                double norm = 0;
                for (int i = 0; i < dim; i++) {
                    v[i] = (float) r.nextGaussian();
                    norm += v[i] * v[i];
                }
                float n = (float) Math.sqrt(norm);
                for (int i = 0; i < dim; i++) v[i] /= n;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return token;
        }
    }

    <T> T su(Function<DSLContext, T> f) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return f.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    void register(String collection) {
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            return null;
        });
    }

    /** A fresh source/target sibling pair: same content type and owner, bge-768 and minilm-384. */
    Fx pair(String tag) {
        String t = tag + seq.incrementAndGet();
        String owner = "dm" + t;
        Fx fx = new Fx(t, "knowledge__" + owner + "__" + BGE + "__v1", "knowledge__" + owner + "__" + MINI + "__v1");
        register(fx.src());
        register(fx.tgt());
        return fx;
    }

    static String ch(String text) {
        return Chash.ofText(text).toHex();
    }

    static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }

    /** Registers the document in the source and writes {@code texts} as its chunks, one manifest row each. */
    String seedDoc(Fx fx, String name, List<String> texts) {
        String docId = "dm." + fx.tag() + "." + name;
        catalog.upsertDocument(TENANT, Map.of("tumbler", docId, "title", "title-" + docId, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", fx.src(), "chunk_count", 0));
        clientWrite(fx.src(), docId, texts);
        return docId;
    }

    /** A client manifest write of {@code texts} into {@code collection} through the combined write. */
    void clientWrite(String collection, String docId, List<String> texts) {
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int k = 0; k < texts.size(); k++) {
            chunks.add(Map.of("chash", ch(texts.get(k)), "text", texts.get(k), "metadata", Map.of()));
            rows.add(Map.of("position", k, "chash", ch(texts.get(k)), "chunk_index", k));
        }
        var r = srcSvc.writeManyCombined(TENANT, collection, chunks,
            List.of(Map.of("doc_id", docId, "rows", rows)), null, false, false);
        assertThat((List<?>) r.response().get("failed_doc_ids")).as("client write of " + docId).isEmpty();
        docTexts.put(docId, texts);
    }

    List<String> seedDocs(Fx fx, int n, int perDoc) {
        List<String> ids = new ArrayList<>();
        for (int d = 0; d < n; d++) {
            List<String> texts = new ArrayList<>();
            for (int k = 0; k < perDoc; k++) texts.add("doc " + fx.tag() + " d" + d + " chunk " + k + " lorem ipsum");
            ids.add(seedDoc(fx, "d" + d, texts));
        }
        return ids;
    }

    // ── the job's read side and the move call ───────────────────────────────────

    /** What the job reads from the source for these documents: manifest rows in order, chunk text. */
    Plan readPlan(Fx fx, List<String> docIds) {
        Map<String, List<String>> expected = new LinkedHashMap<>();
        List<Map<String, Object>> docs = new ArrayList<>();
        Set<String> chashes = new LinkedHashSet<>();
        for (String docId : docIds) {
            List<String> order = new ArrayList<>();
            List<Map<String, Object>> rows = new ArrayList<>();
            for (Map<String, Object> m : catalog.getManifest(TENANT, docId)) {
                String chash = (String) m.get("chash");
                int position = ((Number) m.get("position")).intValue();
                order.add(chash);
                rows.add(Map.of("position", position, "chash", chash, "chunk_index", position));
            }
            expected.put(docId, order);
            chashes.addAll(order);
            docs.add(Map.of("doc_id", docId, "rows", rows));
        }
        List<byte[]> keys = chashes.stream().map(h -> Chash.fromHex(h).toBytes()).toList();
        List<Map<String, Object>> chunks = new ArrayList<>();
        if (!keys.isEmpty()) {
            su(ctx -> ctx.select(CHUNKS.CHASH, CHUNKS.CHUNK_TEXT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(fx.src())).and(CHUNKS.CHASH.in(keys))
                .fetch()).forEach(r -> chunks.add(Map.of("chash", hex(r.value1()), "text", r.value2(), "metadata", Map.of())));
        }
        return new Plan(chunks, docs, expected);
    }

    Map<String, Object> execute(Fx fx, Plan p) {
        return moveSvc.moveDocsCombined(TENANT, fx.src(), fx.tgt(), p.chunks(), p.docs(), p.expected()).response();
    }

    Map<String, Object> move(Fx fx, List<String> docIds) {
        return execute(fx, readPlan(fx, docIds));
    }

    // ── reads ───────────────────────────────────────────────────────────────────

    String physical(String docId) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION));
    }

    /** The collections the document's manifest rows name (tombstone-blind, straight from the table). */
    Set<String> manifestCollections(String docId) {
        return new LinkedHashSet<>(su(ctx -> ctx.selectDistinct(CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION)));
    }

    int manifestRows(String docId) {
        return su(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT),
            CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)));
    }

    int chunkCount(String collection) {
        return su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.COLLECTION.eq(collection)));
    }

    boolean chunkIn(String collection, String chashHex) {
        return su(ctx -> ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT), CHUNKS.COLLECTION.eq(collection),
            CHUNKS.CHASH.eq(Chash.fromHex(chashHex).toBytes())));
    }

    @SuppressWarnings("unchecked")
    static List<Map<String, Object>> failed(Map<String, Object> response) {
        return (List<Map<String, Object>>) response.get("failed");
    }

    @SuppressWarnings("unchecked")
    static List<String> failedIds(Map<String, Object> response) {
        return (List<String>) response.get("failed_doc_ids");
    }

    @SuppressWarnings("unchecked")
    static Map<String, Integer> satellites(Map<String, Object> response) {
        return (Map<String, Integer>) ((Map<String, Object>) response.get("move")).get("satellites");
    }

    // ── satellites ──────────────────────────────────────────────────────────────

    void seedAspect(String collection, String docId, String path, String marker) {
        su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
            .values(TENANT, collection, path, OffsetDateTime.now(), marker, "docling", docId, "file:///" + path)
            .execute());
    }

    void seedQueue(String collection, String docId, String path, String status) {
        su(ctx -> ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
            .values(TENANT, collection, path, status, OffsetDateTime.now(), docId)
            .execute());
    }

    void seedHighlight(String collection, String docId) {
        su(ctx -> ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                DOCUMENT_HIGHLIGHTS.INGESTED_AT)
            .values(TENANT, docId, collection, "file:///hl/" + docId, "highlight", OffsetDateTime.now())
            .execute());
    }

    /** {@code collection|source_path|marker} per aspect row of the document, sorted. */
    List<String> aspectRows(String docId) {
        return su(ctx -> ctx.select(DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.MODEL_VERSION)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
            .orderBy(DOCUMENT_ASPECTS.SOURCE_PATH).fetch(r -> r.value1() + "|" + r.value2() + "|" + r.value3()));
    }

    /** {@code collection|source_path|status} per queue row of the document, sorted. */
    List<String> queueRows(String docId) {
        return su(ctx -> ctx.select(ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                ASPECT_EXTRACTION_QUEUE.STATUS).from(ASPECT_EXTRACTION_QUEUE)
            .where(ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(TENANT)).and(ASPECT_EXTRACTION_QUEUE.DOC_ID.eq(docId))
            .orderBy(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH).fetch(r -> r.value1() + "|" + r.value2() + "|" + r.value3()));
    }

    List<String> highlightCollections(String docId) {
        return su(ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.COLLECTION).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
            .fetch(DOCUMENT_HIGHLIGHTS.COLLECTION));
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: a document moves whole
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void wholeDocumentMove_chunksManifestPhysicalCollectionAndSatellites() {
        Fx fx = pair("whole");
        List<String> docs = seedDocs(fx, 2, 3);
        String moved = docs.get(0);
        String stays = docs.get(1);
        seedAspect(fx.src(), moved, "p/moved.md", "v1");
        seedQueue(fx.src(), moved, "p/moved.md", "pending");
        seedHighlight(fx.src(), moved);
        seedAspect(fx.src(), stays, "p/stays.md", "v1");
        seedQueue(fx.src(), stays, "p/stays.md", "pending");
        seedHighlight(fx.src(), stays);
        List<String> chashes = docTexts.get(moved).stream().map(DocumentMoveTest::ch).toList();
        int embedBefore = tgtEmbed.calls.get();
        int sourceChunksBefore = chunkCount(fx.src());

        Map<String, Object> r = move(fx, List.of(moved));

        assertThat(failedIds(r)).isEmpty();
        assertThat(r.get("docs")).isEqualTo(1);
        // chunks: the document's three, embedded with the target's model, written to the target
        assertThat(tgtEmbed.calls.get() - embedBefore).isEqualTo(3);
        assertThat(chunkCount(fx.tgt())).isEqualTo(3);
        for (String h : chashes) assertThat(chunkIn(fx.tgt(), h)).isTrue();
        // manifest: same chashes in the same order, every row naming the target; physical_collection too
        assertThat(manifestCollections(moved)).containsExactly(fx.tgt());
        assertThat(catalog.getManifest(TENANT, moved).stream().map(m -> (String) m.get("chash")).toList())
            .containsExactlyElementsOf(chashes);
        assertThat(physical(moved)).isEqualTo(fx.tgt());
        // satellites follow the document
        assertThat(aspectRows(moved)).containsExactly(fx.tgt() + "|p/moved.md|v1");
        assertThat(queueRows(moved)).containsExactly(fx.tgt() + "|p/moved.md|pending");
        assertThat(highlightCollections(moved)).containsExactly(fx.tgt());
        assertThat(satellites(r)).containsEntry("document_aspects_moved", 1).containsEntry("document_aspects_replaced", 0)
            .containsEntry("aspect_extraction_queue_moved", 1).containsEntry("aspect_extraction_queue_replaced", 0)
            .containsEntry("document_highlights_moved", 1);
        // the source's chunks stay (LEAVE) until the job deletes the source
        assertThat(chunkCount(fx.src())).isEqualTo(sourceChunksBefore);
        // the other document is untouched in every table
        assertThat(manifestCollections(stays)).containsExactly(fx.src());
        assertThat(physical(stays)).isEqualTo(fx.src());
        assertThat(aspectRows(stays)).containsExactly(fx.src() + "|p/stays.md|v1");
        assertThat(queueRows(stays)).containsExactly(fx.src() + "|p/stays.md|pending");
        assertThat(highlightCollections(stays)).containsExactly(fx.src());
    }

    @Test
    void noDocLessSatelliteRowCanExist_theRdrsPathArmMatchesNothing() {
        // RDR-210 named a second arm for aspects and queue: an empty doc_id with a source_path equal to
        // the document's path. doc_id is NOT NULL with a composite foreign key to catalog_documents, so
        // such a row cannot be written, and the move matches by doc_id alone. If this ever stops failing,
        // the move needs the path arm (and a test that seeds it).
        Fx fx = pair("nodocless");
        assertThatThrownBy(() -> seedAspect(fx.src(), "", "p/orphan.md", "v1"))
            .hasMessageContaining("violates foreign key constraint");
        assertThatThrownBy(() -> seedQueue(fx.src(), "", "p/orphan.md", "pending"))
            .hasMessageContaining("violates foreign key constraint");
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: the target row with the same unique key stands
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void sameUniqueKeyTargetRowStands_sourceRowIsDeleted() {
        Fx fx = pair("stands");
        String doc = seedDocs(fx, 1, 2).get(0);
        // the unique key of aspects and queue is (tenant, collection, source_path)
        seedAspect(fx.tgt(), doc, "p/a.md", "target-wins");
        seedAspect(fx.src(), doc, "p/a.md", "source-loses");
        seedAspect(fx.src(), doc, "p/b.md", "source-moves");
        seedQueue(fx.tgt(), doc, "p/a.md", "failed");
        seedQueue(fx.src(), doc, "p/a.md", "pending");
        seedQueue(fx.src(), doc, "p/b.md", "pending");

        Map<String, Object> r = move(fx, List.of(doc));

        assertThat(failedIds(r)).isEmpty();
        assertThat(aspectRows(doc)).containsExactly(fx.tgt() + "|p/a.md|target-wins", fx.tgt() + "|p/b.md|source-moves");
        assertThat(queueRows(doc)).containsExactly(fx.tgt() + "|p/a.md|failed", fx.tgt() + "|p/b.md|pending");
        assertThat(satellites(r)).containsEntry("document_aspects_moved", 1).containsEntry("document_aspects_replaced", 1)
            .containsEntry("aspect_extraction_queue_moved", 1).containsEntry("aspect_extraction_queue_replaced", 1);
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: precondition and typed refusals
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void documentChangedAfterTheRead_isSkippedWithMovePreconditionFailed_andTheNextPassMovesIt() {
        Fx fx = pair("changed");
        List<String> docs = seedDocs(fx, 3, 3);
        Plan plan = readPlan(fx, docs);
        // a client re-indexes docs[0] in the source between the job's read and its move
        List<String> rewritten = new ArrayList<>(docTexts.get(docs.get(0)));
        rewritten.add("a chunk the client added after the job read the document");
        clientWrite(fx.src(), docs.get(0), rewritten);

        Map<String, Object> r = execute(fx, plan);

        assertThat(failedIds(r)).containsExactly(docs.get(0));
        assertThat(failed(r)).hasSize(1);
        assertThat(failed(r).get(0).get("reason")).as("typed reason, never 'internal error'")
            .isEqualTo(MOVE_PRECONDITION_FAILED);
        assertThat(r.get("docs")).isEqualTo(2);
        // the skipped document is exactly as the client left it, and nothing of it reached the target
        assertThat(physical(docs.get(0))).isEqualTo(fx.src());
        assertThat(manifestCollections(docs.get(0))).containsExactly(fx.src());
        assertThat(manifestRows(docs.get(0))).isEqualTo(4);
        for (String text : docTexts.get(docs.get(0))) assertThat(chunkIn(fx.tgt(), ch(text))).isFalse();
        // the other two moved
        for (String d : docs.subList(1, 3)) {
            assertThat(physical(d)).isEqualTo(fx.tgt());
            assertThat(manifestCollections(d)).containsExactly(fx.tgt());
        }
        // the next pass reads the document where the client put it and moves it
        Map<String, Object> pass2 = move(fx, List.of(docs.get(0)));
        assertThat(failedIds(pass2)).isEmpty();
        assertThat(physical(docs.get(0))).isEqualTo(fx.tgt());
        assertThat(manifestCollections(docs.get(0))).containsExactly(fx.tgt());
        assertThat(manifestRows(docs.get(0))).isEqualTo(4);
    }

    @Test
    void stalePlanForADocumentAlreadyMoved_isSkippedWithMovePreconditionFailed() {
        Fx fx = pair("stale");
        String doc = seedDocs(fx, 1, 2).get(0);
        Plan plan = readPlan(fx, List.of(doc));
        assertThat(failedIds(execute(fx, plan))).isEmpty();

        Map<String, Object> again = execute(fx, plan);

        assertThat(failedIds(again)).containsExactly(doc);
        assertThat(failed(again).get(0).get("reason")).isEqualTo(MOVE_PRECONDITION_FAILED);
        assertThat(physical(doc)).isEqualTo(fx.tgt());
        assertThat(manifestCollections(doc)).containsExactly(fx.tgt());
    }

    @Test
    void tombstonedDocument_isRefusedTyped_andLeftInTheSource() {
        Fx fx = pair("tomb");
        List<String> docs = seedDocs(fx, 2, 2);
        Plan plan = readPlan(fx, docs);
        assertThat(catalog.deleteDocument(TENANT, docs.get(0))).isEqualTo(1);

        Map<String, Object> r = execute(fx, plan);

        assertThat(failedIds(r)).containsExactly(docs.get(0));
        String reason = (String) failed(r).get(0).get("reason");
        assertThat(reason).contains("tombstoned").doesNotContain("internal error");
        assertThat(r.get("docs")).isEqualTo(1);
        assertThat(physical(docs.get(0))).isEqualTo(fx.src());
        assertThat(manifestCollections(docs.get(0))).containsExactly(fx.src());
        assertThat(manifestRows(docs.get(0))).isEqualTo(2);
        assertThat(physical(docs.get(1))).isEqualTo(fx.tgt());
    }

    @Test
    void moveRefusesCollectionsThatAreNotModelSiblings() {
        Fx fx = pair("sib");
        Fx other = pair("sibother");
        // same model as the source, different owner: not the model sibling
        assertThatThrownBy(() -> moveSvc.moveDocsCombined(TENANT, fx.src(), other.src(), List.of(), List.of(), Map.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model siblings");
        // a different owner under the target's model
        assertThatThrownBy(() -> moveSvc.moveDocsCombined(TENANT, fx.src(), other.tgt(), List.of(), List.of(), Map.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model siblings");
        // the collection itself
        assertThatThrownBy(() -> moveSvc.moveDocsCombined(TENANT, fx.src(), fx.src(), List.of(), List.of(), Map.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model siblings");
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: a shared chunk is embedded once
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void chunkSharedByTwoDocuments_isEmbeddedOnce_inOneCall() {
        Fx fx = pair("shared");
        String shared = "the chunk both documents carry " + fx.tag();
        String a = seedDoc(fx, "a", List.of(shared, "only in a " + fx.tag()));
        String b = seedDoc(fx, "b", List.of(shared, "only in b " + fx.tag()));
        int before = tgtEmbed.calls.get();

        Map<String, Object> r = move(fx, List.of(a, b));

        assertThat(failedIds(r)).isEmpty();
        assertThat(tgtEmbed.calls.get() - before).as("three distinct chunks, the shared one once").isEqualTo(3);
        assertThat(r.get("embed_embedded")).isEqualTo(3);
        assertThat(chunkCount(fx.tgt())).isEqualTo(3);
        assertThat(manifestCollections(a)).containsExactly(fx.tgt());
        assertThat(manifestCollections(b)).containsExactly(fx.tgt());
    }

    @Test
    void chunkSharedByTwoDocuments_isEmbeddedOnce_acrossCalls_andTheSourceKeepsItForTheDocumentLeft() {
        Fx fx = pair("sharedtwo");
        String shared = "the chunk two documents carry across passes " + fx.tag();
        String c = seedDoc(fx, "c", List.of(shared, "only in c " + fx.tag()));
        String d = seedDoc(fx, "d", List.of(shared, "only in d " + fx.tag()));
        int before = tgtEmbed.calls.get();

        assertThat(failedIds(move(fx, List.of(c)))).isEmpty();
        assertThat(tgtEmbed.calls.get() - before).isEqualTo(2);
        // d still reads the shared chunk in the source
        assertThat(manifestCollections(d)).containsExactly(fx.src());
        assertThat(chunkIn(fx.src(), ch(shared))).isTrue();

        Map<String, Object> second = move(fx, List.of(d));

        assertThat(failedIds(second)).isEmpty();
        assertThat(tgtEmbed.calls.get() - before).as("the shared chunk is not embedded again").isEqualTo(3);
        assertThat(second.get("embed_skipped")).isEqualTo(1);
        assertThat(second.get("embed_embedded")).isEqualTo(1);
        assertThat(chunkCount(fx.tgt())).isEqualTo(3);
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: a client write of a moved document back into the source
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void clientWriteBackIntoTheSource_barManifestWriteLeavesPhysicalCollectionOnTheTarget_nextPassMovesAgain() {
        // RDR-210 :927-930 inferred, not read: whether a client write into the source also sets
        // physical_collection back. Read and measured here: the manifest write (writeManyCombined, the
        // path a client takes for a document it already registered) does NOT; it replaces the manifest
        // rows and folds chunk_count and nothing else. So the two disagree until something registers
        // the document again (next test) or the job moves it.
        Fx fx = pair("writeback");
        String doc = seedDocs(fx, 1, 3).get(0);
        assertThat(failedIds(move(fx, List.of(doc)))).isEmpty();
        assertThat(physical(doc)).isEqualTo(fx.tgt());

        clientWrite(fx.src(), doc, docTexts.get(doc));

        assertThat(manifestCollections(doc)).as("the manifest moved back").containsExactly(fx.src());
        assertThat(physical(doc)).as("MEASURED: the bare manifest write leaves physical_collection on the target")
            .isEqualTo(fx.tgt());

        Map<String, Object> next = move(fx, List.of(doc));

        assertThat(failedIds(next)).isEmpty();
        assertThat(manifestCollections(doc)).containsExactly(fx.tgt());
        assertThat(physical(doc)).isEqualTo(fx.tgt());
    }

    @Test
    void clientWriteBackIntoTheSource_withTheRegistrationUpsert_agrees_andNextPassMovesAgain() {
        // A client that registers the document (upsertDocument, the /register route) before it writes
        // the manifest does set physical_collection: the upsert's conflict arm takes the excluded value.
        Fx fx = pair("writebackreg");
        String doc = seedDocs(fx, 1, 3).get(0);
        assertThat(failedIds(move(fx, List.of(doc)))).isEmpty();

        catalog.upsertDocument(TENANT, Map.of("tumbler", doc, "title", "title-" + doc, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", fx.src(), "chunk_count", 0));
        clientWrite(fx.src(), doc, docTexts.get(doc));

        assertThat(manifestCollections(doc)).containsExactly(fx.src());
        assertThat(physical(doc)).isEqualTo(fx.src());

        Map<String, Object> next = move(fx, List.of(doc));

        assertThat(failedIds(next)).isEmpty();
        assertThat(manifestCollections(doc)).containsExactly(fx.tgt());
        assertThat(physical(doc)).isEqualTo(fx.tgt());
    }

    // ═════════════════════════════════════════════════════════════════════════════
    // acceptance: a mid-move search returns each document once, one request per sibling
    // ═════════════════════════════════════════════════════════════════════════════

    @Test
    void midMoveSearch_eachDocumentIsInExactlyOneSibling_oneRequestPerSibling() {
        // Engine level: PgVectorRepository.search, one request naming one sibling, each with the
        // embedder of that sibling's model. The client's per-model grouping and union
        // (search_engine.py) is not reachable from Java; this is the engine half of it: the union of
        // the two per-sibling answers is every live chunk, and no chunk is in both.
        Fx fx = pair("midmove");
        List<String> docs = seedDocs(fx, 4, 3);
        assertThat(failedIds(move(fx, docs.subList(0, 2)))).isEmpty();

        Set<String> inSource = searchIds(srcSearch, fx.src());
        Set<String> inTarget = searchIds(tgtSearch, fx.tgt());

        for (String d : docs.subList(0, 2)) {
            for (String text : docTexts.get(d)) {
                assertThat(inTarget).as("moved doc " + d).contains(ch(text));
                assertThat(inSource).as("moved doc " + d + " is gone from the source's answer").doesNotContain(ch(text));
            }
        }
        for (String d : docs.subList(2, 4)) {
            for (String text : docTexts.get(d)) {
                assertThat(inSource).as("unmoved doc " + d).contains(ch(text));
                assertThat(inTarget).as("unmoved doc " + d + " is not in the target yet").doesNotContain(ch(text));
            }
        }
        Set<String> union = new LinkedHashSet<>(inSource);
        union.addAll(inTarget);
        Set<String> all = new LinkedHashSet<>();
        for (String d : docs) for (String text : docTexts.get(d)) all.add(ch(text));
        assertThat(union).isEqualTo(all);
        assertThat(inSource.size() + inTarget.size()).as("no chunk answers from both siblings").isEqualTo(all.size());
    }

    Set<String> searchIds(PgVectorRepository repo, String collection) {
        Set<String> ids = new LinkedHashSet<>();
        for (Map<String, Object> row : repo.search(TENANT, "lorem ipsum chunk", List.of(collection), 300, null)) {
            ids.add((String) row.get("id"));
        }
        return ids;
    }
}

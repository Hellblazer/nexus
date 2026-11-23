// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.IndexRunCensusReads;
import dev.nexus.service.db.PdfStubMetadataReads;
import dev.nexus.service.db.PipelineRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.UriSegmentCensusReads;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the census readers no other group owns: UriSegmentCensusReads, TelemetryRepository,
 * PdfStubMetadataReads, IndexRunCensusReads and PipelineRepository read a collection by its id once gate 3 has latched, and by its
 * name before. (LegacyChunkSurvey has its own class over its fixture, AspectQueueDrain a test beside its fixture.)
 *
 * <p><b>The fixture</b> is the taxonomy group's: rows are written under a registered collection (name {@code old}, id Y) by the
 * ordinary writers, then the registry row is renamed in place to {@code neu} with FK triggers off, which is the state P3.7's
 * one-row rename leaves (the registry row is Y/{@code neu}; every child row still says {@code old} and Y). A reader that names the
 * collection by its name finds the rows under {@code old} only; one that names it by its id finds them under {@code neu} only.
 * Each test makes both assertions, before the latch (a {@link Gate3#never()} reader) and after it (a fresh reader over
 * {@code () -> true}), with no restart between.
 */
class Rdr229P35cRestReadersByIdTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final Gate3 LATCHED = () -> true;

    // ---- fixture -----------------------------------------------------------------------------------------------

    private <T> T su(Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private String register(String tenant, String type, String tag) throws Exception {
        String c = type + "__p35cx" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, c);
            return null;
        });
        return c;
    }

    /** Renames the registry row of {@code old} to {@code neu} in place; the children keep {@code old}. */
    private String renameRegistryRow(String tenant, String old, String neu) throws Exception {
        su(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            return null;
        });
        return neu;
    }

    private String renameRegistryRow(String tenant, String old) throws Exception {
        return renameRegistryRow(tenant, old, old.replaceFirst("p35cx", "p35cn"));
    }

    private byte[] nextChash() {
        int n = seq.incrementAndGet();
        byte[] b = new byte[32];
        b[0] = (byte) (n >> 8);
        b[1] = (byte) n;
        b[31] = 7;
        return b;
    }

    private void insertChunk(String tenant, String collection, byte[] chash, String metadataJson) throws Exception {
        float[] v = new float[384];
        v[0] = 1f;
        su(ctx -> ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, "p35c rest")
            .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.EMBEDDING_384, Vector.of(v)).execute());
    }

    private void insertDocument(String tenant, String tumbler, String collection, String contentType, String filePath,
                                String sourceUri, String indexState, String indexContentHash) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + tumbler).set(CATALOG_DOCUMENTS.CONTENT_TYPE, contentType)
            .set(CATALOG_DOCUMENTS.FILE_PATH, filePath).set(CATALOG_DOCUMENTS.SOURCE_URI, sourceUri)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CATALOG_DOCUMENTS.INDEX_STATE, indexState.isEmpty() ? null : indexState)
            .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, indexContentHash)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{}"))
            .execute());
    }

    private void insertManifest(String tenant, String tumbler, int position, String collection, byte[] chash) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .execute());
    }

    // ---- UriSegmentCensusReads --------------------------------------------------------------------------------

    @Test
    void uriSegmentCensusReads_readsTheIdFromGate3() throws Exception {
        String old = register(TENANT, "code", "us");
        String segment = "p35cseg" + seq.incrementAndGet();
        insertDocument(TENANT, "p35c.us." + seq.incrementAndGet(), old, "code", "", "chroma://" + segment + "/x", "", "");
        String neu = renameRegistryRow(TENANT, old);

        var before = new UriSegmentCensusReads(repo).list(TENANT, 100).groups().stream()
            .filter(g -> segment.equals(g.segment())).toList();
        assertThat(before).hasSize(1);
        assertThat(before.get(0).collection()).as("by name: the row's own name").isEqualTo(old);
        assertThat(before.get(0).contentType()).as("by name the registry renamed row is not joined").isEmpty();

        var after = new UriSegmentCensusReads(repo, LATCHED).list(TENANT, 100).groups().stream()
            .filter(g -> segment.equals(g.segment())).toList();
        assertThat(after).hasSize(1);
        assertThat(after.get(0).collection()).as("by id: the registry's current label").isEqualTo(neu);
        assertThat(after.get(0).contentType()).as("by id the registry row is joined").isEqualTo("code");
        assertThat(after.get(0).rows()).isEqualTo(1);
    }

    // ---- TelemetryRepository ----------------------------------------------------------------------------------

    @Test
    void telemetryRepository_readsTheIdWhereARowCarriesOneAndTheNameWhereItDoesNot() throws Exception {
        String old = register(TENANT, "code", "tl");
        String ghost = "ghost-p35c-" + seq.incrementAndGet();   // never registered: its rows carry no id
        String now = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString();
        var byName = new TelemetryRepository(tenantScope);
        var byId = new TelemetryRepository(tenantScope, LATCHED);
        byName.logSearchBatch(TENANT, List.of(new Object[] {now, "qh-" + old, old, 5, 3, 0.4, 0.5},
            new Object[] {now, "qh-" + ghost, ghost, 5, 3, 0.4, 0.5}));
        byName.recordHookFailure(TENANT, "doc-" + seq.incrementAndGet(), old, "p35c-hook", "err", now, "", false, "single");
        byName.logRelevance(TENANT, "q-p35c-" + seq.get(), "ab".repeat(32), "star", "sess", old);
        String neu = renameRegistryRow(TENANT, old);

        // before the latch: the name path
        assertThat(byName.queryCollectionStats(TENANT, old, 365).get("row_count")).isEqualTo(1L);
        assertThat(byName.queryCollectionStats(TENANT, neu, 365).get("row_count")).isEqualTo(0L);
        assertThat(hookCollections(byName)).contains(old).doesNotContain(neu);
        assertThat(relevanceCollections(byName)).contains(old).doesNotContain(neu);

        // gate 3 latched: a row with an id by its id (under the registry's current label), a row with none by its name
        assertThat(byId.queryCollectionStats(TENANT, neu, 365).get("row_count")).isEqualTo(1L);
        assertThat(byId.queryCollectionStats(TENANT, old, 365).get("row_count"))
            .as("the row carries an id: the stale name no longer finds it").isEqualTo(0L);
        assertThat(byId.queryCollectionStats(TENANT, ghost, 365).get("row_count"))
            .as("a row with no id is found by its name").isEqualTo(1L);
        assertThat(byId.queryCollectionStats(TENANT, "", 365).get("row_count")).as("a NULL id is no collection").isEqualTo(0L);
        assertThat(hookCollections(byId)).contains(neu).doesNotContain(old);
        assertThat(relevanceCollections(byId)).contains(neu).doesNotContain(old);
    }

    @SuppressWarnings("unchecked")
    private static List<String> hookCollections(TelemetryRepository repo) {
        return ((List<Map<String, Object>>) repo.getHookFailures(TENANT, 0, List.of(), 1000).get("rows")).stream()
            .map(r -> (String) r.get("collection")).toList();
    }

    private static List<String> relevanceCollections(TelemetryRepository repo) {
        return repo.getRelevanceLog(TENANT, null, null, null, null, 1000).stream()
            .map(r -> (String) r.get("collection")).toList();
    }

    // ---- PdfStubMetadataReads ---------------------------------------------------------------------------------

    @Test
    void pdfStubMetadataReads_readsTheIdFromGate3() throws Exception {
        String old = register(TENANT, "knowledge", "ps");
        byte[] chash = nextChash();
        insertChunk(TENANT, old, chash, "{\"title\":\"\",\"content_hash\":\"deadbeefcafe0123\"}");
        String tumbler = "7." + seq.incrementAndGet() + ".9";
        insertDocument(TENANT, tumbler, old, "pdf", "/p35c/" + tumbler + ".pdf", "file:///p35c/" + tumbler + ".pdf", "", "");
        insertManifest(TENANT, tumbler, 0, old, chash);
        String neu = renameRegistryRow(TENANT, old);

        var before = stubEntry(new PdfStubMetadataReads(repo).read(TENANT, 10), neu);
        assertThat(before.stubChunks()).as("by name the label of the list names no document").isZero();
        var after = stubEntry(new PdfStubMetadataReads(repo, LATCHED).read(TENANT, 10), neu);
        assertThat(after.stubChunks()).as("by id the stub chunk is found under the new label").isEqualTo(1);
        assertThat(after.examples()).containsExactly("file:///p35c/" + tumbler + ".pdf");
    }

    private static PdfStubMetadataReads.CollectionStubs stubEntry(PdfStubMetadataReads.Page page, String collection) {
        return page.collections().stream().filter(c -> collection.equals(c.collection())).findFirst()
            .orElseThrow(() -> new AssertionError("no entry for " + collection + " in " + page));
    }

    // ---- IndexRunCensusReads ----------------------------------------------------------------------------------

    @Test
    void indexRunCensusReads_readsTheIdFromGate3() throws Exception {
        String old = register(TENANT, "code", "ir");
        String hash = "p35c-hidden-" + seq.incrementAndGet();
        String tumbler = "7." + seq.incrementAndGet() + ".8";
        insertChunk(TENANT, old, nextChash(), "{\"content_hash\":\"" + hash + "\"}");
        insertDocument(TENANT, tumbler, old, "code", "/p35c/x.py", "", "failed", hash);
        String neu = renameRegistryRow(TENANT, old);

        var before = new IndexRunCensusReads(repo).read(TENANT, 50).failedHidden().value();
        assertThat(before.examples()).extracting(IndexRunCensusReads.HiddenDoc::tumbler)
            .as("by name the document's collection is no registered label").doesNotContain(tumbler);
        var after = new IndexRunCensusReads(repo, LATCHED).read(TENANT, 50).failedHidden().value();
        assertThat(after.examples()).extracting(IndexRunCensusReads.HiddenDoc::tumbler)
            .as("by id its collection is the registry's label %s, and its chunks are found", neu).contains(tumbler);
    }

    // ---- PipelineRepository -----------------------------------------------------------------------------------

    @Test
    void pipelineRepository_readsTheIdFromGate3() throws Exception {
        String old = register(TENANT, "code", "pl");
        String hash = "p35c-pipe-" + seq.incrementAndGet();
        String path = "/p35c/pipe.pdf";
        var byName = new PipelineRepository(tenantScope);
        var byId = new PipelineRepository(tenantScope, LATCHED);
        assertThat(byName.create(TENANT, hash, path, old, true).status()).isEqualTo("created");
        String neu = renameRegistryRow(TENANT, old);

        var ref = PipelineRepository.PipelineRef.byDocument(hash, old, path);
        var refNew = PipelineRepository.PipelineRef.byDocument(hash, neu, path);
        assertThat(byName.resolve(TENANT, ref)).as("by name").isNotNull();
        assertThat(byName.resolve(TENANT, refNew)).isNull();
        assertThat(byId.resolve(TENANT, refNew)).as("by id, under the registry's current label").isNotNull();
        assertThat(byId.resolve(TENANT, ref)).as("the stale name no longer names the collection").isNull();

        assertThat(byId.deleteForCollection(TENANT, old)).as("the label that names nothing").isZero();
        assertThat(byId.deleteForCollection(TENANT, neu)).isEqualTo(1);
    }
}

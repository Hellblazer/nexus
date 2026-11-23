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
class Rdr229P35cRestReadersByIdTest extends Rdr229P35cRestFixture {

    private static final Gate3 LATCHED = () -> true;

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
        var ghostStats = byId.queryCollectionStats(TENANT, ghost, 365);
        assertThat(ghostStats.get("row_count")).as("a row with no id is no collection's: never folded into its figures").isEqualTo(0L);
        assertThat(ghostStats.get("pre_id_row_count")).as("it is reported on its own, as pre-id history").isEqualTo(1L);
        assertThat(byId.queryCollectionStats(TENANT, "", 365).get("row_count")).as("a NULL id is no collection").isEqualTo(0L);
        assertThat(byId.queryCollectionStats(TENANT, "", 365).get("pre_id_row_count")).isEqualTo(0L);
        assertThat(byName.queryCollectionStats(TENANT, ghost, 365)).as("before the latch the stats keep their shape")
            .doesNotContainKey("pre_id_row_count").containsEntry("row_count", 1L);
        assertThat(hookCollections(byId)).contains(neu).doesNotContain(old);
        assertThat(relevanceCollections(byId)).contains(neu).doesNotContain(old);
    }

    private void insertPreIdSearchRow(String tenant, String name, double kept) throws Exception {
        su(ctx -> ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY)
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.TENANT_ID, tenant)
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.TS, java.time.OffsetDateTime.now())
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.QUERY_HASH, "preid-" + seq.incrementAndGet())
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.COLLECTION, name)
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.RAW_COUNT, 5)
            .set(dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY.KEPT_COUNT, (int) kept)
            .execute());
    }

    /**
     * Q2 (final): a row written under a label before it carried an id is pre-id label history, never attributed to whichever
     * collection holds the label now. A is renamed to B (the id rows follow the id to B), then a NEW collection takes the label A:
     * the new A's figures exclude every old row, and the NULL-id rows written under "A" show only as pre_id_row_count.
     */
    @Test
    void telemetryRepository_aLabelThatPassedToAnotherCollectionNeverReachesTheOldRows() throws Exception {
        String a = register(TENANT, "code", "tq");
        String now = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString();
        var byName = new TelemetryRepository(tenantScope);
        var byId = new TelemetryRepository(tenantScope, LATCHED);
        byName.logSearchBatch(TENANT, List.<Object[]>of(new Object[] {now, "qh-id-" + a, a, 5, 3, 0.4, 0.5}));   // stamped with A's id
        insertPreIdSearchRow(TENANT, a, 0);                                                              // written before the stamp
        String b = renameRegistryRow(TENANT, a);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, a);   // a new collection takes the label A
            return null;
        });

        var newA = byId.queryCollectionStats(TENANT, a, 365);
        assertThat(newA.get("row_count")).as("the new A has none of the old collection's rows").isEqualTo(0L);
        assertThat(newA.get("zero_hit_rate")).as("no rate over rows that are not its own").isEqualTo("null");
        assertThat(newA.get("median_top_distance")).isEqualTo("null");
        assertThat(newA.get("pre_id_row_count")).as("the old rows named A with no id appear only as pre-id").isEqualTo(1L);
        var oldA = byId.queryCollectionStats(TENANT, b, 365);
        assertThat(oldA.get("row_count")).as("the id row follows its id to B").isEqualTo(1L);
        assertThat(oldA.get("pre_id_row_count")).isEqualTo(0L);
    }

    /** The listings say what a row is: an id row carries its id, a NULL-id row is flagged pre_id, a dangling id says gone. */
    @Test
    @SuppressWarnings("unchecked")
    void telemetryRepository_listingsFlagPreIdAndGoneRows() throws Exception {
        String live = register(TENANT, "code", "tf");
        String doomed = register(TENANT, "code", "td");
        String ghost = "ghost-p35c-flag-" + seq.incrementAndGet();
        String now = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString();
        var byName = new TelemetryRepository(tenantScope);
        var byId = new TelemetryRepository(tenantScope, LATCHED);
        String h = "p35c-flag-" + seq.get();
        byName.recordHookFailure(TENANT, h + "-live", live, "p35c-hook", "e", now, "", false, "single");
        byName.recordHookFailure(TENANT, h + "-ghost", ghost, "p35c-hook", "e", now, "", false, "single");
        byName.recordHookFailure(TENANT, h + "-doomed", doomed, "p35c-hook", "e", now, "", false, "single");
        Long doomedId = registryId(TENANT, doomed);
        deleteRegistryRow(TENANT, doomed);

        Map<String, Map<String, Object>> byDoc = new java.util.HashMap<>();
        for (var r : (List<Map<String, Object>>) byId.getHookFailures(TENANT, 0, List.of(), 1000).get("rows")) {
            byDoc.put((String) r.get("doc_id"), r);
        }
        assertThat(byDoc.get(h + "-live")).containsEntry("collection", live).doesNotContainKeys("pre_id", "gone")
            .containsEntry("collection_id", registryId(TENANT, live));
        assertThat(byDoc.get(h + "-ghost")).containsEntry("collection", ghost).containsEntry("pre_id", true)
            .doesNotContainKey("collection_id");
        assertThat(byDoc.get(h + "-doomed")).containsEntry("gone", true).containsEntry("collection_id", doomedId)
            .containsEntry("collection", doomed);
        for (var r : (List<Map<String, Object>>) byName.getHookFailures(TENANT, 0, List.of(), 1000).get("rows")) {
            assertThat(r).as("before the latch the listing keeps its shape").doesNotContainKeys("pre_id", "gone", "collection_id");
        }
    }

    /**
     * The rename arm retags a row that carries an id by that id, and a row with no id by name (linda 2026-11-23: telemetry is a history
     * table); from the latch the pre-id row is never stamped with the new id, so it reads as pre-id history under the new label.
     */
    @Test
    void telemetryRepository_renameLeavesPreIdRowsAlone() throws Exception {
        String old = register(TENANT, "code", "tr");
        String target = register(TENANT, "code", "tt");
        String now = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString();
        var byId = new TelemetryRepository(tenantScope, LATCHED);
        new TelemetryRepository(tenantScope).logSearchBatch(TENANT, List.<Object[]>of(new Object[] {now, "qh-r-" + old, old, 5, 3, 0.4, 0.5}));
        insertPreIdSearchRow(TENANT, old, 0);
        // the name-path rename the route follows: the catalog retires the old row as a tombstone naming the new one, then the telemetry
        // rename runs; the stamped row's id is the tombstone's, and the renamed row's own id is the successor's
        long targetId = registryId(TENANT, target);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, target).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, targetId)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute());
        assertThat(byId.renameCollection(TENANT, old, target)).containsEntry("search_telemetry", 2);
        assertThat(byId.queryCollectionStats(TENANT, target, 365).get("row_count")).as("the stamped row, by id").isEqualTo(1L);
        assertThat(byId.queryCollectionStats(TENANT, target, 365).get("pre_id_row_count"))
            .as("the pre-id row is retagged by name and stays without an id").isEqualTo(1L);
        assertThat(byId.queryCollectionStats(TENANT, old, 365).get("pre_id_row_count")).as("nothing is left under the old label").isEqualTo(0L);
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

    // ---- a superseded (retired) collection that keeps its data ---------------------------------------------------

    /** A retired collection with stub PDFs, failed-run documents and chunks is read by its own id: the readers neither skip nor lose it. */
    @Test
    void theReaders_readASupersededCollectionByItsOwnId() throws Exception {
        String stubs = register(TENANT, "knowledge", "sp");
        byte[] chash = nextChash();
        insertChunk(TENANT, stubs, chash, "{\"title\":\"\",\"content_hash\":\"deadbeefcafe0123\"}");
        String pdf = "7." + seq.incrementAndGet() + ".6";
        insertDocument(TENANT, pdf, stubs, "pdf", "/p35c/" + pdf + ".pdf", "file:///p35c/" + pdf + ".pdf", "", "");
        insertManifest(TENANT, pdf, 0, stubs, chash);
        supersede(TENANT, stubs);
        assertThat(stubEntry(new PdfStubMetadataReads(repo, LATCHED).read(TENANT, 10), stubs).stubChunks())
            .as("a kept source holding stub PDFs is not reported clean").isEqualTo(1);

        String failed = register(TENANT, "code", "sf");
        String hash = "p35c-retired-" + seq.incrementAndGet();
        String tumbler = "7." + seq.incrementAndGet() + ".5";
        insertChunk(TENANT, failed, nextChash(), "{\"content_hash\":\"" + hash + "\"}");
        insertDocument(TENANT, tumbler, failed, "code", "/p35c/r.py", "", "failed", hash);
        supersede(TENANT, failed);
        assertThat(new IndexRunCensusReads(repo, LATCHED).read(TENANT, 50).failedHidden().value().examples())
            .extracting(IndexRunCensusReads.HiddenDoc::tumbler).contains(tumbler);
    }

    /**
     * updateMetadata stamps the row's own collection id from its own name. A chunk of a superseded collection (a re-embed that kept its
     * source) must still take it: the name resolves to the retired row's id, not to NULL (which the id CHECK refuses).
     */
    @Test
    void updateMetadata_stillWorksOnAChunkOfASupersededCollection() throws Exception {
        String c = register(TENANT, "code", "um");
        byte[] chash = nextChash();
        insertChunk(TENANT, c, chash, "{}");
        supersede(TENANT, c);
        var vectors = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder);
        String hex = java.util.HexFormat.of().formatHex(chash);
        assertThat(vectors.updateMetadata(TENANT, c, List.of(hex), List.of(Map.of("frecency_score", 0.5)))).isEqualTo(1);
    }

    // ---- PipelineRepository: the ledger rule -----------------------------------------------------------------

    /** The client deletes the registry row first and then asks for the pipeline cleanup by name: the rows must still go. */
    @Test
    void pipelineRepository_deleteForCollectionStillFindsRowsOfADeletedRegistryRow() throws Exception {
        String c = register(TENANT, "code", "pd");
        String hash = "p35c-ledger-" + seq.incrementAndGet();
        var byId = new PipelineRepository(tenantScope, LATCHED);
        assertThat(byId.create(TENANT, hash, "/p35c/l.pdf", c, true).status()).isEqualTo("created");
        deleteRegistryRow(TENANT, c);
        assertThat(byId.resolve(TENANT, PipelineRepository.PipelineRef.byDocument(hash, c, "/p35c/l.pdf")))
            .as("a pipeline whose collection is gone is still found by its recorded name").isNotNull();
        assertThat(byId.deleteForCollection(TENANT, c)).isEqualTo(1);
    }

    /** A label that passed to another live collection never reaches the old collection's pipeline rows. */
    @Test
    void pipelineRepository_aReusedLabelDoesNotReachTheOldRows() throws Exception {
        String a = register(TENANT, "code", "pr");
        String hash = "p35c-reuse-" + seq.incrementAndGet();
        var byId = new PipelineRepository(tenantScope, LATCHED);
        assertThat(byId.create(TENANT, hash, "/p35c/u.pdf", a, true).status()).isEqualTo("created");
        String b = renameRegistryRow(TENANT, a);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, a);   // the label A now names another collection
            return null;
        });
        assertThat(byId.deleteForCollection(TENANT, a)).as("the new A owns none of the old rows").isZero();
        assertThat(byId.deleteForCollection(TENANT, b)).isEqualTo(1);
    }
}

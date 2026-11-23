// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the writer census ({@code scripts/rdr229_p31_writer_census.py}, {@code rdr229/p31-writer-census.tsv})
 * marks each write site of a collection-scoped table with how the scan proves its registry read: {@code require} (a registry read comes before the
 * statement in its method), {@code history}, {@code verb}, {@code sql}, {@code move}, or {@code unproven} (the scan sees no registry read before it
 * in the method; its caller may read the registry first, which the scan cannot see). The ceiling in the table's comment is editable text, so the
 * UNPROVEN set itself is pinned here: {@code file|method|table}, one entry per site. A new write site that proves nothing, or a moved
 * one, fails this test until it is read from the registry first or added below WITH a reason in the commit that adds it. Removing an entry (a site
 * that now proves its read) fails it too, so the pin only ever shrinks on purpose.
 */
class WriterCensusUnprovenPinTest {

    private static final Path TABLE = Path.of("src/test/resources/rdr229/p31-writer-census.tsv");

    /** The unproven write sites at the time of the pin: file|method|table. */
    private static final List<String> PINNED = List.of(
        "AspectRepository.java|setSalientSentences|document_aspects",
        "CatalogIdStamps.java|repairCascadedIdsFrom|chunk_orphaned_at",
        "CatalogIdStamps.java|repairCascadedIdsFrom|topic_assignments",
        "CatalogIdStamps.java|repairCascadedIds|chunk_orphaned_at",
        "CatalogIdStamps.java|repairCascadedIds|topic_assignments",
        "CatalogMoves.java|absorb|catalog_documents",
        "CatalogMoves.java|absorb|catalog_documents",
        "CatalogMoves.java|documentMove|catalog_documents",
        "CatalogMoves.java|documentMove|document_highlights",
        "CatalogMoves.java|fold|catalog_documents",
        "CatalogMoves.java|moveAspects|document_aspects",
        "CatalogMoves.java|moveAspects|document_aspects",
        "CatalogMoves.java|rekeyHighlights|document_highlights",
        "CatalogMoves.java|rewrite|catalog_documents",
        "CatalogMoves.java|rewrite|document_aspects",
        "CatalogMoves.java|rewrite|document_highlights",
        "CatalogMoves.java|trashAbsorbed|catalog_documents",
        "CatalogRepository.java|backfillTitleAndYear|catalog_documents",
        "CatalogRepository.java|beginIndexRun|catalog_documents",
        "CatalogRepository.java|buildUpdateDocumentQuery|catalog_documents",
        "CatalogRepository.java|completeIndexRun|catalog_documents",
        "CatalogRepository.java|deleteDocumentsMany|catalog_documents",
        "CatalogRepository.java|deleteDocument|catalog_documents",
        "CatalogRepository.java|failIndexRun|catalog_documents",
        "CatalogRepository.java|mergeDocuments|catalog_documents",
        "CatalogRepository.java|mergeDocuments|catalog_documents",
        "CatalogRepository.java|mergeDocuments|catalog_documents",
        "CatalogRepository.java|moveScopedTable|canary_query_ring",
        "CatalogRepository.java|moveScopedTable|catalog_document_chunks",
        "CatalogRepository.java|moveScopedTable|catalog_documents",
        "CatalogRepository.java|moveScopedTable|chunks",
        "CatalogRepository.java|moveScopedTable|document_aspects",
        "CatalogRepository.java|moveScopedTable|document_highlights",
        "CatalogRepository.java|moveScopedTable|taxonomy_centroids",
        "CatalogRepository.java|moveScopedTable|taxonomy_meta",
        "CatalogRepository.java|moveScopedTable|topic_assignments",
        "CatalogRepository.java|moveScopedTable|topics",
        "CatalogRepository.java|purgeManifest|catalog_documents",
        "CatalogRepository.java|raiseSensitivity|catalog_documents",
        "CatalogRepository.java|raiseWrittenRows|catalog_documents",
        "CatalogRepository.java|reconcileStage|catalog_reconcile_rows",
        "CatalogRepository.java|rederiveOwner|catalog_collections",
        "CatalogRepository.java|restoreDocument|catalog_documents",
        "CatalogRepository.java|resyncChunkCount|catalog_documents",
        "CatalogRepository.java|rewriteStaleNotes|catalog_documents",
        "CatalogRepository.java|rewriteStaleNotes|document_aspects",
        "CatalogRepository.java|rewriteStaleNotes|document_highlights",
        "CatalogRepository.java|setAlias|catalog_documents",
        "CatalogRepository.java|setSensitivity|catalog_documents",
        "CatalogRepository.java|stampCompleteIfVerified|catalog_documents",
        "CatalogRepository.java|stampIndexedAt|catalog_documents",
        "ChunkKeySweep.java|oneBatch|chunks",
        "CollectionMint.java|mint|catalog_collections",
        "CollectionMint.java|withDisplayName|catalog_collections",
        "CollectionRename.java|writeSuccessor|catalog_collections",
        "CollectionTombstones.java|casSupersede|catalog_collections",
        "CollectionTombstones.java|deleteRegistryRows|catalog_collections",
        "CollectionTombstones.java|repointTombstonesNaming|catalog_collections",
        "CollectionTombstones.java|stampTombstone|catalog_collections",
        "DepartedWriterSpans.java|update|chunks",
        "ManifestFill.java|applyLabels|catalog_document_chunks",
        "ManifestFill.java|applySpans|catalog_document_chunks",
        "ManifestReplace.java|update|catalog_document_chunks",
        "PgVectorRepository.java|batchUpdateMetadata|chunks",
        "PgVectorRepository.java|updateMetadataOneRow|chunks",
        "PipelineRepository.java|clearOrphanWal|pdf_pipeline",
        "PipelineRepository.java|markCompleted|pdf_pipeline",
        "PipelineRepository.java|markFailed|pdf_pipeline",
        "PipelineRepository.java|resetLeftoverRow|pdf_pipeline",
        "PipelineRepository.java|resetUploaded|pdf_pipeline",
        "PipelineRepository.java|storeExtractionMeta|pdf_pipeline",
        "PipelineRepository.java|updateProgress|pdf_pipeline",
        "QuarantineOrigin.java|retagRowsOf|chunks",
        "ReembedJobRepository.java|advanceAfterBatch|reembed_jobs",
        "ReembedJobRepository.java|applyTransition|reembed_jobs",
        "ReembedJobRepository.java|markRevived|reembed_jobs",
        "ReembedJobRepository.java|recordMoveInTxn|reembed_jobs",
        "ReembedJobRepository.java|reenter|reembed_jobs",
        "ReembedJobRepository.java|rescan|reembed_jobs",
        "ReembedJobRepository.java|setCutoverStep|reembed_jobs",
        "ReembedJobRepository.java|setLastError|reembed_jobs",
        "ReembedJobRepository.java|startNextPass|reembed_jobs",
        "TenantImportLoad.java||catalog_collections",
        "TenantImportLoad.java||catalog_document_chunks",
        "TenantImportLoad.java||catalog_documents",
        "TenantImportLoad.java||chunks",
        "TenantImportLoad.java||document_aspects",
        "TenantImportLoad.java||document_highlights",
        "TenantImportLoad.java||taxonomy_meta",
        "TenantImportLoad.java||topic_assignments",
        "TenantImportLoad.java||topics");

    @Test
    void theUnprovenWriteSitesAreExactlyThePinnedOnes() throws IOException {
        List<String> found = new ArrayList<>();
        int rows = 0;
        int registry = -1;
        int method = -1;
        int file = -1;
        int table = -1;
        for (String line : Files.readAllLines(TABLE)) {
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            String[] c = line.split("\t", -1);
            if (registry < 0) {
                List<String> head = List.of(c);
                registry = head.indexOf("registry");
                method = head.indexOf("method");
                file = head.indexOf("file_or_function");
                table = head.indexOf("table");
                assertThat(registry).as("the table has a registry column").isNotNegative();
                assertThat(method).as("the table has a method column").isNotNegative();
                continue;
            }
            rows++;
            if (c[registry].equals("unproven")) {
                String f = c[file];
                found.add(f.substring(f.lastIndexOf('/') + 1) + "|" + c[method] + "|" + c[table]);
            }
        }
        assertThat(rows).as("non-vacuity: the census rows read").isGreaterThan(200);
        assertThat(found.stream().sorted().toList()).as("the unproven write sites (file|method|table); a new one must read the registry first")
            .containsExactlyElementsOf(PINNED.stream().sorted().toList());
    }
}

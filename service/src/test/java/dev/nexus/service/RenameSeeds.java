// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import org.jooq.DSLContext;

import java.time.OffsetDateTime;
import java.util.HexFormat;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23): one row in every table that carries a collection name, for the tests that need a collection's
 * whole footprint ({@code RenameNoOpOneRowTest}). The seed is {@code CollectionRenameIdTest}'s, with the tenant a parameter.
 */
final class RenameSeeds {

    private RenameSeeds() {
    }

    private static byte[] bytes(String hex) {
        return HexFormat.of().parseHex(hex);
    }

    private static String hex(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    /** A collection holding one row in every table that carries a collection name, each carrying the collection's id (copied from CollectionRenameIdTest). */
    static void seed(DSLContext ctx, String T, String coll, String p) {
        PgContainerHelper.insertCollection(ctx, T, coll);
        final Long id = CollectionIds.require(ctx, T, coll);
        final String model = PgContainerHelper.collectionModel(ctx, T, coll);
        final String doc = p + "-doc";
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .values(T, doc, "Doc " + p, coll, id).execute();
        PgContainerHelper.insertChunk384(ctx, T, coll, bytes(hex(p + "1")), VectorSeeds.vec(384, 0.1f));
        PgContainerHelper.insertChunk384(ctx, T, coll, bytes(hex(p + "2")), VectorSeeds.vec(384, 0.1f));
        CollectionIdSeeds.manifestRows(ctx, T, doc, coll, 0, hex(p + "1"));
        // chunk 2 has no manifest owner: its orphaned-at record is the row the chunk cascade carries without its id
        ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
                CHUNK_ORPHANED_AT.COLLECTION_ID, CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.EMBEDDING_MODEL,
                CHUNK_ORPHANED_AT.ORPHANED_AT)
            .values(T, coll, id, bytes(hex(p + "2")), model, OffsetDateTime.now()).onConflictDoNothing().execute();
        long topicId = Math.abs((long) (T + coll).hashCode());
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.COLLECTION_ID,
                TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(topicId, T, "topic-" + p, coll, id, 0, OffsetDateTime.now(), "pending").execute();
        ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION, TAXONOMY_META.COLLECTION_ID)
            .values(T, coll, id).execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
            .values(T, bytes(hex(p + "1")), topicId, "projection", coll, id, OffsetDateTime.now(), model).execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.COLLECTION_ID, TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL,
                TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_384)
            .values(T, coll, id, topicId, model, "", VectorSeeds.vec(384, 0.1f)).execute();
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT,
                DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID,
                DOCUMENT_ASPECTS.SOURCE_URI)
            .values(T, coll, id, "/p/" + p + ".md", OffsetDateTime.now(), "v1", "docling", doc, "file:///p/" + p + ".md")
            .execute();
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
            .values(T, doc, coll, id, "file:///" + p + "-hl", "hi", OffsetDateTime.now()).execute();
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.COLLECTION_ID, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                RELEVANCE_LOG.TIMESTAMP)
            .values(T, "q", hex(p + "1"), coll, id, "click", "s1", OffsetDateTime.now()).execute();
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.COLLECTION_ID, SEARCH_TELEMETRY.RAW_COUNT,
                SEARCH_TELEMETRY.KEPT_COUNT)
            .values(T, OffsetDateTime.now(), "qh-" + p, coll, id, 10, 5).execute();
        ctx.insertInto(HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID, HOOK_FAILURES.COLLECTION,
                HOOK_FAILURES.COLLECTION_ID, HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR, HOOK_FAILURES.OCCURRED_AT)
            .values(T, doc, coll, id, "post_store", "boom", OffsetDateTime.now()).execute();
        ctx.insertInto(GC_AUDIT, GC_AUDIT.TENANT_ID, GC_AUDIT.OPERATION, GC_AUDIT.COLLECTION, GC_AUDIT.COLLECTION_ID)
            .values(T, "purge", coll, id).execute();
    }
}

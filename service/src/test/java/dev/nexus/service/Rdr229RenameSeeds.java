// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * Rows for the RDR-229 P3.7 rename tests, written as the superuser (RLS bypassed). {@link #full} is
 * {@code CatalogRenameCollectionTest}'s fixture: one collection with a row in every table a rename rewrites today, 4 chunks,
 * one manifest row, topics, taxonomy_meta, three centroids, two topic assignments, two aspects, one highlight. {@link #chunksAndRing}
 * is the small one: two chunks and one {@code canary_query_ring} row.
 */
final class Rdr229RenameSeeds {

    private Rdr229RenameSeeds() { /* static */ }

    /** A registry row and two chunks and one canary_query_ring row, all 384-wide, for {@code coll}. */
    static void chunksAndRing(DSLContext ctx, String tenant, String coll) {
        PgContainerHelper.insertCollection(ctx, tenant, coll);
        insertChunk384(ctx, tenant, coll, chashBytes("ring384a"), vector(384));
        insertChunk384(ctx, tenant, coll, chashBytes("ring384b"), vector(384));
        String model = PgContainerHelper.collectionModel(ctx, tenant, coll);
        byte[] digest = new byte[32];
        ctx.insertInto(CANARY_QUERY_RING)
            .set(CANARY_QUERY_RING.TENANT_ID, tenant)
            .set(CANARY_QUERY_RING.COLLECTION, coll)
            .set(CANARY_QUERY_RING.COLLECTION_ID, CollectionIds.idOf(tenant, coll))
            .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
            .set(CANARY_QUERY_RING.EMBEDDING_384, vector(384))
            .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest)
            .set(CANARY_QUERY_RING.ADDED_AT, OffsetDateTime.now())
            .execute();
    }

    static void full(DSLContext ctx, String tenant, String coll) {
        PgContainerHelper.insertCollection(ctx, tenant, coll);
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                       CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
           .values(tenant, "rn-doc-1", "Doc 1", coll, CollectionIds.require(ctx, tenant, coll))
           .execute();
        // chunks: 4 in one unified nexus.chunks table (RDR-191), all of the collection's one model (RDR-225).
        insertChunk384(ctx, tenant, coll, chashBytes("rn384a"), vector(384));
        // RDR-194 P3d (nexus-tk070.p3d): rn384b and rn768a below are REPURPOSED
        // (HexFormat.parseHex(hexChash(...)) identity, not the file's own
        // chashBytes(seed) escape-format shape) to ALSO back the two
        // topic_assignments rows further down via topic_assignments_chunk_fk --
        // reusing two of the four already-seeded chunks rather than minting two
        // more, so the "chunks (unified, 2+1+1)" count assertion elsewhere stays
        // exactly 4. The two encodings are NOT interchangeable (chashBytes(seed)
        // stores raw ASCII bytes of a hex-digit-shaped string via bytea "escape
        // format"; HexFormat.parseHex(...) stores the genuine hex-decoded bytes),
        // so this chunk's chash is no longer chashBytes("rn384b") -- nothing else
        // in this file references it by that name (unlike rn384a, reused by the
        // manifest INSERT below).
        // RDR-225: the collection is minilm-384, and a collection holds one embedding width, so the
        // four chunks (and the three centroids below) are all 384-wide; the rename counts do not
        // depend on the width.
        insertChunk384(ctx, tenant, coll, hexChashBytes("rn-doc-1"), vector(384));
        insertChunk384(ctx, tenant, coll, hexChashBytes("rn-doc-2"), vector(384));
        insertChunk384(ctx, tenant, coll, chashBytes("rn384c"), vector(384));
        final String model = PgContainerHelper.collectionModel(ctx, tenant, coll);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below -- reuse rn384a's
        // chash (rather than minting a fifth, distinct chunk) so the "chunks
        // (unified, 2+1+1)" count assertion elsewhere stays exactly 4.
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under coll, so stamp the manifest row the same.
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                       CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                       CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
           .values(tenant, "rn-doc-1", 0, chashBytes("rn384a"), coll, model, CollectionIds.require(ctx, tenant, coll))
           .execute();
        // (chash_index seeds removed — RDR-187/nexus-piwya.9: router dropped)
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + coll).hashCode());
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                       TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS, TOPICS.COLLECTION_ID)
           .values(topicId, tenant, "topic-rn", coll, 0, OffsetDateTime.now(), "pending", CollectionIds.require(ctx, tenant, coll))
           .execute();
        // taxonomy_meta: 1 (fk-003-4 RESTRICT)
        ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION, TAXONOMY_META.COLLECTION_ID)
           .values(tenant, coll, CollectionIds.require(ctx, tenant, coll))
           .execute();
        // topic_assignments: 2 (source_collection=coll). doc_id is bytea now
        // (nexus-tk070.p3c) — a genuine 64-hex chash, independent of the catalog
        // tumbler string (topic_assignments.doc_id has no FK to catalog_documents).
        // RDR-194 P3d (nexus-tk070.p3d): HexFormat.parseHex here (NOT a bare string
        // literal, which would store the ASCII bytes of the hex STRING via bytea
        // "escape format", never matching a real decoded row) so both rows resolve
        // against the two REPURPOSED chunks seeded above.
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
           .values(tenant, hexChashBytes("rn-doc-1"), topicId, "projection", coll, OffsetDateTime.now(), model, CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
           .values(tenant, hexChashBytes("rn-doc-2"), topicId, "projection", coll, OffsetDateTime.now(), model, CollectionIds.require(ctx, tenant, coll))
           .execute();
        // centroids: three, one unified nexus.taxonomy_centroids table (RDR-191), all 384-wide (RDR-225).
        // PK is (tenant_id, collection, topic_id, embedding_model) -- three DIFFERENT topic_ids, not
        // the shared `topicId` above.
        // hygiene-001 step 9b (nexus-tk070.p6a follow-on): label is NOT NULL now,
        // no default -- supply it explicitly.
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                       TAXONOMY_CENTROIDS.EMBEDDING_384, TAXONOMY_CENTROIDS.COLLECTION_ID)
           .values(tenant, coll, topicId, model, "", vector(384), CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                       TAXONOMY_CENTROIDS.EMBEDDING_384, TAXONOMY_CENTROIDS.COLLECTION_ID)
           .values(tenant, coll, topicId + 1, model, "", vector(384), CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                       TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                       TAXONOMY_CENTROIDS.EMBEDDING_384, TAXONOMY_CENTROIDS.COLLECTION_ID)
           .values(tenant, coll, topicId + 2, model, "", vector(384), CollectionIds.require(ctx, tenant, coll))
           .execute();
        // document_aspects: 2, both doc-rooted at rn-doc-1 (the collection's only
        // registered catalog_documents row -- catalog_documents count elsewhere in
        // this fixture is asserted ==1, so a second row cannot be introduced here).
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): doc_id/source_uri are both
        // NOT NULL now, reversing fk-001-2's nullable conversion -- the second row
        // used to be DOC-LESS (doc_id=NULL); that state is no longer representable,
        // so it is now a second row against the same doc, distinguished by source_path.
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.COLLECTION_ID)
           .values(tenant, coll, "/p/a1.md", OffsetDateTime.now(), "v1", "docling", "rn-doc-1", "file:///p/a1.md", CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                       DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                       DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.COLLECTION_ID)
           .values(tenant, coll, "/p/a2.md", OffsetDateTime.now(), "v1", "docling", "rn-doc-1", "file:///p/a2.md", CollectionIds.require(ctx, tenant, coll))
           .execute();
        // document_highlights: 1. hygiene-001 step 3: source_uri is NOT NULL now too.
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                       DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                       DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT, DOCUMENT_HIGHLIGHTS.COLLECTION_ID)
           .values(tenant, "rn-doc-1", coll, "file:///rn-doc-1-hl", "hi", OffsetDateTime.now(), CollectionIds.require(ctx, tenant, coll))
           .execute();
        // search_telemetry: 2 (no FK, but re-homed)
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                       SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT, SEARCH_TELEMETRY.COLLECTION_ID)
           .values(tenant, OffsetDateTime.now(), "qh1", coll, 10, 5, CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                       SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT, SEARCH_TELEMETRY.COLLECTION_ID)
           .values(tenant, OffsetDateTime.now(), "qh2", coll, 8, 4, CollectionIds.require(ctx, tenant, coll))
           .execute();
        // hook_failures: 1 (no FK, but re-homed)
        ctx.insertInto(HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID, HOOK_FAILURES.COLLECTION,
                       HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR, HOOK_FAILURES.OCCURRED_AT, HOOK_FAILURES.COLLECTION_ID)
           .values(tenant, "rn-doc-1", coll, "post_store", "boom", OffsetDateTime.now(), CollectionIds.require(ctx, tenant, coll))
           .execute();
        // relevance_log: 2 (no FK, but re-homed — RDR-164 §Approach Phase 3 third audit table)
        // nexus-lgdel.l1: chunk_id must be canonical 64-hex TEXT now
        // (relevance_log_chunk_id_canonical_check, legacy-001-drop-chash-
        // alias.xml) — 'ch1'/'ch2' no longer pass. NOT the chashBytes(seed) helper
        // below: that produces a 32-ASCII-char bytea value for the chunks.chash
        // column; chunk_id here is TEXT and needs a real 64-hex STRING.
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                       RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                       RELEVANCE_LOG.TIMESTAMP, RELEVANCE_LOG.COLLECTION_ID)
           .values(tenant, "q1", "0b2ace5def2ecf1234ae0db2a062b83fe40dd330121844b37bc8bdfb6b2f3ea5", coll, "click",
                   "s1", OffsetDateTime.now(), CollectionIds.require(ctx, tenant, coll))
           .execute();
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                       RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                       RELEVANCE_LOG.TIMESTAMP, RELEVANCE_LOG.COLLECTION_ID)
           .values(tenant, "q2", "f7088d7f354fadfc6fe69df2f0a9f2057a715e9a62672258a88ee200e72f1c22", coll, "skip",
                   "s1", OffsetDateTime.now(), CollectionIds.require(ctx, tenant, coll))
           .execute();
    }

    private static void insertChunk384(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        PgContainerHelper.insertChunk384(ctx, tenant, collection, chashBytes, v);
    }

    private static void insertChunk768(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        PgContainerHelper.insertChunk768(ctx, tenant, collection, chashBytes, v);
    }

    /** A pgvector value with every one of {@code dim} components equal to {@code 0.1}. */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /** 32-char hex-alphabet label, stored as its own ASCII bytes (no real hash semantics --
     *  matches the pre-conversion raw-SQL behavior of a bare string literal into a
     *  {@code bytea} column via PostgreSQL's escape-format input, {@link #hexChash} below is
     *  the genuine-hash counterpart). */
    private static byte[] chashBytes(String seed) {
        String label = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return label.getBytes(StandardCharsets.US_ASCII);
    }

    private static byte[] hexChashBytes(String seed) {
        return java.util.HexFormat.of().parseHex(hexChash(seed));
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c), unlike {@link #chashBytes} above which is a
     *  32-char synthetic id used only for the chunks/manifest {@code chash} column. */
    static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

}

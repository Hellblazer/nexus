// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;

/**
 * RDR-254 (aspect-queue race, found by the P5 minimum viable validation of GH #1571): which {@code doc_id} a
 * {@code document_aspects} row ends with when {@link AspectRepository#upsertAspect} lands on an existing row.
 *
 * <p>An extraction is queued for a document T, a document move then absorbs T into S (S's row at the new path is
 * re-keyed to S, T is trashed), and the extraction completes. Its upsert matches S's row by
 * (tenant, collection, source_path), and without this check would set {@code doc_id} back to T, so
 * {@code get_by_doc_id(S)} found nothing. With it, a doc_id that names a trashed document or an alias row never
 * replaces the row's doc_id, and the late write still updates the aspect FIELDS: T and S are the same file at the
 * same path, and the move itself keeps the newer extraction when T's row is the newer one. A first write (no
 * conflict) inserts as before. No wire change: the answer is still {@code written:true} with the row's id. The
 * {@code import*} verbs do not use this: they restore exported rows as they were.
 *
 * <p><b>Locks.</b> The read takes {@code FOR SHARE} on the document's catalog row, in the upsert's transaction and
 * before the {@code document_aspects} write, so it cannot race a move: a move holding T's row (it trashes T, and it
 * locks catalog rows before it touches aspect rows) makes this wait for its commit and read T trashed; a move not
 * yet started waits for this transaction. The order is catalog row, then {@code document_aspects}, the move's order.
 * It is the REVERSE of {@code renameCollectionTxn} and the collection delete ({@code COLLECTION_SCOPED_TABLES} lists
 * {@code document_aspects} before {@code catalog_documents}), so an UNFENCED upsert running during a rename or delete
 * of the same collection can deadlock, and PostgreSQL aborts one of the two. Both sides run their whole transaction
 * under {@link DeadlockRetry} (the upsert in {@link AspectRepository#upsertAspect}, the rename and the delete through
 * {@code CatalogRepository.manifestWriteTxn}), so the aborted one reruns, up to four attempts, after which the 40P01
 * propagates. A fenced (worker) upsert usually takes the claim's tuples row lock first, which serializes it with the rename's task rewrite before
 * either reaches these tables.
 */
final class AspectDocIdAttribution {

    private AspectDocIdAttribution() { }

    /**
     * The value for {@code doc_id} in an aspect upsert's {@code ON CONFLICT DO UPDATE}: the row's own
     * {@code doc_id} when {@code docId} names a trashed or alias document, {@code incomingWins} otherwise.
     */
    static Field<String> onConflict(DSLContext ctx, String tenant, String docId, Field<String> incomingWins) {
        return namesNonLiveDocument(ctx, tenant, docId) ? DOCUMENT_ASPECTS.DOC_ID : incomingWins;
    }

    /**
     * Whether {@code docId} names a document that is no longer a live identity: trashed ({@code deleted_at} set) or
     * an alias row ({@code alias_of} set). A document with no catalog row is neither; the insert's foreign key
     * refuses it as before.
     */
    static boolean namesNonLiveDocument(DSLContext ctx, String tenant, String docId) {
        var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT, CATALOG_DOCUMENTS.ALIAS_OF)
            .from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId)))
            .forShare()
            .fetchOne();
        if (row == null) return false;
        String aliasOf = row.value2();
        return row.value1() != null || (aliasOf != null && !aliasOf.isBlank());
    }
}

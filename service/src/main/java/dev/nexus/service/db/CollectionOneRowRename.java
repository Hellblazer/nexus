// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record6;
import org.jooq.impl.DSL;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27, item 6 "Switch" and item 7): the one-row rename. From gate 3, once the name-key drop move has completed
 * and every P3.5b changeset has applied ({@link RenameGuard}), a rename updates the registry row's label and nothing else of the
 * collection: the row keeps its {@code collection_id}, so no chunk, manifest, taxonomy, aspect, highlight or telemetry row is rewritten and the
 * per-collection indexes (whose predicate is the id) stay. The referencing tables' name columns keep the old label: they are dead, read by
 * no id reader, and dropped by P4.0/P4.1. {@link CatalogRepository#renameCollection} delegates here (over its size ceiling) from inside its rename
 * transaction, after the checks every rename shares (the sweep gates, the re-embed job, the model token, the live-target refusal).
 *
 * <p>What it writes, in the caller's transaction:
 * <ol>
 * <li>The registry row: {@code name} (and {@code content_type} and {@code owner_id} when the client named them, as the name path does). The
 *     row is found by its old label and must be live. A tombstone at the new label, left by a rename before gate 3 and empty (the caller
 *     refused a populated or foreign one), is deleted first ({@link CollectionTombstones#deleteRegistryRows}); the tombstones that named it
 *     follow the renamed row by id afterwards ({@link CollectionTombstones#repointTombstonesNaming}).</li>
 * <li>Tombstones whose {@code superseded_by} named the old label now name the new one (their id already pointed at this row), so the name
 *     chain the name readers follow, until P4.0 retires them, still ends at a registered row.</li>
 * <li>The {@code nx-note://} source URIs of the collection's documents, aspects and highlights, limited to what
 *     {@link NoteUri#namesCollection} classes as a note (item 7): never {@code chroma://} residue. The rows are selected by the collection's
 *     id, not by its old label, because rows written after an earlier one-row rename carry a newer label.</li>
 * <li>The queued aspect tasks, which name the collection in their body ({@link AspectTaskRefs}), and the quarantine origin tags
 *     ({@link QuarantineOrigin#retagRowsOf}), which the engine finds a sibling by.</li>
 * </ol>
 * It does not rewrite the fourteen tables of {@code CatalogRepository.COLLECTION_SCOPED_TABLES} or the history tables.
 */
final class CollectionOneRowRename {

    private CollectionOneRowRename() {
    }

    /**
     * @param newContentType the client's content type for the new label, or null to keep the row's
     * @param newOwnerId     the client's owner, or null to keep the row's
     * @param targetRowExists a row of the new label exists (the caller established it is an empty tombstone)
     * @param tuple          receives the row's tuple for the caller's race recheck ({@link CollectionMint#recheckOf})
     * @return the counts the rename route answers: {@code catalog_collections_renamed}, a zero for each table the name path rewrites (nothing
     *         is), the URI counts and the queued tasks
     */
    static Map<String, Integer> apply(DSLContext ctx, String tenant, String oldName, String newName, String newContentType,
                                      String newOwnerId, boolean targetRowExists, String[] tuple) {
        // LOCK ORDER (linda, folio's P3.5a review): the rename lock (RenameLatchLock SHARED, taken by the caller), then FOR UPDATE on the registry rows
        // this rename touches, in ascending collection_id order, then everything else. The id writers hold FOR KEY SHARE on the registry row; once the
        // name key is gone the name UPDATE below is a non-key update, so without FOR UPDATE a writer could commit into the row this rename just retired.
        ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.in(oldName, newName)))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID).forUpdate().fetch();
        Record6<Long, String, String, String, String, String> own = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(oldName))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")))
            .forUpdate()
            .fetchOne();
        if (own == null) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": " + oldName
                + " is not a live registered collection (it was retired or removed while this one ran); nothing was renamed");
        }
        long id = own.value1();
        // linda 2026-11-23, addendum 8: a label a collection was renamed away from is reserved until P4.1, unless this very collection takes it back
        LabelReserved.requireFree(ctx, tenant, newName, id);
        String contentType = newContentType != null ? newContentType : own.value2();
        // The owner a settled collection holds is not replaced by a client's slug: see ownerTheRenameMayChange.
        newOwnerId = ownerTheRenameMayChange(contentType, own.value3(), newOwnerId);
        String owner = newOwnerId != null ? newOwnerId : own.value3();
        if (!CollectionMint.tupleIndexExists(ctx)) {
            // the row's tuple and the new one (the same key when the client names none): sorted and once each, as the name path takes them
            CollectionTupleLock.lock(ctx, List.of(
                new CollectionTupleLock.Tuple(tenant, own.value2(), own.value3(), own.value4(), own.value5()),
                new CollectionTupleLock.Tuple(tenant, contentType, owner, own.value4(), own.value5())));
        }
        Map<String, Integer> counts = new LinkedHashMap<>();
        if (targetRowExists) {
            CollectionTombstones.deleteRegistryRows(ctx, tenant, List.of(newName));
        }
        Map<Field<?>, Object> set = new LinkedHashMap<>();
        set.put(CATALOG_COLLECTIONS.NAME, newName);
        if (newContentType != null) {
            set.put(CATALOG_COLLECTIONS.CONTENT_TYPE, newContentType);
        }
        if (newOwnerId != null) {
            set.put(CATALOG_COLLECTIONS.OWNER_ID, newOwnerId);
        }
        int renamed = ctx.update(CATALOG_COLLECTIONS).set(set)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
                .and(CATALOG_COLLECTIONS.NAME.eq(oldName)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")))
            .execute();
        if (renamed == 0) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": " + oldName
                + " was retired or renamed by another request while this one ran; nothing was renamed, so try again");
        }
        counts.put("catalog_collections_renamed", renamed);
        // the row's tuple after the rename: refused, naming both, when another live collection holds it (the index raises 23505 first, and the recheck names both)
        CollectionMint.remember(tuple, newName, contentType, owner, own.value4(), own.value5());
        CollectionMint.refuseHeldTuple(ctx, tenant, newName, contentType, owner, own.value4(), own.value5(), own.value6(), "");

        ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, newName)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(oldName))
                // only the tombstones that point at THIS collection: a tombstone that names the old label but another id (a tombstone of an
                // older collection of that label) is not this rename's to repoint (review comment 4)
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull().or(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(id)))).execute();
        CollectionTombstones.repointTombstonesNaming(ctx, tenant, List.of(newName));

        requireUrisFree(ctx, tenant, id, oldName, newName);
        // the queued tasks first: their tuple row locks come before the document_aspects write, the order the claim-fenced aspect write takes
        counts.put(CatalogRepository.ASPECT_TASKS_KEY, AspectTaskRefs.rewriteCollection(ctx, tenant, oldName, newName));
        for (CatalogRepository.CollectionScopedTable t : CatalogRepository.COLLECTION_SCOPED_TABLES) {
            counts.put(t.countKey(), 0);   // rewritten by the name path only: the contract of the counts map is the key, and a one-row rename rewrites none
        }
        counts.put("catalog_documents_source_uri", ctx.update(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, NoteUri.moveTo(CATALOG_DOCUMENTS.SOURCE_URI, oldName, newName))
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.eq(id)))
            .and(NoteUri.namesCollection(CATALOG_DOCUMENTS.SOURCE_URI, oldName)).execute());
        counts.put("document_aspects_source_uri", ctx.update(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.SOURCE_URI, NoteUri.moveTo(DOCUMENT_ASPECTS.SOURCE_URI, oldName, newName))
            .set(DOCUMENT_ASPECTS.COLLECTION_ID, CatalogIdStamps.keepAspectId())
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.COLLECTION_ID.eq(id)))
            .and(NoteUri.namesCollection(DOCUMENT_ASPECTS.SOURCE_URI, oldName)).execute());
        counts.put("document_highlights_source_uri", ctx.update(DOCUMENT_HIGHLIGHTS)
            .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, NoteUri.moveTo(DOCUMENT_HIGHLIGHTS.SOURCE_URI, oldName, newName))
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, CatalogIdStamps.keepHighlightId())
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION_ID.eq(id)))
            .and(NoteUri.namesCollection(DOCUMENT_HIGHLIGHTS.SOURCE_URI, oldName)).execute());
        QuarantineOrigin.retagRowsOf(ctx, tenant, oldName, newName);
        return counts;
    }

    /** The shape of an owner that documents settled: a tumbler segment such as {@code 1-2} ({@code catalog_collection_owner_replaceable}'s test). */
    private static final java.util.regex.Pattern SETTLED_OWNER = java.util.regex.Pattern.compile("^[0-9]+(-[0-9]+)*$");

    /** The content types whose owner is derived from the documents' tumblers ({@code catalog_collections_owner_from_docs}). */
    private static final java.util.Set<String> DOCUMENT_OWNED = java.util.Set.of("code", "docs", "rdr");

    /**
     * Owner re-derivation in the one-row path (stage-1 review S3). The name path re-homes the documents under the new label and then
     * re-derives a replaceable owner from them ({@code rederiveOwner}: catalog-044/051's triggers read the documents by
     * {@code physical_collection = name}), so a code collection whose documents sit under tumbler owner {@code 1-2} ends with that owner
     * whatever slug the new label carries. The one-row path leaves the documents' name column at the old label, so the same triggers,
     * keyed on the new label, find no document and would leave the client's slug in place, replacing a settled owner.
     *
     * <p>Re-deriving by {@code collection_id} would need a second copy of catalog-044's derivation and 051's tuple guard; P3.5b redefines
     * those functions to read the id, so this path does not copy them. It keeps the invariant they enforce instead, for the content types
     * they derive for ({@code code}, {@code docs}, {@code rdr}): an owner that is a tumbler segment is settled and a SLUG never replaces
     * it. A client's explicit tumbler-shaped owner is applied, as the name path applies it, and so is any owner of a collection that is
     * not document-owned (a {@code knowledge} collection with an all-digit owner takes the client's). For a settled owner and a requested slug
     * the rename changes the label and leaves the owner as it is, which is what the name path ends with for a collection whose documents
     * name that owner.
     *
     * @param contentType  the collection's content type after the rename
     * @return {@code requested}, or null when nothing is requested or the row's settled owner must not be replaced by a slug
     */
    static String ownerTheRenameMayChange(String contentType, String currentOwner, String requested) {
        if (requested == null) {
            return null;
        }
        boolean settledNow = currentOwner != null && SETTLED_OWNER.matcher(currentOwner).matches();
        boolean requestedIsSlug = !SETTLED_OWNER.matcher(requested).matches();
        return DOCUMENT_OWNED.contains(contentType) && settledNow && requestedIsSlug ? null : requested;
    }

    /**
     * RDR-231: refuse, naming the holder, when a note URI of the collection's own live documents, rewritten by {@link NoteUri#moveTo}, is
     * already held by a different live document (catalog-016's live-URI index would abort the UPDATE as an opaque 500). The documents are
     * the collection's by id, as the rewrite below selects them.
     */
    private static void requireUrisFree(DSLContext ctx, String tenant, long id, String oldName, String newName) {
        var moving = CATALOG_DOCUMENTS.as("moving");
        var holder = CATALOG_DOCUMENTS.as("holder");
        Field<String> rewritten = NoteUri.moveTo(moving.SOURCE_URI, oldName, newName);
        var hit = ctx.select(moving.TUMBLER, holder.SOURCE_URI, holder.TUMBLER, holder.PHYSICAL_COLLECTION)
            .from(moving).join(holder).on(holder.TENANT_ID.eq(tenant).and(holder.SOURCE_URI.eq(rewritten)))
            .where(moving.TENANT_ID.eq(tenant).and(moving.PHYSICAL_COLLECTION_ID.eq(id)))
            .and(NoteUri.namesCollection(moving.SOURCE_URI, oldName))
            .and(moving.DELETED_AT.isNull()).and(rewritten.ne(moving.SOURCE_URI))
            .and(holder.DELETED_AT.isNull()).and(holder.TUMBLER.ne(moving.TUMBLER))
            .orderBy(moving.TUMBLER).limit(1).fetchOne();
        if (hit != null) {
            throw new CatalogRepository.SourceUriCollision("renaming " + oldName + " to " + newName, hit.value2(), hit.value3(),
                hit.value4());
        }
    }
}

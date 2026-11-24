// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record6;

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
public final class CollectionOneRowRename implements OneRowRename {

    /**
     * Gate 3 for the helpers this body calls, the service's own signal ({@code NexusService} passes the one it gives every repository). This body runs
     * only on the one-row path, which {@link RenameGuard#runRename} takes only once the latch row exists and the readiness reads none; a sample that
     * read the latch row answers none only while this signal is set ({@code CompletionBootWork} sets it before it publishes the sample, and its
     * readiness answers unreadable until it is set; a lost latch row is refused before this body), so on this path the signal holds and the helpers take the id forms every reader takes. Across engines there is no
     * window: {@code EngineSingletonLock} (nexus-5tykh) allows one engine per database.
     */
    private final dev.nexus.service.datamove.Gate3 gate3;

    /** The seam implementation {@code NexusService} wires into the one {@link RenameGuard}, over the service's {@code gate3}. */
    public CollectionOneRowRename(dev.nexus.service.datamove.Gate3 gate3) {
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
    }

    @Override
    public RenameOutcome rename(DSLContext ctx, String tenant, String oldLabel, String newLabel, Request request) {
        Outcome o = apply(ctx, gate3, tenant, oldLabel, newLabel, request.sourceId(), request.newContentType(), request.newOwnerId(),
            request.targetUnderLock(), request.tupleOut());
        // read here, inside the rename's own tenant scope (CatalogEpoch.of is empty outside it); the id survived, so ids are stable
        return new RenameOutcome(o.counts(), o.collectionId(), CatalogEpoch.of(ctx, tenant).orElse(null), true);
    }

    /**
     * @param gate3          the service's gate-3 signal, handed to the helpers below that read by id once it holds
     * @param sourceId       the id the request named the source by, or null for a request by label (see {@link OneRowRename.Request})
     * @param newContentType the client's content type for the new label, or null to keep the row's
     * @param newOwnerId     the client's owner, or null to keep the row's
     * @param targetUnderLock the caller's target checks, run here under the registry row locks: they refuse a live or populated target and answer
     *                        whether a retired row of the new label exists now (folio's r3 critique F1)
     * @param tuple          receives the row's tuple for the caller's race recheck ({@link CollectionMint#recheckOf})
     *
     * <p>The CALLER takes the sweep gates (SHARED, sorted, keyed by the old and the new label) and runs the shared checks before this body, in the same
     * transaction and after the rename lock; this body then takes the tuple locks and the registry row locks (see the lock order in the code), so every advisory
     * lock precedes any row lock.
     * @return the counts the rename route answers: {@code catalog_collections_renamed}, a zero for each table the name path rewrites (nothing
     *         is), the URI counts and the queued tasks
     */
    /** What the one-row rename answers: the renamed collection's id (slice C's {@code RenameOutcome} carries it) and the counts the route reports. */
    record Outcome(long collectionId, Map<String, Integer> counts) { }

    static Outcome apply(DSLContext ctx, dev.nexus.service.datamove.Gate3 gate3, String tenant, String oldName, String newName, Long sourceId, String newContentType,
                                      String newOwnerId, OneRowRename.TargetCheck targetUnderLock, String[] tuple) {
        // LOCK ORDER (linda's canonical order, via tamsin; runRename's javadoc on slice C states it once): the rename lock (RenameLatchLock SHARED, taken by the
        // caller), then the sweep gate(s) SHARED (the shared checks the caller runs before this body), then the collection tuple lock(s), then the registry rows
        // in collection_id order, FOR UPDATE, then the child rows. Every advisory lock comes before any row lock. The id writers hold FOR KEY SHARE on the
        // registry row; once the name key is gone the name UPDATE below is a non-key update, so without FOR UPDATE a writer could commit into the row this
        // rename just retired.
        //
        // The tuple locks are keyed on the row's tuple, so the row is read once WITHOUT a lock to name them, then locked and read again.
        Record6<Long, String, String, String, String, String> peek = readOwn(ctx, tenant, oldName, false);
        if (peek == null) {
            if (sourceId != null) {
                // the label names no live row now. The request named the source by id, so say why that is typed: an id with no row, a retired one, or one the
                // label was renamed away from (its row is locked with the others first, in id order, as below)
                CollectionTombstones.lockRowsOfOneRowRename(ctx, tenant, oldName, newName, sourceId);
                CollectionRefusals.requireSourceById(ctx, tenant, sourceId, oldName);
            }
            throw notLive(oldName, newName);
        }
        String peekContentType = newContentType != null ? newContentType : peek.value2();
        newOwnerId = ownerTheRenameMayChange(peekContentType, peek.value3(), newOwnerId);
        String peekOwner = newOwnerId != null ? newOwnerId : peek.value3();
        if (!CollectionMint.tupleIndexExists(ctx)) {
            // the row's tuple and the new one (the same key when the client names none): sorted and once each, as the name path takes them
            CollectionTupleLock.lock(ctx, List.of(
                new CollectionTupleLock.Tuple(tenant, peek.value2(), peek.value3(), peek.value4(), peek.value5()),
                new CollectionTupleLock.Tuple(tenant, peekContentType, peekOwner, peek.value4(), peek.value5())));
        }
        // EVERY registry row the rename will touch is locked here, in ONE statement ordered by collection_id (linda's census, item 3): the old row, the
        // target or the tombstone at the new label (which is deleted), the tombstones whose superseded_by names the old label (repointed), those that point
        // at the target tombstone's id (repointed after its delete), and every tombstone whose superseded_by names the NEW label whatever id it carries:
        // repointTombstonesNaming below updates them all, a self-pointing or unstamped one included (linda's census pass 2). Locking the tombstones later,
        // after the rename's own row, would take them out of id order.
        CollectionTombstones.lockRowsOfOneRowRename(ctx, tenant, oldName, newName, sourceId);   // bounded: RenameBusyException, 503 rename_busy, nothing written
        if (sourceId != null) {
            // the request named the source by id: the label was resolved from it outside this transaction, and a label freed and registered again in between
            // would make readOwn below lock and rename the new holder. The id's row is held now (the statement above took it in id order): refuse, before any
            // read of the label and any write, an id that is unknown or retired or that no longer bears oldName
            CollectionRefusals.requireSourceById(ctx, tenant, sourceId, oldName);
        }
        Record6<Long, String, String, String, String, String> own = readOwn(ctx, tenant, oldName, true);
        if (own == null) {
            throw notLive(oldName, newName);
        }
        if (!own.value2().equals(peek.value2()) || !java.util.Objects.equals(own.value3(), peek.value3()) || !own.value4().equals(peek.value4())
                || !own.value5().equals(peek.value5())) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": " + oldName
                + " changed attributes while this one ran; nothing was renamed, so try again");
        }
        long id = own.value1();
        // folio's r3 critique F1 (linda: a ship-blocker): the caller's target checks ran before any lock, and this rename may have waited here while a
        // registration revived the tombstone at the new label and a writer filed rows under it. Run them again now that every row of the new label is locked,
        // so a target that became live or populated is refused, and decide the delete from what they read now.
        boolean targetRowExists = targetUnderLock.underLock(ctx);
        // linda 2026-11-23, addendum 8: a label a collection was renamed away from is reserved until P4.1, unless this very collection takes it back
        LabelReserved.requireFree(ctx, tenant, newName, id);
        String contentType = peekContentType;
        String owner = peekOwner;
        Map<String, Integer> counts = new LinkedHashMap<>();
        if (targetRowExists) {
            deleteRetiredTarget(ctx, gate3, tenant, oldName, newName);
        }
        // remembered BEFORE the UPDATE (folio's r3 review F5): a 23505 the UPDATE raises on the live-tuple index then finds the tuple, and the caller's race
        // recheck (CollectionMint#recheckOf) names the holder instead of a generic 409
        CollectionMint.remember(tuple, newName, contentType, owner, own.value4(), own.value5());
        Map<Field<?>, Object> set = new LinkedHashMap<>();
        set.put(CATALOG_COLLECTIONS.NAME, newName);
        if (newContentType != null) {
            set.put(CATALOG_COLLECTIONS.CONTENT_TYPE, newContentType);
        }
        if (newOwnerId != null) {
            set.put(CATALOG_COLLECTIONS.OWNER_ID, newOwnerId);
        }
        CollectionRegistry.forgetWriteMemo(ctx);   // a write of the registry row: a row this transaction read under the old label is not the row now
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
        CollectionMint.refuseHeldTuple(ctx, tenant, newName, contentType, owner, own.value4(), own.value5(), own.value6(), "", gate3);

        CollectionRegistry.forgetWriteMemo(ctx);   // the tuple check above may have read a row; the tombstones repointed here are rows of the registry too
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
        // the latched gate (this body runs only past the latch): quarantine rows found by the collection's id and siblings by the registry's label, as every
        // other gate-3 reader (linda's census pass 2; the 4-argument form passes Gate3.never()); the origin is named by id, because its registry row already
        // carries the new label, so an attribute-derived sibling is found through it (folio's r3 review F4)
        QuarantineOrigin.retagRowsOf(ctx, tenant, oldName, newName, gate3, id);
        return new Outcome(id, counts);
    }

    /**
     * Deletes the retired row(s) of the new label BY ID (linda's ruling on folio's r3 F1): the ids of the rows this rename holds FOR UPDATE, never a delete by
     * label. A belt under the caller's checks: a live row of the label, a row under one of those ids in any collection-scoped child table, or a queued
     * aspect task naming the label is refused and nothing is deleted.
     */
    private static void deleteRetiredTarget(DSLContext ctx, dev.nexus.service.datamove.Gate3 gate3, String tenant, String oldName, String newName) {
        var rows = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(newName)))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID).fetch();
        if (rows.stream().anyMatch(r -> r.value2() == null || r.value2().isEmpty())) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": target collection already exists: " + newName
                + " (it became live while this rename ran); nothing was renamed");
        }
        List<Long> ids = rows.map(r -> r.value1());
        // every collection-scoped child table, by the locked rows' ids (whatever a row's name column says), and the queued aspect tasks naming the label: the
        // tables the caller's blockingTable walks (folio on 6eb8000e0d: chunks alone left a manifest, a document or an aspect unchecked)
        for (CatalogRepository.CollectionScopedTable t : CatalogRepository.COLLECTION_SCOPED_TABLES) {
            if (ctx.fetchExists(ctx.selectOne().from(t.table()).where(t.collectionId().in(ids)))) {
                throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": the retired target " + newName
                    + " holds rows in " + t.countKey() + "; renaming onto it would merge two collections, so nothing was renamed");
            }
        }
        if (AspectTaskRefs.countNaming(ctx, tenant, newName) > 0) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": queued aspect tasks name the retired target "
                + newName + "; renaming onto it would merge two collections, so nothing was renamed");
        }
        for (long targetId : ids) {
            CollectionTombstones.deleteRegistryRowById(ctx, tenant, newName, targetId, List.of(), gate3);
        }
    }

    private static Record6<Long, String, String, String, String, String> readOwn(DSLContext ctx, String tenant, String oldName, boolean forUpdate) {
        var q = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(oldName))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")));
        return forUpdate ? q.forUpdate().fetchOne() : q.fetchOne();
    }

    private static CatalogRepository.CollectionMergeRefused notLive(String oldName, String newName) {
        return new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": " + oldName
            + " is not a live registered collection (it was retired or removed while this one ran); nothing was renamed");
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

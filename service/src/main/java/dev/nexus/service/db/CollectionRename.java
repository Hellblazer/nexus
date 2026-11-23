// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record4;
import org.jooq.Record5;
import org.jooq.impl.DSL;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The registry half of a collection rename (RDR-229 P3.8, nexus-p2m1x.28): retire the old row X, naming its successor, and
 * register or revive the new row Y. {@link CatalogRepository#renameCollection} (over its size ceiling) delegates here.
 *
 * <p><b>The order is the tuples locked, X retired, then Y written.</b> A rename copies X's attributes onto Y, so Y has the live tuple
 * {@code (content_type, owner_id, embedding_model, model_version)} that X holds until it is retired. With the partial UNIQUE
 * index {@value CollectionTupleIndexProbe#INDEX} present, inserting Y live while X is still live raises 23505 (an unnamed
 * error, on a plain rename as well as on the way back). And P3.1's {@code catalog_collections_superseded_by_id_check} ties
 * {@code superseded_by = ''} to {@code superseded_by_id IS NULL}, so X cannot be retired with only a name: it needs Y's id
 * before Y has a row. So:
 * <ol>
 * <li>With the index absent, the advisory locks of X's tuple and Y's tuple, in the sorted order the batch paths take theirs
 *     ({@link CollectionTupleLock#lock}), before either row is touched: a registration takes a tuple's lock and then
 *     its row, so a rename that held X's row and then waited for a tuple's lock could meet it head on.</li>
 * <li>Y's id: the id of Y's own tombstone when Y is being revived (a rename back keeps the row and its id), else a fresh draw
 *     from the column's sequence ({@link CollectionMint#drawId}: taken-id check, one forward heal).</li>
 * <li>X is retired through {@link CollectionTombstones#supersede(DSLContext, String, String, String, long)}, the one writer
 *     of {@code superseded_by_id} and the one that takes the id from the caller: {@code superseded_by = Y's name},
 *     {@code superseded_by_id = that id}, under the supersede CAS (an X another writer retired for a third collection refuses).</li>
 * <li>When the caller names another content type or owner, so Y's tuple is not X's, it is checked against every other live
 *     collection ({@link CollectionMint#refuseHeldTuple}, a named 409 in both index states). When Y takes X's own tuple it
 *     replaces X there and nothing is checked.</li>
 * <li>Y is inserted with that id, or revived: {@code superseded_by} and {@code superseded_by_id} cleared, attributes
 *     refreshed from X, its own id kept. Only a RETIRED Y is revived. When another rename of another source registered the
 *     same new name between this rename's look at Y and its write, Y is live and the write changes nothing: a named 409
 *     ({@link CatalogRepository.CollectionMergeRefused}) and the transaction, X's retirement included, is gone. Without that,
 *     this rename's X would name an id of this rename's own draw that no row holds, and the winner's live Y would be
 *     overwritten.</li>
 * </ol>
 * The child re-homing of the rename (every table that names the collection) follows, in the caller's transaction, once Y
 * has a row for the foreign keys to point at; nothing in it reads X's liveness.
 */
final class CollectionRename {

    private CollectionRename() {
    }

    /**
     * Retire {@code oldName} and register or revive {@code newName}; adds {@code catalog_collections_superseded} and
     * {@code catalog_collections_inserted} to {@code counts}. {@code tuple} receives Y's tuple, for the caller's race
     * recheck ({@link CollectionMint#recheckOf}).
     *
     * @param newContentType the new row's content type: the caller's, else X's
     * @param newOwnerId     the new row's owner: the caller's, else X's
     */
    static void retireAndRegister(DSLContext ctx, String tenant, String oldName, String newName,
                                  Field<String> newContentType, Field<String> newOwnerId, Map<String, Integer> counts,
                                  String[] tuple) {
        Record4<String, String, String, String> own = ownTuple(ctx, tenant, oldName);
        Record5<String, String, String, String, String> source = successorAttributes(ctx, tenant, oldName, newContentType, newOwnerId);
        if (source != null && own != null && !CollectionMint.tupleIndexExists(ctx)) {
            // X's tuple and Y's: the same key when Y takes X's own. Sorted, once each, as the batch paths lock theirs.
            CollectionTupleLock.lock(ctx, List.of(
                new CollectionTupleLock.Tuple(tenant, own.value1(), own.value2(), own.value3(), own.value4()),
                new CollectionTupleLock.Tuple(tenant, source.value1(), source.value2(), source.value3(), source.value4())));
        }
        long successorId = successorId(ctx, tenant, newName);
        int retired = CollectionTombstones.supersede(ctx, tenant, oldName, newName, successorId);
        if (retired == 0 && own != null) {
            // the CAS matched no row of an X that exists: another writer retired it for a third target while this rename ran
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": " + oldName
                + " was retired for another collection while this one ran; nothing was renamed, so try again");
        }
        counts.put("catalog_collections_superseded", retired);
        if (source != null && own != null && !(source.value1().equals(own.value1()) && source.value2().equals(own.value2()))) {
            checkSuccessorTuple(ctx, tenant, newName, source, tuple);
        }
        int written = writeSuccessor(ctx, tenant, oldName, newName, successorId, newContentType, newOwnerId);
        if (written == 0) {
            throw new CatalogRepository.CollectionMergeRefused("renaming " + oldName + " to " + newName + ": "
                + newName + " was registered by another rename or registration while this one ran, and is live; nothing was "
                + "renamed, so try again");
        }
        counts.put("catalog_collections_inserted", written);
        // RDR-229 P3.1 (R7): Y is the successor that tombstones naming it were waiting for; a revived Y already is, so no row moves
        CollectionTombstones.repointTombstonesNaming(ctx, tenant, java.util.List.of(newName));
    }

    /**
     * Y's id: the id of Y's own tombstone when Y is being revived (it keeps it), else a fresh draw from the sequence. Revival
     * happens only while the name primary key still makes the tombstone's label conflict; once it is gone (RDR-229 G15) the
     * write inserts a new live row, and a row inserted with the tombstone's id would break the registry's id key.
     */
    static long successorId(DSLContext ctx, String tenant, String newName) {
        Long own = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(newName)))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        return own != null && CollectionMint.nameKeyExists(ctx) ? own : CollectionMint.drawId(ctx, tenant);
    }

    /** X's own content type, owner, model and version (null when X has no row), to tell whether Y's tuple is X's. */
    private static Record4<String, String, String, String> ownTuple(DSLContext ctx, String tenant, String oldName) {
        return ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(oldName)))
            .fetchOne();
    }

    /** What Y will hold: content type and owner (the caller's, else X's), X's model and version, X's lifecycle state. */
    private static Record5<String, String, String, String, String> successorAttributes(
            DSLContext ctx, String tenant, String oldName, Field<String> newContentType, Field<String> newOwnerId) {
        return ctx.select(newContentType, newOwnerId, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(oldName))).fetchOne();
    }

    /**
     * Only when Y's tuple is not X's (the caller named another content type or owner): a live collection holding it
     * would be a second one, refused naming both. When Y takes X's own tuple it replaces X there and nothing is checked, so a
     * rename of one member of D4's accepted pair stays possible. {@code tuple} records Y's tuple for the race recheck.
     */
    private static void checkSuccessorTuple(DSLContext ctx, String tenant, String newName,
                                            Record5<String, String, String, String, String> source, String[] tuple) {
        CollectionMint.remember(tuple, newName, source.value1(), source.value2(), source.value3(), source.value4());
        CollectionMint.refuseHeldTuple(ctx, tenant, newName, source.value1(), source.value2(), source.value3(),
            source.value4(), source.value5(), "");
    }

    /**
     * Write Y with {@code successorId}, copying X's attributes (the content type and owner are the caller's when it
     * named them), or revive Y's tombstone. Answers the rows written.
     */
    static int writeSuccessor(DSLContext ctx, String tenant, String oldName, String newName, long successorId,
                              Field<String> newContentType, Field<String> newOwnerId) {
        return ctx.insertInto(CATALOG_COLLECTIONS,
                    CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                    CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                    CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                    CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                    CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                    CATALOG_COLLECTIONS.CREATED_AT)
                .select(ctx.select(
                        CATALOG_COLLECTIONS.TENANT_ID, DSL.val(newName), DSL.val(successorId),
                        newContentType, newOwnerId,
                        CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                        CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                        // dimension and lifecycle_state carry NOT NULL / CHECK constraints (hygiene-002, RDR-204): carried
                        // forward from X like every other attribute, never left unset
                        CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                        // the new row is LIVE by construction, never a copy of the source's tombstone markers (nexus-c29vr)
                        DSL.val("", CATALOG_COLLECTIONS.SUPERSEDED_BY),
                        DSL.val(null, CATALOG_COLLECTIONS.SUPERSEDED_AT),
                        CATALOG_COLLECTIONS.CREATED_AT)
                    .from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(oldName))))
                // UPSERT, not a bare INSERT (nexus-cecqy): the conflict that can reach here is Y's own tombstone from an
                // earlier rename Y->X (a LIVE Y is refused by the caller). Renaming back onto that retired name REVIVES
                // it, which is what the round trip means. The conflicting row keeps its own collection_id.
                // RDR-229 G15 (P3.5c): the arbiter is the LIVE-label name key, WHERE superseded_by = '' AND superseded_by_id IS NULL,
                // so it infers P3.6's live-label index (the predicate implies the index's own); a label held only by a tombstone
                // does not conflict and is written as a new live row. P4.1, which drops superseded_by, drops this half of
                // the predicate. Disposition label-kept: the label is the registry's name.
                .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                .where(CollectionIdReads.liveLabelConflict())
                .doUpdate()
                .set(CATALOG_COLLECTIONS.CONTENT_TYPE,         DSL.excluded(CATALOG_COLLECTIONS.CONTENT_TYPE))
                .set(CATALOG_COLLECTIONS.OWNER_ID,             DSL.excluded(CATALOG_COLLECTIONS.OWNER_ID))
                .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL,      DSL.excluded(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
                .set(CATALOG_COLLECTIONS.MODEL_VERSION,        DSL.excluded(CATALOG_COLLECTIONS.MODEL_VERSION))
                .set(CATALOG_COLLECTIONS.DISPLAY_NAME,         DSL.excluded(CATALOG_COLLECTIONS.DISPLAY_NAME))
                .set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, DSL.excluded(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED))
                .set(CATALOG_COLLECTIONS.DIMENSION,            DSL.excluded(CATALOG_COLLECTIONS.DIMENSION))
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE,      DSL.excluded(CATALOG_COLLECTIONS.LIFECYCLE_STATE))
                // revive: the tombstone markers are cleared, both of them (P3.1's CHECK ties the id to the name)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, (Long) null)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, (java.time.OffsetDateTime) null)
                // only a RETIRED Y is revived: a live one is another rename's (or registration's) and is left alone
                .where(CATALOG_COLLECTIONS.SUPERSEDED_BY.ne(""))
                .execute();
    }

    /**
     * After the children are re-homed and the source URIs are rewritten, still in the rename's transaction: the place
     * where a replaceable legacy owner of {@code newName} (a slug, a dotted or an empty owner) is re-derived from its
     * documents by touching {@code owner_id}, which fires catalog-044's owner trigger. Today it does nothing: catalog-044 has
     * no held-tuple guard on this tree, and X was retired in step 2, before the documents moved, so the trigger that fires
     * at the re-home already sees X as not live. A guard that keeps a legacy owner off a held live tuple (marlowe's
     * catalog-051) re-derives here once it lands, and must keep that skip.
     */
    static void rederiveReplaceableOwner(DSLContext ctx, String tenant, String newName) {
        // intentionally empty; see above
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.CatalogRepository.CollectionScopedTable;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * Which rows a collection re-home reads and moves in each content table (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's ruling
 * R-B: a re-home stays keyed by id, RDR-229 section "what this replaces").
 *
 * <p>Before gate 3 every reader and writer of a re-home is scoped by the name columns, as it always was. From gate 3 the source
 * and target labels resolve to their registry rows first (the live row first, else the newest tombstone:
 * {@link CollectionRegistry#labelResolutionOrder()}) and every content table is scoped by the resolved {@code collection_id}:
 * after a one-row rename the content's name columns still hold the old label, so a name would match no row (a re-home naming the
 * new label answering 200 "moved 0") or another row's. A label with no registry row has no id to scope by, so it matches nothing,
 * the same empty answer an unknown label gets.
 *
 * <p>Built once per re-home attempt (and per status read) from the repository's {@code Gate3} read at that moment, so the switch
 * needs no restart. What the moved rows are written with is unchanged: the target's label in the name columns, the target's id in
 * the id columns, the note URIs rewritten label to label (a one-row rename rewrites them, so they name the current label).
 */
final class CollectionRehomeScope {

    private final String source;
    private final String target;
    private final boolean byId;
    private final Long sourceId;
    private final Long targetId;

    private CollectionRehomeScope(String source, String target, boolean byId, Long sourceId, Long targetId) {
        this.source = source;
        this.target = target;
        this.byId = byId;
        this.sourceId = sourceId;
        this.targetId = targetId;
    }

    /** The scope of one attempt: the name columns while {@code gate3Holds} is false; from gate 3 the resolved ids ({@code target} may be null: a status read). */
    static CollectionRehomeScope resolve(DSLContext ctx, boolean gate3Holds, String tenant, String source, String target) {
        if (!gate3Holds) {
            return new CollectionRehomeScope(source, target, false, null, null);
        }
        return new CollectionRehomeScope(source, target, true,
            CollectionPurgeScope.idOfLabel(ctx, tenant, source),
            target == null ? null : CollectionPurgeScope.idOfLabel(ctx, tenant, target));
    }

    String source() { return source; }

    String target() { return target; }

    /** True from gate 3 when the source label has no registry row, so the scope matches nothing at the source. */
    boolean sourceMatchesNothing() { return byId && sourceId == null; }

    /** The id the moved rows take: the target's resolved id from gate 3, else read by name as before. */
    Long targetId(DSLContext ctx, String tenant) {
        return byId ? targetId : CollectionIds.require(ctx, tenant, target);
    }

    private static Condition of(Field<String> nameColumn, Field<Long> idColumn, boolean byId, String name, Long id) {
        if (!byId) {
            return nameColumn.eq(name);
        }
        return id == null ? DSL.falseCondition() : idColumn.eq(id);
    }

    Condition atSource(Field<String> nameColumn, Field<Long> idColumn) {
        return of(nameColumn, idColumn, byId, source, sourceId);
    }

    /** The rows of {@code t} at the source. */
    Condition atSource(CollectionScopedTable t) {
        return atSource(t.collection(), t.collectionId());
    }

    Condition sourceChunks() {
        return atSource(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID);
    }

    Condition sourceDocuments() {
        return atSource(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }

    /** The embedding model of the source's registry row (the one the scope resolved, from gate 3), else of the row named {@code source}. */
    String sourceModel(DSLContext ctx, String tenant) {
        return model(ctx, tenant, source, sourceId);
    }

    String targetModel(DSLContext ctx, String tenant) {
        return model(ctx, tenant, target, targetId);
    }

    private String model(DSLContext ctx, String tenant, String name, Long id) {
        return ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(byId ? CATALOG_COLLECTIONS.COLLECTION_ID.eq(id) : CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
    }

    /**
     * RDR-231: refuse when a note URI of the source's own documents, rewritten label to label by {@link NoteUri#moveTo}, a
     * different live document already holds. Only the source's own documents move (home collection = the source).
     */
    void requireUrisFree(DSLContext ctx, String tenant, String operation) {
        var hit = CatalogRepository.moveUriCollisions(ctx, tenant, source, target,
            m -> of(m.PHYSICAL_COLLECTION, m.PHYSICAL_COLLECTION_ID, byId, source, sourceId)
                .and(NoteUri.namesCollection(m.SOURCE_URI, source)))
            .stream().findFirst().orElse(null);
        if (hit != null) {
            throw new CatalogRepository.SourceUriCollision(operation, hit.value2(), hit.value3(), hit.value4());
        }
    }

    /**
     * The WHERE that selects rows at the source this operation CAN move: everything at the source except rows whose move would
     * collide with a row already at the target. The collision predicate is DERIVED from the table's own unique keys: for each key
     * that contains the collection column, a row is excluded when the target already holds a row agreeing on every OTHER column of
     * that key; "at the target" is the target's name from before gate 3 and its id from it. A table with no collection-bearing
     * key cannot collide.
     *
     * <p>Shared by the mover and the status read on purpose: if they disagreed about what is movable, {@code done()} would
     * either never fire or fire early, and both failures look like a correct 200 to a caller.
     */
    @SuppressWarnings({"unchecked", "rawtypes"})
    Condition movable(DSLContext ctx, CollectionScopedTable t) {
        Condition where = atSource(t);
        int aliasSeq = 0;
        for (org.jooq.UniqueKey<?> key : t.table().getKeys()) {
            boolean keyed = key.getFields().stream()
                .anyMatch(f -> f.getName().equals(t.collection().getName()));
            if (!keyed) {
                continue;
            }
            Table<?> peer = t.table().as("rehome_peer_" + (aliasSeq++));
            Condition match = of((Field<String>) peer.field(t.collection().getName()),
                (Field<Long>) peer.field(t.collectionId().getName()), byId, target, targetId);
            for (Field<?> f : key.getFields()) {
                if (f.getName().equals(t.collection().getName()) || f.getName().equals(t.collectionId().getName())) {
                    continue;
                }
                match = match.and(((Field) peer.field(f.getName()))
                    .eq((Field) t.table().field(f.getName())));
            }
            where = where.and(DSL.notExists(ctx.selectOne().from(peer).where(match)));
        }
        return where;
    }
}

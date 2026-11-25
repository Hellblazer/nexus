// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * Which rows a collection delete removes from each content table (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's ruling of
 * 2026-11-23, option (b) for the delete of a retired label).
 *
 * <p>Before gate 3 a delete is scoped by the name columns, as it always was. From gate 3 a delete resolves the label to its
 * registry row first and removes only the rows that carry that row's {@code collection_id}, then the row: a name column can
 * hold a label that another row (one a one-row rename moved on) now answers to, so the name would reach content that is not the
 * deleted collection's. A label with no registry row has no id to scope by, so it removes nothing from any content table (the
 * same empty answer an unknown label gets), whatever the name columns say.
 *
 * <p>Built once per delete attempt, from the repository's {@code Gate3} read at that moment, so the switch needs no
 * restart.
 */
final class CollectionPurgeScope {

    private final String name;
    private final boolean byId;
    private final Long id;

    private CollectionPurgeScope(String name, boolean byId, Long id) {
        this.name = name;
        this.byId = byId;
        this.id = id;
    }

    /** Before gate 3: the name columns. */
    static CollectionPurgeScope byName(String name) {
        return new CollectionPurgeScope(name, false, null);
    }

    /** From gate 3: the id columns, of {@code id} ({@code null}: no registry row, so no row matches). */
    static CollectionPurgeScope byId(String name, Long id) {
        return new CollectionPurgeScope(name, true, id);
    }

    /**
     * The scope of one delete attempt: the name columns while {@code gate3Holds} is false; from gate 3 the id of the row the
     * delete acts on, which is {@code onlyId} when the request named the row by its id, else the registry row of {@code name}
     * (the live one first, else the newest tombstone: {@link CollectionRegistry#labelResolutionOrder()}).
     */
    static CollectionPurgeScope resolve(DSLContext ctx, boolean gate3Holds, String tenant, String name, Long onlyId) {
        if (!gate3Holds) {
            return byName(name);
        }
        if (onlyId != null) {
            return byId(name, onlyId);
        }
        return byId(name, idOfLabel(ctx, tenant, name));
    }

    /** The id of the registry row {@code name} resolves to (the live one first, else the newest tombstone), or {@code null}: no row. */
    static Long idOfLabel(DSLContext ctx, String tenant, String name) {
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .orderBy(CollectionRegistry.labelResolutionOrder())
            .limit(1)
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    /** True when this delete has no id to scope by and so removes nothing. */
    boolean matchesNothing() {
        return byId && id == null;
    }

    boolean scopedById() {
        return byId;
    }

    Long id() {
        return id;
    }

    private Condition of(org.jooq.Field<String> nameColumn, org.jooq.Field<Long> idColumn) {
        if (!byId) {
            return nameColumn.eq(name);
        }
        return id == null ? DSL.falseCondition() : idColumn.eq(id);
    }

    Condition topicAssignments() {
        return of(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID);
    }

    Condition chunks() {
        return of(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID);
    }

    Condition manifest() {
        return of(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
    }

    Condition topics() {
        return of(TOPICS.COLLECTION, TOPICS.COLLECTION_ID);
    }

    Condition taxonomyMeta() {
        return of(TAXONOMY_META.COLLECTION, TAXONOMY_META.COLLECTION_ID);
    }

    Condition centroids() {
        return of(TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.COLLECTION_ID);
    }

    Condition aspects() {
        return of(DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.COLLECTION_ID);
    }

    Condition highlights() {
        return of(DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID);
    }

    Condition documents() {
        return of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }
}

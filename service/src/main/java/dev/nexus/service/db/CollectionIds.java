// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.CatalogCollections;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the one way a writer turns {@code (tenant, collection name)} into the registry's
 * {@code collection_id}, so it can set the id beside the name on every insert and update (the dual write).
 *
 * <p><b>The id is the registry's id at write time.</b> Both shapes read {@code nexus.catalog_collections} from the
 * statement or transaction that is writing, so a row the same transaction just inserted resolves before it commits
 * (a rename inserts the new registry row and stamps every rewritten row with that row's id), and a row a rollback
 * removed does not resolve afterwards. There is no cache here on purpose: {@link CollectionRegistry}'s cache is
 * process-wide and survives a rollback, which is right for "may this write skip registration" and wrong for "what
 * id does the registry hold right now".
 *
 * <p><b>The empty name.</b> {@code ''} (and {@code null}) means unset in every P3.1 CHECK
 * ({@code (NULLIF(name, '') IS NULL) = (id IS NULL)}), so both shapes give NULL for it without reading the registry,
 * even if a registry row is literally named {@code ''}.
 *
 * <p><b>Which row.</b> The LIVE registry row of the name first ({@code superseded_by = '' AND superseded_by_id IS NULL}, the
 * predicate of P3.6's live-label unique index), else the newest tombstone: {@code ORDER BY (live) DESC, collection_id DESC
 * LIMIT 1} (linda's corrected Q3: no resolver filters strictly to live). Once a tombstone and a live row can share a label a bare
 * {@code WHERE name = ?} returns two rows and the scalar subselect errors (21000); the ordering picks one. A label only
 * tombstones carry still names its own (newest) retired row, so the writers that legitimately file rows under a retired
 * collection (the fidelity imports, the T2 rename verbs, an own-name stamp of a row that stayed in a superseded collection) keep
 * working. A superseded or quarantine row has an id too; a write to one is decided by the writer's own checks, not here.
 *
 * <p><b>Two shapes.</b>
 * <ul>
 *   <li>{@link #idOf(String, String)} and {@link #idOf(Field, Field)}: a scalar subselect to use as a value in the
 *       statement, {@code .set(T.COLLECTION_ID, idOf(tenant, name))}. It is NULL for an unregistered name, and the
 *       P3.1 CHECK then refuses the write with 23514, so use it where the writer already guarantees the
 *       registration (it checked or inserted the row in the same transaction).</li>
 *   <li>{@link #require}: an explicit lookup that refuses an unregistered name with
 *       {@link UnregisteredCollectionException} (the typed 422 every writer already raises) before anything is
 *       written; use it where the writer wants the refusal to name the collection, or needs the id in Java.</li>
 * </ul>
 */
public final class CollectionIds {

    private CollectionIds() {}

    /**
     * A scalar subselect giving the registry's {@code collection_id} for {@code (tenant, name)}; NULL when the name
     * is {@code ''}, {@code null} or unregistered. Needs the registry row visible to the writing session (a tenant
     * scope's RLS applies, as to every other read of the registry).
     */
    public static Field<Long> idOf(String tenant, String name) {
        Objects.requireNonNull(tenant, "tenant");
        if (name == null || name.isEmpty()) {
            return DSL.castNull(Long.class);
        }
        return idOf(DSL.val(tenant), DSL.val(name));
    }

    /**
     * The same subselect over columns or expressions, for a statement that resolves many names at once
     * ({@code INSERT ... SELECT}, a correlated update): {@code idOf(t.TENANT_ID, t.COLLECTION)}. An {@code ''} or
     * NULL name gives NULL, as the string shape does.
     */
    public static Field<Long> idOf(Field<String> tenant, Field<String> name) {
        var c = CATALOG_COLLECTIONS.as("cid");
        return DSL.when(name.isNull().or(name.eq("")), DSL.castNull(Long.class))
            .otherwise(DSL.field(DSL.select(c.COLLECTION_ID).from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.NAME.eq(name))
                .orderBy(DSL.field(live(c)).desc(), c.COLLECTION_ID.desc()).limit(1)));
    }

    /** The registry row is live: the predicate of P3.6's live-label partial unique index, so it can infer that index. */
    public static Condition live(CatalogCollections c) {
        return c.SUPERSEDED_BY.eq("").and(c.SUPERSEDED_BY_ID.isNull());
    }

    /**
     * The registry's {@code collection_id} for {@code (tenant, name)}, read now in {@code ctx}'s transaction;
     * {@code null} for {@code ''} or {@code null} (unset, no read). Never cached.
     *
     * @throws UnregisteredCollectionException when the name is not empty and has no registry row for the tenant
     */
    public static Long require(DSLContext ctx, String tenant, String name) {
        Objects.requireNonNull(tenant, "tenant");
        if (name == null || name.isEmpty()) {
            return null;
        }
        Long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .orderBy(DSL.field(live(CATALOG_COLLECTIONS)).desc(), CATALOG_COLLECTIONS.COLLECTION_ID.desc()).limit(1)
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        if (id == null) {
            throw new UnregisteredCollectionException(tenant, name);
        }
        return id;
    }
}

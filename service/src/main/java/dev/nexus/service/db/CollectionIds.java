// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.Objects;
import java.util.OptionalLong;

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
 * <p><b>Which row.</b> The registry row of the name whatever its lifecycle: a superseded or quarantine row has an id
 * too, and a write to one is decided by the writer's own checks, not here.
 *
 * <p><b>The write transaction's registry check.</b> {@link #require} reads the row {@code FOR KEY SHARE} through
 * {@link CollectionRegistry#requireForWrite}, once per transaction, for a label and an id alike (RDR-229 P3.5a): it is the check
 * every writer runs before any child row, and it makes a verb's {@code FOR UPDATE} wait for the writer. {@link #stampHistory} is its
 * unlocked twin for history rows.
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
        long requested = ExpectedCollectionIds.of(name).orElse(0L);
        if (requested > 0) {
            // the request named this label by id: the statement names the id too, so a row whose label is no longer the id's
            // gets NULL, which the P3.1 CHECK refuses. It is a stamp and not a read of the registry row: no lock and no consumption
            // (the write that must be live at commit calls require, which reads the id FOR KEY SHARE)
            var c = CATALOG_COLLECTIONS.as("cid");
            return DSL.field(DSL.select(c.COLLECTION_ID).from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION_ID.eq(requested)).and(c.NAME.eq(name)));
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
                .where(c.TENANT_ID.eq(tenant)).and(c.NAME.eq(name))));
    }

    /**
     * The registry's {@code collection_id} for {@code (tenant, name)}, read now in {@code ctx}'s transaction;
     * {@code null} for {@code ''} or {@code null} (unset, no read). Never cached. For a name the request named by
     * {@code collection_id} ({@link ExpectedCollectionIds}) it is the row of THAT id, read {@code FOR KEY SHARE}: no row is
     * {@link UnknownCollectionIdException}, a retired row {@link CollectionRetiredIdException}, another label
     * {@link CollectionRenamedUnderRequestException}.
     *
     * @throws UnregisteredCollectionException when the name is not empty and has no registry row for the tenant
     */
    public static Long require(DSLContext ctx, String tenant, String name) {
        Objects.requireNonNull(tenant, "tenant");
        if (name == null || name.isEmpty()) {
            return null;
        }
        // the write transaction's registry check (RDR-229 P3.5a, linda's option (b)): the row FOR KEY SHARE, once per transaction,
        // the row of the named id for a request by id and the row the label resolves to for a request by label
        return CollectionRegistry.requireForWrite(ctx, tenant, name).collectionId();
    }

    /**
     * The registry's {@code collection_id} for {@code (tenant, name)} to STAMP a history row (a relevance log row, a hook failure:
     * a row about a collection that decides nothing), read in {@code ctx}'s transaction WITHOUT a lock and never refusing: the live row
     * first and the newest tombstone after it ({@link CollectionRegistry#labelResolutionOrder}); {@code null} for {@code ''},
     * {@code null} or a name no row has, which the caller writes as NULL beside the label (history tables carry no CHECK). A request
     * that named the label by id stamps THAT id when the row of the id still has the label. Marks the transaction a history write for
     * the census ({@link ExpectedCollectionIds#historyWrite}). History writes are exempt from {@code FOR KEY SHARE} and from refusal:
     * a history write during a parked supersede does not wait for it.
     */
    public static Long stampHistory(DSLContext ctx, String tenant, String name) {
        Objects.requireNonNull(tenant, "tenant");
        if (name == null || name.isEmpty()) {
            return null;
        }
        ExpectedCollectionIds.historyWrite(ctx, name);
        OptionalLong requested = ExpectedCollectionIds.of(name);
        var read = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name));
        Long id = requested.isPresent()
            ? read.and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(requested.getAsLong())).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID)
            : read.orderBy(CollectionRegistry.labelResolutionOrder()).limit(1).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        if (id == null) {
            org.slf4j.LoggerFactory.getLogger(CollectionIds.class).debug(
                "event=history_row_without_registry_id tenant={} collection={} (written with a NULL id beside the label)", tenant, name);
        }
        return id;
    }
}

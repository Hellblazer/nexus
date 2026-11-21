// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Name;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultDataType;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * Typed jOOQ helpers of the RDR-229 P3.8 mint tests ({@code CollectionMint*Test}): the partial UNIQUE index of the live
 * tuple, the {@code collection_id} sequence and rows written the way a superuser (a restore, a fixture) writes them. No
 * raw SQL: {@code RawSqlGateTest} ratchets the test tree.
 */
final class CollectionMintSupport {

    static final String INDEX = "catalog_collections_live_tuple_uq";

    private CollectionMintSupport() {
    }

    private static Field<String> sequenceText() {
        return DSL.function("pg_get_serial_sequence", String.class, DSL.val("nexus.catalog_collections"), DSL.val("collection_id"));
    }

    private static Field<Object> sequence() {
        return sequenceText().cast(new DefaultDataType<>(SQLDialect.POSTGRES, Object.class, "regclass"));
    }

    /** The value the {@code collection_id} sequence hands out next. */
    static long nextSequenceValue(DSLContext ctx) {
        Long last = ctx.fetchValue(DSL.function("pg_sequence_last_value", Long.class, sequence()));
        return last == null ? 1L : last + 1;
    }

    static void dropIndex(DSLContext ctx) {
        var drop = ctx.dropIndexIfExists(DSL.name("nexus", INDEX));
        drop.execute();
        if (indexPresent(ctx)) {
            throw new IllegalStateException("the index is still there after: " + drop.getSQL() + " (autocommit "
                + ctx.connectionResult(c -> c.getAutoCommit()) + ")");
        }
    }

    /** The partial UNIQUE index of changeset collections-001-2, built by hand (the changeset waits for the re-own move). */
    static void createIndex(DSLContext ctx) {
        ctx.createUniqueIndexIfNotExists(DSL.name(INDEX))
            .on(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.CONTENT_TYPE,
                CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
            .where(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("").and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.ne("quarantine")))
            .execute();
    }

    static boolean indexPresent(DSLContext ctx) {
        return ctx.fetchExists(ctx.selectOne().where(
            DSL.function("to_regclass", Object.class, DSL.val("nexus." + INDEX)).isNotNull()));
    }

    /**
     * A live {@code knowledge} row, as a superuser writes it. With {@code collectionId} null the sequence assigns the id;
     * otherwise the id is explicit (a restore without a {@code setval}).
     */
    static void insertLiveRow(DSLContext ctx, String tenant, String name, String owner, String model, Long collectionId) {
        var insert = ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, tenant)
            .set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge")
            .set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model)
            .set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live");
        if (collectionId != null) {
            insert.set(CATALOG_COLLECTIONS.COLLECTION_ID, collectionId);
        }
        insert.execute();
    }

    static void deleteRow(DSLContext ctx, String tenant, String name) {
        ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    /**
     * Make the sequence cycle over {@code [1, max(collection_id)]} and stand at the max, so every draw after the next one
     * wraps to 1: a row {@code tenant} holds under id 1 is then taken on the first draw AND on the draw after a heal (a
     * heal sets the sequence to the largest id, and the next draw wraps again). Ensures that row exists.
     */
    static void cycleOverTheRows(DSLContext ctx, String tenant, String anchorName, String model) {
        ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(1L)).execute();
        insertLiveRow(ctx, tenant, anchorName, anchorName, model, 1L);
        long max = ctx.select(DSL.max(CATALOG_COLLECTIONS.COLLECTION_ID)).from(CATALOG_COLLECTIONS).fetchOne(0, Long.class);
        ctx.select(DSL.function("setval", Long.class, sequence(), DSL.val(max))).fetch();
        Name sequenceName = DSL.name(ctx.select(sequenceText()).fetchOne(0, String.class).split("\\."));
        ctx.alterSequence(DSL.sequence(sequenceName, Long.class)).maxvalue(max).cycle().execute();
    }

    /** Whether {@code role} holds UPDATE on the {@code collection_id} sequence. */
    static boolean holdsUpdateOnSequence(DSLContext ctx, String role) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("has_sequence_privilege", Boolean.class,
            DSL.val(role), sequenceText(), DSL.val("UPDATE"))).fetchOne(0, Boolean.class));
    }
}

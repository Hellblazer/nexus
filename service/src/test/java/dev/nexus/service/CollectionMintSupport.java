// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Condition;
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
public final class CollectionMintSupport {

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

    public static void dropIndex(DSLContext ctx) {
        // jOOQ renders DROP INDEX with the unqualified index name even when it is given a qualified one, so the name must
        // resolve against this (single-use) connection's schema, or "if exists" quietly drops nothing
        ctx.setSchema("nexus").execute();
        var drop = ctx.dropIndexIfExists(DSL.name(INDEX));
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

    /**
     * A unique index of the live tuple's own name over the tuple columns but with another predicate, as an operator or an
     * older build could leave: the mint's {@code ON CONFLICT ... WHERE} cannot be inferred onto it (42P10) when the predicate
     * is not implied by the mint's own.
     */
    static void createIndexWhere(DSLContext ctx, Condition predicate) {
        ctx.createUniqueIndexIfNotExists(DSL.name(INDEX))
            .on(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.CONTENT_TYPE,
                CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
            .where(predicate)
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

    /**
     * A live {@code code} row with an explicit creation stamp, as a superuser writes it. The tie-break tests of
     * {@code CatalogRepositoryTest} model D4's accepted duplicate (two live rows of one tuple), which a named upsert now
     * refuses (RDR-229 P3.8), so they cannot register the second row through the repository.
     */
    static void insertCodeRowAt(DSLContext ctx, String tenant, String name, String owner, String model, String version,
                                String createdAt, boolean legacyGrandfathered) {
        ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, tenant).set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "code").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model).set(CATALOG_COLLECTIONS.MODEL_VERSION, version)
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live").set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, legacyGrandfathered)
            .set(CATALOG_COLLECTIONS.CREATED_AT, java.time.OffsetDateTime.parse(createdAt)).execute();
    }

    static void deleteRow(DSLContext ctx, String tenant, String name) {
        ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    /**
     * Make the sequence cycle over {@code [1, max(collection_id)]} and stand at the max, so every draw after the next one
     * wraps to 1: a row {@code tenant} holds under id 1 is then taken on the first draw AND on the draw after a heal (a
     * heal draws forward to the largest id, and the next draw wraps again). Ensures that row exists.
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

    /** The signature of the definer function that heals the sequence (changeset collections-002). */
    static final String HEAL_FUNCTION = "nexus.collection_id_seq_heal(bigint)";

    /** Whether {@code role} may EXECUTE the heal function (an error when the function does not exist). */
    static boolean holdsExecuteOnHeal(DSLContext ctx, String role) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("has_function_privilege", Boolean.class,
            DSL.val(role), DSL.val(HEAL_FUNCTION), DSL.val("EXECUTE"))).fetchOne(0, Boolean.class));
    }

    /** The heal function's own definition text: its SECURITY DEFINER and its pinned search_path are read from it. */
    static String healFunctionDefinition(DSLContext ctx) {
        return functionDefinition(ctx, HEAL_FUNCTION);
    }

    /** The signature of the definer function that answers whether any tenant holds a collection_id (collections-002). */
    static final String TAKEN_FUNCTION = "nexus.collection_id_taken(bigint)";

    static String functionDefinition(DSLContext ctx, String signature) {
        return ctx.select(DSL.function("pg_get_functiondef", String.class,
            DSL.function("to_regprocedure", Object.class, DSL.val(signature)))).fetchOne(0, String.class);
    }

    /** Whether {@code role} (or {@code public}, the pseudo-role) may EXECUTE the function (an error when it does not exist). */
    static boolean holdsExecute(DSLContext ctx, String role, String signature) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("has_function_privilege", Boolean.class,
            DSL.val(role), DSL.val(signature), DSL.val("EXECUTE"))).fetchOne(0, Boolean.class));
    }

    /** {@code nexus.collection_id_taken(id)} as the connection's own role. */
    static boolean callTaken(DSLContext ctx, long id) {
        return Boolean.TRUE.equals(ctx.select(DSL.function(DSL.name("nexus", "collection_id_taken"), Boolean.class,
            DSL.val(id, org.jooq.impl.SQLDataType.BIGINT))).fetchOne(0, Boolean.class));
    }

    /** Whether some session is blocked waiting for a lock: a statement that is held by another open transaction. */
    static boolean someSessionWaitsOnALock(DSLContext ctx) {
        return ctx.fetchExists(ctx.selectOne().from(DSL.table(DSL.name("pg_stat_activity")))
            .where(DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")));
    }

    /**
     * A superseded row, as a superuser writes it: {@code superseded_by} names its successor and P3.1's
     * {@code catalog_collections_superseded_by_id_check} wants a {@code superseded_by_id} beside it (any value: these
     * tests judge the tuple, not the successor's id).
     */
    static void insertTombstone(DSLContext ctx, String tenant, String name, String owner, String model, String supersededBy) {
        ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, tenant).set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live")
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, 1L)
            .execute();
    }

    /** Call the heal function as the connection's own role: the sequence's last value afterwards (or null). */
    static Long callHeal(DSLContext ctx, Long floor) {
        return ctx.select(DSL.function(DSL.name("nexus", "collection_id_seq_heal"), Long.class, DSL.val(floor, org.jooq.impl.SQLDataType.BIGINT)))
            .fetchOne(0, Long.class);
    }

    /** {@code n} draws of the {@code collection_id} sequence in ONE statement (the draws a stream of mints would make), in order. */
    static long[] drawIds(DSLContext ctx, int n) {
        return ctx.select(DSL.function("nextval", Long.class, sequence())).from(DSL.generateSeries(1, n))
            .fetch().stream().mapToLong(r -> r.value1()).toArray();
    }

    /**
     * {@code n} calls of the heal function in ONE statement, each with a floor of one more than the sequence's value at
     * that instant: the floor is read microseconds before the function reads the sequence itself, which is the interleaving a
     * concurrent drawer has to cross. Answers how many calls ran.
     */
    static int healAtTheEdge(DSLContext ctx, int n) {
        Field<Long> floor = DSL.function("pg_sequence_last_value", Long.class, sequence()).plus(1L);
        return ctx.select(DSL.function(DSL.name("nexus", "collection_id_seq_heal"), Long.class, floor))
            .from(DSL.generateSeries(1, n)).fetch().size();
    }

    /** A live row whose id the column's DEFAULT identity assigns, as a mint or a plain insert gets it: the assigned id. */
    static long insertWithDefaultId(DSLContext ctx, String tenant, String name, String owner, String model) {
        return ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, tenant).set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live")
            .returning(CATALOG_COLLECTIONS.COLLECTION_ID).fetchOne().getCollectionId();
    }

    /** Whether {@code role} holds UPDATE on the {@code collection_id} sequence. */
    static boolean holdsUpdateOnSequence(DSLContext ctx, String role) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("has_sequence_privilege", Boolean.class,
            DSL.val(role), sequenceText(), DSL.val("UPDATE"))).fetchOne(0, Boolean.class));
    }
}

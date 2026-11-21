// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.TreeSet;

/**
 * The per-tuple advisory lock of RDR-229: a collection's identity tuple is
 * {@code (tenant_id, content_type, owner_id, embedding_model, model_version)}, and a writer that must not race another
 * writer onto the same tuple (the catalog-051-2 owner triggers, and the P3.8 mint when no unique index backs the tuple)
 * takes {@code pg_advisory_xact_lock} on that tuple's key, held to the end of its transaction.
 *
 * <p><b>One key derivation.</b> The key is {@code nexus.collection_tuple_lock_key(...)}, the SQL function of changelog
 * catalog-051-1a, and nothing else computes it. The two triggers call the function; this class SELECTs it, so Java never
 * re-implements the encoding or the hash. Two writers on one tuple therefore always take the same lock and wait for each
 * other.
 *
 * <p><b>One order.</b> A caller that takes SEVERAL tuple locks in one transaction MUST take them through
 * {@link #lock(DSLContext, Collection)}, never one {@code pg_advisory_xact_lock} after another in its own order: that
 * method SELECTs every key in one round trip, sorts the keys ascending by the {@code bigint} the SQL function returns,
 * drops duplicates and locks them in that order, so two such callers can never each hold a lock the other waits for.
 * {@link #lockTuple} is the one-tuple case of the same order. A trigger takes only the one lock of its own row.
 *
 * <p><b>The five values</b> are the row's own: {@code model_version} is the column's text as stored ({@code 'v1'} and so
 * on). Nothing here normalises, trims or lower-cases; the SQL function encodes them as given.
 *
 * <p>The locks are transaction-scoped: {@code ctx} must be inside the transaction whose end should release them.
 */
public final class CollectionTupleLock {

    /** One collection tuple. */
    public record Tuple(String tenantId, String contentType, String ownerId, String embeddingModel, String modelVersion) {}

    private CollectionTupleLock() {}

    /** The advisory-lock key of {@code tuple}, as {@code nexus.collection_tuple_lock_key} computes it. */
    public static long key(DSLContext ctx, Tuple tuple) {
        return keys(ctx, List.of(tuple)).get(0);
    }

    /** Take {@code pg_advisory_xact_lock} for one tuple. @return its key */
    public static long lockTuple(DSLContext ctx, Tuple tuple) {
        return lock(ctx, List.of(tuple)).get(0);
    }

    /**
     * Take {@code pg_advisory_xact_lock} for each tuple: every key SELECTed in one round trip, sorted ascending, each key
     * once, locked in that order.
     *
     * @return the keys in the order they were locked (ascending, no duplicates); empty when {@code tuples} is empty
     */
    public static List<Long> lock(DSLContext ctx, Collection<Tuple> tuples) {
        if (tuples.isEmpty()) {
            return List.of();
        }
        List<Long> locked = new ArrayList<>(new TreeSet<>(keys(ctx, tuples)));
        for (long k : locked) {
            ctx.select(DSL.function("pg_advisory_xact_lock", SQLDataType.VARCHAR, DSL.val(k))).fetch();
        }
        return locked;
    }

    /** The key of each tuple, in argument order, in one statement (a VALUES list joined to the SQL function). */
    private static List<Long> keys(DSLContext ctx, Collection<Tuple> tuples) {
        List<Tuple> in = List.copyOf(tuples);
        List<Field<?>> keyColumns = new ArrayList<>(in.size());
        for (Tuple t : in) {
            keyColumns.add(DSL.function(DSL.name("nexus", "collection_tuple_lock_key"), SQLDataType.BIGINT,
                DSL.val(t.tenantId()), DSL.val(t.contentType()), DSL.val(t.ownerId()),
                DSL.val(t.embeddingModel()), DSL.val(t.modelVersion())));
        }
        org.jooq.Record row = ctx.select(keyColumns).fetchOne();
        List<Long> keys = new ArrayList<>(in.size());
        for (int i = 0; i < in.size(); i++) {
            Long k = row.get(i, Long.class);
            if (k == null) {
                throw new IllegalStateException("nexus.collection_tuple_lock_key returned NULL for " + in.get(i));
            }
            keys.add(k);
        }
        return keys;
    }
}

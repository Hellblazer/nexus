// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.OptionalLong;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.jooq.impl.DSL.function;
import static org.jooq.impl.DSL.val;

/**
 * Serializes the taxonomy persist operations of one collection within the current transaction (nexus-n2ls1), keyed by the
 * collection's registry id (RDR-229 P3.5a, linda's ruling), and takes the writer's registry check right after it. Moved out of
 * {@code TaxonomyRepository} (at its size ceiling) as the area this change rewrites.
 *
 * <p><b>Why the lock exists.</b> The persist paths' existing-topics guard is a plain unlocked SELECT COUNT: two concurrent persists for
 * the same collection both counted 0 under READ COMMITTED, both inserted the same root label, and the loser hit the taxonomy-004 partial
 * unique index (SQLSTATE 23505, HTTP 409, observed live 2026-07-07). {@code pg_advisory_xact_lock} blocks the second persist until the
 * first commits, so the loser then sees the winner's committed rows and takes the guard-skip (discover) or a fresh replace (rebuild).
 * Released at commit or rollback. A {@code hashtext} collision between two different keys merely over-serializes.
 *
 * <p><b>Why it is keyed by the id.</b> A label can name a different row after a delete and a re-registration, and a request can name the
 * collection by id or by label; two requests that mean one collection must meet on one key. The key is
 * {@code tenant/#<collection_id>}: the id the request named, else the id the label resolves to by an UNLOCKED read (the live row first,
 * then the newest tombstone: {@link CollectionRegistry#labelResolutionOrder}), taken before the lock because the lock is advisory and
 * must precede every row lock (the lock order of {@link RenameGuard#runRename}: advisory locks, then the registry {@code FOR KEY SHARE},
 * then child rows). A label no row has keys by {@code tenant/<label>}, and the registry check then answers the typed 422.
 *
 * <p><b>The race the unlocked read leaves, and what closes it.</b> If the label is freed and taken again between that read and the
 * registry check, the row the check reads is another row than the key named. The method then takes the lock of that row's id too, so
 * the persist is serialized with the writers of the row it will write under (two advisory locks in one transaction, taken in an order
 * only this rare race produces).
 */
final class TaxonomyCollectionLock {

    private TaxonomyCollectionLock() {
    }

    /**
     * Takes the collection's taxonomy lock, then the writer's registry check ({@link CollectionRegistry#requireWritable}, which refuses
     * a retired name 409 and an unregistered one 422), and returns the row. A blank collection takes the label key and skips the check,
     * as the writers always did.
     */
    static CollectionRow lockAndRequireWritable(DSLContext ctx, String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            lock(ctx, tenant + "/" + collection);
            return null;
        }
        WriteRegistryMemo.assertNoneHeld(ctx, "the taxonomy lock of " + collection);   // the second lock below is the stated exception
        OptionalLong named = ExpectedCollectionIds.of(collection);
        Long keyed = named.isPresent() ? Long.valueOf(named.getAsLong()) : unlockedId(ctx, tenant, collection);
        lock(ctx, keyed == null ? tenant + "/" + collection : key(tenant, keyed));
        CollectionRow row = CollectionRegistry.requireWritable(ctx, tenant, collection);
        if (keyed == null || keyed != row.collectionId()) {
            lock(ctx, key(tenant, row.collectionId()));
        }
        return row;
    }

    /** The label to write under: the locked row's CURRENT label, else {@code asked} (a blank collection has no row). */
    static String currentLabel(CollectionRow row, String asked) {
        return row == null ? asked : row.label();
    }

    private static String key(String tenant, long collectionId) {
        return tenant + "/#" + collectionId;
    }

    /** The id the label resolves to now, read without a lock; null when no row has the label. */
    private static Long unlockedId(DSLContext ctx, String tenant, String collection) {
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .orderBy(CollectionRegistry.labelResolutionOrder()).limit(1)
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private static void lock(DSLContext ctx, String key) {
        // Bounded wait FIRST (critique, nexus-n2ls1): without a lock_timeout a stuck holder turns the fast retryable 409 this removes
        // into an indefinite in-transaction wait surfacing as an edge 504. set_config(..., true) is SET LOCAL (transaction-scoped); a
        // timed-out acquire raises SQLSTATE 55P03, a clean retryable error. The bound is for THIS advisory lock only: it is put back
        // as soon as the lock is held, so the registry key share and the child-row waits that follow have none (a writer's wait has no
        // timeout, linda's ruling; the verb's wait is the bounded one).
        String previous = ctx.select(function("current_setting", String.class, val("lock_timeout"))).fetchOne(0, String.class);
        ctx.select(function("set_config", String.class, val("lock_timeout"), val("5000"), val(true))).fetch();
        // Typed-DSL function composition (house rule: no raw string SQL, RawSqlGateTest): SELECT pg_advisory_xact_lock(hashtext(:key)).
        ctx.select(function("pg_advisory_xact_lock", Object.class, function("hashtext", Integer.class, val(key)))).fetch();
        ctx.select(function("set_config", String.class, val("lock_timeout"), val(previous), val(true))).fetch();
    }
}

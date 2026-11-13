// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

/**
 * The restore-load advisory lock (RDR-248 "Import session", Commit; bead nexus-csayy.6). One key for the
 * database. A tenant import's load takes it exclusively with {@code pg_try_advisory_xact_lock} as the load
 * transaction's first statement, so it is released by that transaction's end on every path; at most one load runs
 * at a time. {@code nexus.create_tenant_partitions} (changelog {@code vectors-059}) takes it SHARED, on its slow
 * path only (the path that runs DDL), so a tenant create waits out no load: it is refused at once, and
 * {@link TokenStore} turns that into {@code tenant_creation_busy}. The two sides must agree on the key text, which
 * {@code vectors-059} spells out in SQL; {@code RestoreLoadLockTest} pins both to this constant.
 */
public final class RestoreLoadLock {

    private RestoreLoadLock() {
    }

    /** The text the key hashes from. Never change it: the changelog function hashes the same text. */
    public static final String KEY_NAME = "nexus.restore_load";

    private static Field<Long> key() {
        return DSL.function("hashtextextended", SQLDataType.BIGINT, DSL.val(KEY_NAME), DSL.inline(0L));
    }

    /** Takes the lock exclusively for the current transaction if no load and no tenant create holds it. */
    public static boolean tryExclusive(DSLContext tx) {
        return Boolean.TRUE.equals(tx.select(DSL.function("pg_try_advisory_xact_lock", SQLDataType.BOOLEAN, key()))
            .fetchOne(0, Boolean.class));
    }

    /**
     * True when some session holds the key exclusively right now: a load is running. Read from {@code pg_locks}: a
     * bigint advisory key is the lock's classid (its high 32 bits) and objid (its low 32 bits), with objsubid 1.
     */
    public static boolean heldByLoad(DSLContext ctx) {
        Long k = ctx.select(key()).fetchOne(0, Long.class);
        if (k == null) return false;
        long classid = (k >>> 32) & 0xFFFFFFFFL;
        long objid = k & 0xFFFFFFFFL;
        var locks = DSL.table(DSL.name("pg_catalog", "pg_locks"));
        Field<String> type = DSL.field(DSL.name("locktype"), String.class);
        Field<String> mode = DSL.field(DSL.name("mode"), String.class);
        Field<Boolean> granted = DSL.field(DSL.name("granted"), Boolean.class);
        // classid and objid are oids; widen them to bigint so the 32-bit halves compare without sign trouble
        Field<Long> classidF = DSL.field(DSL.name("classid"), SQLDataType.BIGINT).cast(SQLDataType.BIGINT);
        Field<Long> objidF = DSL.field(DSL.name("objid"), SQLDataType.BIGINT).cast(SQLDataType.BIGINT);
        Field<Short> objsubid = DSL.field(DSL.name("objsubid"), Short.class);
        return ctx.fetchExists(ctx.selectOne().from(locks)
            .where(type.eq("advisory"), mode.eq("ExclusiveLock"), granted.isTrue(), objsubid.eq((short) 1),
                classidF.eq(classid), objidF.eq(objid)));
    }
}

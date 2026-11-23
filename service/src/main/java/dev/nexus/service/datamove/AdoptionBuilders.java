// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PciBuilders;
import dev.nexus.service.vectors.PciBuilderSession;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * What the adoption move does when it could not take its SHARE ROW EXCLUSIVE lock on chunks (nexus-p2m1x.21, linda 2026-11-23, option b). A running
 * {@code CREATE INDEX CONCURRENTLY} holds SHARE UPDATE EXCLUSIVE on a leaf and the lock conflicts with it, so ending builders after the lock is held
 * would never meet one. The pass that timed out finds who blocked it: the sessions holding, on chunks or a leaf, a lock that conflicts with SHARE ROW
 * EXCLUSIVE (ROW EXCLUSIVE and up, which is a writer's too). If, and only if, every one of them is a per-collection index builder of this engine (the
 * builder's name, this database, this role), it ends exactly those, logs which, and leaves the failure to be the 55P03 it already is, so the next
 * attempt after the backoff takes the lock. A blocker that is not a builder (a writer, autovacuum, a person's session) ends nothing: it is plain
 * 55P03 with backoff. A pass that deferred for a leaf, or that did not reach the lock, never gets here.
 */
final class AdoptionBuilders {

    private static final Logger log = LoggerFactory.getLogger(AdoptionBuilders.class);

    /** The lock modes that conflict with SHARE ROW EXCLUSIVE and so block the adoption's lock. */
    private static final List<String> CONFLICTING = List.of("RowExclusiveLock", "ShareUpdateExclusiveLock", "ShareLock",
        "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock");

    private AdoptionBuilders() { /* static */ }

    /** How long a signalled builder gets to leave the server: its locks are released only when the backend has exited, not when it is signalled. */
    static final long GONE_WAIT_MS = 2_000;

    /**
     * Waits, bounded by {@link #GONE_WAIT_MS}, until none of {@code pids} is a backend any more, so the next attempt does not meet the lock of a
     * backend that was signalled and is still exiting. The activity view's snapshot is dropped before each read (it is cached per transaction).
     *
     * @return whether they are all gone
     */
    private static boolean waitUntilGone(DSLContext admin, Set<Integer> pids) {
        long end = System.nanoTime() + GONE_WAIT_MS * 1_000_000L;
        Field<Integer> pid = DSL.field(DSL.name("pid"), Integer.class);
        while (true) {
            try {
                if (noneLeft(admin, pid, pids)) {
                    return true;
                }
            } catch (DataAccessException e) {
                log.warn("event=rdr229_adoption_builders_gone_unchecked cause=\"{}\"", FailureHandler.oneLine(e.toString()));
                return false;
            }
            if (System.nanoTime() >= end) {
                return false;
            }
            try {
                Thread.sleep(20);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }

    private static boolean noneLeft(DSLContext admin, Field<Integer> pid, Set<Integer> pids) {
        admin.select(DSL.function("pg_stat_clear_snapshot", Object.class)).fetch();
        return admin.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")), pid.in(pids)) == 0;
    }

    /** @return how many builders it ended (0 when a blocker was not a builder, or nothing blocked any more) */
    static int endBlockingBuilders(DSLContext admin) {
        try {
            var l = DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l");
            var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            var a = DSL.table(DSL.name("pg_catalog", "pg_stat_activity")).as("a");
            Field<Integer> pid = DSL.field(DSL.name("l", "pid"), Integer.class);
            Field<String> application = DSL.field(DSL.name("a", "application_name"), String.class);
            Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
            Field<String> relkind = DSL.field(DSL.name("c", "relkind"), String.class);
            Field<Boolean> ours = DSL.field(DSL.field(DSL.name("a", "datname"), String.class).eq(DSL.function("current_database", org.jooq.impl.SQLDataType.VARCHAR))
                .and(DSL.field(DSL.name("a", "usename"), String.class).eq(DSL.currentUser()))).as("ours");
            // pg_locks holds the locks of every database of the cluster, and a relation oid means nothing outside its own: only this database's rows
            var thisDatabase = DSL.field(DSL.name("l", "database")).eq(DSL.field(DSL.select(DSL.field(DSL.name("d", "oid")))
                .from(DSL.table(DSL.name("pg_catalog", "pg_database")).as("d"))
                .where(DSL.field(DSL.name("d", "datname"), String.class).eq(DSL.function("current_database", org.jooq.impl.SQLDataType.VARCHAR)))));
            var rows = admin.select(pid, application, relname, relkind, ours)
                .from(l)
                .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("l", "relation"))))
                .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .join(a).on(DSL.field(DSL.name("a", "pid")).eq(DSL.field(DSL.name("l", "pid"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(thisDatabase)
                .and(relname.eq("chunks").or(relname.like("chunks\\_%", '\\')))
                .and(DSL.field(DSL.name("l", "granted"), Boolean.class).isTrue())
                .and(DSL.field(DSL.name("l", "mode"), String.class).in(CONFLICTING))
                .and(pid.ne(DSL.function("pg_backend_pid", org.jooq.impl.SQLDataType.INTEGER)))
                .fetch();
            if (rows.isEmpty()) {
                return 0;
            }
            Map<Integer, Set<String>> leavesOf = new LinkedHashMap<>();
            Set<Integer> notBuilders = new LinkedHashSet<>();
            for (var r : rows) {
                int p = r.value1();
                leavesOf.computeIfAbsent(p, k -> new LinkedHashSet<>()).add(r.value3());
                if (!(r.value2() != null && r.value2().startsWith(PciBuilderSession.APPLICATION_NAME_PREFIX)) || !Boolean.TRUE.equals(r.value5())) {
                    notBuilders.add(p);
                }
            }
            if (!notBuilders.isEmpty()) {
                log.info("event=rdr229_adoption_blocked_by_others pids={} (not index builders of this engine: nothing is ended)", notBuilders);
                return 0;
            }
            Set<String> indexes = new LinkedHashSet<>();
            for (var r : admin.select(DSL.field(DSL.name("c", "relname"), String.class))
                    .from(l).join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("l", "relation"))))
                    .where(DSL.field(DSL.name("c", "relkind"), String.class).eq("i")).and(pid.in(leavesOf.keySet())).and(thisDatabase).fetch()) {
                indexes.add(r.value1());
            }
            List<String> leaves = new ArrayList<>();
            leavesOf.values().forEach(leaves::addAll);
            String why = "reason=rdr229_adoption_blocked pids=" + leavesOf.keySet() + " leaves=" + leaves + " indexes=" + indexes
                + " (they hold SHARE UPDATE EXCLUSIVE and the adoption's SHARE ROW EXCLUSIVE on chunks timed out behind them; the next attempt after the backoff takes the lock)";
            int ended = admin.transactionResult(cfg -> PciBuilders.terminatePidsOrThrow(cfg.dsl(), leavesOf.keySet(), why, log));
            boolean gone = waitUntilGone(admin, leavesOf.keySet());
            log.info("event=rdr229_adoption_ended_builders ended={} pids={} leaves={} indexes={} gone={}", ended, leavesOf.keySet(), leaves, indexes, gone);
            return ended;
        } catch (DataAccessException e) {
            log.warn("event=rdr229_adoption_builders_not_ended cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            return 0;
        }
    }
}

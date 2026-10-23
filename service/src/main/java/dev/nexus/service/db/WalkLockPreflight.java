// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;

/**
 * RDR-225 follow-up (nexus-vxmcl): the lock-capacity preflight that runs before the schema walk.
 *
 * <p>The walk {@code vectors-030-1} creates a leaf partition for every (model, tenant) pair on both partitioned
 * parents inside one transaction, and holds every new relation's locks until it commits. Those locks live in the
 * server's shared lock table, which Postgres sizes at {@code max_locks_per_transaction x (MaxBackends +
 * max_prepared_transactions)} entries (see Capacity below). A walk that needs more fails with "out of shared memory" after it has done
 * most of its work. The transaction rolls back and the store is untouched, but the engine exits, and every restart
 * repeats the whole copy and fails the same way until the setting is raised; on a local install nobody is watching
 * that loop. This check refuses before the copy instead, naming the setting to raise.
 *
 * <p><strong>The estimate.</strong> The walk creates {@code models x tenants x 2} leaves, where the models are the
 * {@code nexus.embedding_models} rows of a dimension that has a typed vector column (384, 768 or 1024) and the
 * tenants are {@code 'default'}, every {@code service_tokens} tenant, and every tenant holding a chunk or a
 * centroid: the same sets the walk's step 2 iterates. Each leaf costs about {@link #LOCKS_PER_LEAF} relation locks
 * (the leaf, its indexes and TOAST relations), each model about {@link #LOCKS_PER_MODEL} (its two model partitions
 * and their indexes), on top of {@link #FIXED_LOCKS} for the tables, views and functions every walk touches. The
 * three are a fit, rounded up, to walks counted end to end by the walk's own end-of-walk notice. At four models: 654
 * locks for 16 leaves and 3,466 for 168 ({@code WalkLockPreflightIntegrationTest}, which pins the estimate at or
 * above both), 513 for 8 leaves holding 100k 768-d chunks (a one-off run, T2
 * {@code nexus_rdr/225-vxmcl-walk-locks-and-populated-768-walk-2026-10-22}), and the cloud walk's 979 for 32 (T2
 * {@code nexus_rdr/225-rehearsal-2026-10-07}). At seven models: 1,899 for 70 leaves (RDR-225, 2026-10-06), which
 * is where the per-model term comes from. Row counts do not move the figure. The earlier per-leaf figures of 27
 * and 30.6 divided the whole count, fixed part included, by the leaves.
 *
 * <p><strong>When it runs:</strong> only while {@code vectors-030-1} is not yet in {@code
 * public.databasechangelog}, and only on a store that has a {@code nexus.chunks} table to walk (a fresh install
 * creates its leaves for {@code 'default'} alone). It reads the lock-table size with {@code current_setting},
 * which every role may read for these settings, so unlike the disk preflight it needs no local path and applies
 * to a managed database too.
 *
 * <p><strong>Reading the tenants.</strong> {@code nexus.chunks} and {@code nexus.taxonomy_centroids} carry FORCE
 * ROW LEVEL SECURITY and the migration role has no BYPASSRLS, so a plain read on this connection sees zero rows
 * (the nexus-1wjmq trap). The count therefore runs in a transaction that turns FORCE off on both tables, as the
 * walk itself does before step 2, and rolls back, leaving FORCE as it was found. A store from before
 * {@code catalog-036} has no {@code nexus.embedding_models} yet; the walk will find the four models
 * {@code catalog-036-2} seeds, so that count stands in.
 *
 * <p><strong>Capacity.</strong> A lower bound on the table Postgres guarantees: {@code max_locks_per_transaction}
 * times every backend slot, MaxBackends ({@code max_connections + autovacuum_max_workers + 1 +
 * max_worker_processes + max_wal_senders}; {@code autovacuum_worker_slots} in place of
 * {@code autovacuum_max_workers} where the server has it; PostgreSQL 17 counts one more special worker than the
 * {@code + 1} here, so this is a slot short there, on the safe side), plus {@code max_prepared_transactions}. A
 * setting the server does not have counts as 0; an unreadable {@code max_locks_per_transaction} or
 * {@code max_connections} skips the check. The estimate is admitted up to the whole table: room for other
 * sessions' locks is the shared-memory slack past it, which Postgres does not promise. Measured once, on one host
 * (laptop colima, {@code max_locks_per_transaction} 64, {@code max_connections} 20, default shared_buffers;
 * computed table 2,688): the walk passed holding 6,278 locks at 40 tenants and failed with SQLSTATE 53200 by 44
 * (T2 {@code nexus_rdr/225-xzit5-walk-lock-failure-boundary-2026-10-22}). How that slack scales with other
 * settings was not measured. {@code WalkLockPreflightIntegrationTest} walks the largest tenant count admitted at
 * a lowered setting.
 */
public final class WalkLockPreflight {

    private static final Logger log = LoggerFactory.getLogger(WalkLockPreflight.class);

    /** The changeset whose lock footprint this check guards. */
    static final String CHANGESET_ID = "vectors-030-1";

    /** Relation locks held per leaf partition at the walk's commit (measured; class javadoc). */
    static final long LOCKS_PER_LEAF = 19;

    /** Relation locks held per registered model at the walk's commit (measured; class javadoc). */
    static final long LOCKS_PER_MODEL = 80;

    /** Relation locks the walk holds whatever the model and tenant counts (measured; class javadoc). */
    static final long FIXED_LOCKS = 100;

    /** {@code lock_timeout} for the tenant count's ALTER; past it the check is skipped, never refused. */
    static final String LOCK_TIMEOUT = "5s";

    /** The models {@code catalog-036-2} seeds, for a store the walk will reach before that changeset has run. */
    static final long SEEDED_MODELS = 4;

    private WalkLockPreflight() { /* static utility */ }

    /** {@code FIXED_LOCKS + LOCKS_PER_MODEL x models + LOCKS_PER_LEAF x leaves}. */
    static long estimatedLocks(long models, long leaves) {
        return FIXED_LOCKS + LOCKS_PER_MODEL * models + LOCKS_PER_LEAF * leaves;
    }

    /**
     * Refuses (throws {@link SchemaMigrator.MigrationException}) when the walk is pending on a store with a
     * {@code nexus.chunks} table and its estimated lock count exceeds the server's lock table. Otherwise returns,
     * having logged why; a query that fails is logged and skipped, never a refusal.
     *
     * @param conn the migration connection, before Liquibase has run
     */
    static void check(Connection conn) {
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            if (!walkPending(ctx)) {
                return;
            }
            if (!exists(ctx, "nexus.chunks")) {
                log.info("event=walk_lock_preflight_skipped reason=\"nothing to walk: nexus.chunks does not exist yet\"");
                return;
            }
            long models = exists(ctx, "nexus.embedding_models") ? models(ctx) : SEEDED_MODELS;
            long tenants = tenants(conn);
            long leaves = models * tenants * 2;
            long estimated = estimatedLocks(models, leaves);
            long perTransaction = setting(ctx, "max_locks_per_transaction");
            if (perTransaction <= 0 || setting(ctx, "max_connections") <= 0) {
                log.warn("event=walk_lock_preflight_skipped reason=\"the lock-table settings are unreadable\"");
                return;
            }
            long workerSlots = setting(ctx, "autovacuum_worker_slots");
            long slots = setting(ctx, "max_connections")
                + (workerSlots > 0 ? workerSlots : setting(ctx, "autovacuum_max_workers")) + 1
                + setting(ctx, "max_worker_processes") + setting(ctx, "max_wal_senders")
                + setting(ctx, "max_prepared_transactions");
            long capacity = perTransaction * slots;
            if (estimated > capacity) {
                // Rounded up to a multiple of 64 (the default), which also leaves headroom for other sessions.
                long needed = ((estimated + slots - 1) / slots + 63) / 64 * 64;
                String dataDir = System.getenv(LocalDiskPreflight.DATA_DIR_ENV);
                String how = dataDir == null || dataDir.isBlank()
                    ? "in the server's configuration (postgresql.conf, or a managed database's parameters), restart "
                        + "Postgres, and start the engine again"
                    : "in " + dataDir.strip() + "/postgresql.conf, then run "
                        + "'nx daemon service stop --with-pg' and 'nx daemon service start'";
                log.error("event=walk_lock_preflight_refused estimated_locks={} lock_table_capacity={} leaves={} "
                    + "models={} tenants={} max_locks_per_transaction={} needed_max_locks_per_transaction={}",
                    estimated, capacity, leaves, models, tenants, perTransaction, needed);
                throw new SchemaMigrator.MigrationException(
                    "lock preflight: the " + CHANGESET_ID + " walk creates " + leaves + " leaf partitions ("
                    + models + " models x " + tenants + " tenants x 2 tables) in one transaction and holds about "
                    + estimated + " relation locks until it commits, but the server's lock table has room for "
                    + capacity + " (max_locks_per_transaction " + perTransaction + " x " + slots
                    + " backend and prepared-transaction slots). Set max_locks_per_transaction to at least "
                    + needed + " " + how + ".");
            }
            log.info("event=walk_lock_preflight_passed estimated_locks={} lock_table_capacity={} leaves={} models={} "
                + "tenants={}", estimated, capacity, leaves, models, tenants);
        } catch (DataAccessException | SQLException | NumberFormatException e) {
            // Advisory: a check that cannot read its inputs must not stop a boot the walk itself may finish.
            log.warn("event=walk_lock_preflight_skipped reason=\"lock preflight query failed: {}\"", e.toString());
        }
    }

    /** The models the walk's step 2 partitions: registered, with a dimension that has a typed vector column. */
    private static long models(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("nexus", "embedding_models")),
            DSL.field(DSL.name("dimension"), Integer.class).in(384, 768, 1024));
    }

    /**
     * {@code 'default'}, every token tenant, every tenant with a chunk or a centroid: step 2's tenant set. Counted
     * with FORCE off on the two data tables, in a transaction that is rolled back (class javadoc).
     */
    // SANCTIONED RAW (nexus-vxmcl): ALTER TABLE ... NO FORCE ROW LEVEL SECURITY is DDL with no jOOQ typed-DSL form,
    // the same category as SchemaMigrator.preflightChashConstraints' toggle. The table name is one of two constants.
    private static long tenants(Connection conn) throws SQLException {
        boolean autoCommit = conn.getAutoCommit();
        conn.setAutoCommit(false);
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            // The ALTER takes ACCESS EXCLUSIVE ahead of the migration lock: bounded, so an engine booting beside a
            // running walk (which holds chunks to its commit) skips this check instead of waiting silently.
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val("lock_timeout"), DSL.val(LOCK_TIMEOUT), DSL.val(true))).fetchOne();
            boolean centroids = exists(ctx, "nexus.taxonomy_centroids");
            for (String table : centroids ? List.of("chunks", "taxonomy_centroids") : List.of("chunks")) {
                try (Statement alter = conn.createStatement()) {
                    alter.execute("ALTER TABLE nexus." + table + " NO FORCE ROW LEVEL SECURITY");
                }
            }
            var tenant = DSL.field(DSL.name("tenant_id"), String.class);
            var union = DSL.select(tenant).from(DSL.table(DSL.name("nexus", "service_tokens")))
                .union(DSL.select(DSL.inline("default").as("tenant_id")))
                .union(DSL.select(tenant).from(DSL.table(DSL.name("nexus", "chunks"))));
            if (centroids) {
                union = union.union(DSL.select(tenant).from(DSL.table(DSL.name("nexus", "taxonomy_centroids"))));
            }
            return ctx.fetchCount(union);
        } finally {
            // DDL is transactional: the rollback puts FORCE back on both tables, on success and on failure alike.
            conn.rollback();
            conn.setAutoCommit(autoCommit);
        }
    }

    /** An integer server setting, 0 when this server version has no such setting. */
    private static long setting(DSLContext ctx, String name) {
        String value = ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val(name), DSL.val(true)))
            .fetchOne(0, String.class);
        return value == null || value.isBlank() ? 0L : Long.parseLong(value.strip());
    }

    private static boolean exists(DSLContext ctx, String qualifiedName) {
        return ctx.select(DSL.function("to_regclass", SQLDataType.VARCHAR, DSL.val(qualifiedName)))
            .fetchOne(0, String.class) != null;
    }

    /** True while {@code vectors-030-1} has no row in {@code public.databasechangelog} (also on a first boot). */
    private static boolean walkPending(DSLContext ctx) {
        if (!exists(ctx, "public.databasechangelog")) {
            return true;
        }
        return ctx.fetchCount(DSL.table(DSL.name("public", "databasechangelog")),
            DSL.field(DSL.name("id"), String.class).eq(CHANGESET_ID)) == 0;
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.OptionalLong;
import java.util.function.Supplier;

/**
 * RDR-225 P1.4 (nexus-3wh8d.9): the local disk preflight that runs before the schema walk.
 *
 * <p>The walk {@code vectors-030-1} copies {@code nexus.chunks} and {@code nexus.taxonomy_centroids} into
 * partitioned replacements inside one transaction, and keeps the old tables as the 14-day recovery copy, so
 * the copy, its indexes and the WAL it writes all land on the data directory's filesystem on top of what the
 * old tables already occupy. A disk that fills mid-walk fails the walk late and noisily. This check refuses to
 * start early instead, naming the shortfall.
 *
 * <p><strong>When it runs:</strong> only while {@code vectors-030-1} is not yet in {@code
 * public.databasechangelog}, and only when there is something to copy (a fresh install has no {@code
 * nexus.chunks} yet). Once the changeset is applied the check never runs again.
 *
 * <p><strong>The factor is inferred, not measured.</strong> {@link #requiredBytes} is 2.2x the combined
 * {@code pg_total_relation_size} (heap, indexes and TOAST) of the two tables: 1x for the copy, 1x for the WAL
 * the copy writes (estimated as 1x the table), 0.2x headroom. RDR-225 P0.4, the measured peak this was to be
 * compared against, was cancelled with the evaluation experiment, so nothing here has been checked against a
 * real walk. The Phase 3 rehearsal on a production fork measures the real peak; if it exceeds this
 * estimate, change {@link #FACTOR_TENTHS} and the RDR's statement together.
 *
 * <p><strong>Where free space is read.</strong> The engine may not share a filesystem with Postgres: the
 * cloud runs managed Postgres, and a BYO database is remote too. Free space is therefore read only from a data
 * directory the engine is explicitly told about, {@code NX_PG_DATA_DIR}, which the local supervisor sets from
 * the {@code PG_DATA} it already holds for the bundled cluster. Unset, or naming nothing the engine can see,
 * means "not local" and the check is skipped with a logged reason; a managed deployment's disk is covered by
 * the Phase 3 PITR-fork rehearsal. Two alternatives were rejected. Asking Postgres ({@code SHOW
 * data_directory}) does not work: the migration role is NOSUPERUSER and not a member of {@code
 * pg_read_all_settings}, so on every real local install the query would fail and the check would never fire.
 * Guessing locality from whether some path happens to exist would let a coincidental path on the engine's own
 * filesystem stand in for Postgres's disk.
 */
public final class LocalDiskPreflight {

    private static final Logger log = LoggerFactory.getLogger(LocalDiskPreflight.class);

    /** The changeset whose copy this check guards. */
    static final String CHANGESET_ID = "vectors-030-1";

    /** Environment variable naming the Postgres data directory when Postgres shares this host's filesystem. */
    public static final String DATA_DIR_ENV = "NX_PG_DATA_DIR";

    /** Required free space as tenths of the tables' total size: 2.2x. Inferred, not measured (class javadoc). */
    static final long FACTOR_TENTHS = 22;

    private LocalDiskPreflight() { /* static utility */ }

    /** The production free-space source: the data directory named by {@value #DATA_DIR_ENV}, if visible. */
    public static OptionalLong dataDirFreeBytesFromEnv() {
        return dataDirFreeBytes(System.getenv(DATA_DIR_ENV));
    }

    /**
     * Usable bytes on the filesystem holding {@code dataDir}, or empty (with a logged reason) when the value is
     * unset or blank, or names a path that is not a directory this process can see.
     */
    public static OptionalLong dataDirFreeBytes(String dataDir) {
        if (dataDir == null || dataDir.isBlank()) {
            log.info("event=disk_preflight_skipped reason=\"{} is unset: Postgres is not known to share this "
                + "host's filesystem (managed or remote database); disk headroom is not checked here\"",
                DATA_DIR_ENV);
            return OptionalLong.empty();
        }
        Path dir = Path.of(dataDir.strip());
        if (!Files.isDirectory(dir)) {
            log.info("event=disk_preflight_skipped reason=\"{} names a path the engine cannot see as a "
                + "directory: {}\"", DATA_DIR_ENV, dir);
            return OptionalLong.empty();
        }
        try {
            return OptionalLong.of(Files.getFileStore(dir).getUsableSpace());
        } catch (IOException e) {
            log.warn("event=disk_preflight_skipped reason=\"free space of {} is unreadable: {}\"", dir, e.toString());
            return OptionalLong.empty();
        }
    }

    /** {@code ceil(total * 2.2)}. */
    static long requiredBytes(long totalBytes) {
        return (totalBytes * FACTOR_TENTHS + 9) / 10;
    }

    /**
     * Refuses (throws {@link SchemaMigrator.MigrationException}) when the walk is pending, there is data to
     * copy, a free-space figure is available and it falls short. Otherwise returns, having logged why.
     *
     * @param conn      the migration connection, before Liquibase has run
     * @param freeBytes read only when the walk is pending and there is data to copy
     */
    static void check(Connection conn, Supplier<OptionalLong> freeBytes) throws SQLException {
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            if (!walkPending(ctx)) {
                return;
            }
            long total = totalRelationBytes(ctx, "nexus.chunks") + totalRelationBytes(ctx, "nexus.taxonomy_centroids");
            if (total == 0) {
                log.info("event=disk_preflight_skipped reason=\"nothing to copy: nexus.chunks does not exist yet\"");
                return;
            }
            OptionalLong free = freeBytes.get();
            if (free.isEmpty()) {
                return;     // the source logged its reason
            }
            long required = requiredBytes(total);
            if (free.getAsLong() < required) {
                long shortfall = required - free.getAsLong();
                log.error("event=disk_preflight_refused required_bytes={} free_bytes={} shortfall_bytes={} "
                    + "table_bytes={}", required, free.getAsLong(), shortfall, total);
                throw new SchemaMigrator.MigrationException(
                    "disk preflight: the " + CHANGESET_ID + " walk copies nexus.chunks and "
                    + "nexus.taxonomy_centroids and needs about 2.2x their size free on the Postgres data "
                    + "directory's filesystem: required " + required + " bytes, free " + free.getAsLong()
                    + " bytes, shortfall " + shortfall + " bytes (tables, indexes and TOAST: " + total
                    + " bytes). Free at least that much space and start the engine again.");
            }
            log.info("event=disk_preflight_passed required_bytes={} free_bytes={} table_bytes={}",
                required, free.getAsLong(), total);
        } catch (DataAccessException e) {
            throw new SQLException("disk preflight query failed", e);
        }
    }

    /** True while {@code vectors-030-1} has no row in {@code public.databasechangelog} (also on a first boot). */
    private static boolean walkPending(DSLContext ctx) {
        String regclass = ctx.select(DSL.function("to_regclass", SQLDataType.VARCHAR, DSL.val("public.databasechangelog")))
            .fetchOne(0, String.class);
        if (regclass == null) {
            return true;
        }
        Field<String> id = DSL.field(DSL.name("id"), String.class);
        return ctx.fetchCount(DSL.table(DSL.name("public", "databasechangelog")), id.eq(CHANGESET_ID)) == 0;
    }

    /** {@code pg_total_relation_size} of a schema-qualified relation, 0 when it does not exist. */
    private static long totalRelationBytes(DSLContext ctx, String qualifiedName) {
        Long bytes = ctx.select(DSL.function("pg_total_relation_size", SQLDataType.BIGINT,
                DSL.function("to_regclass", SQLDataType.OTHER, DSL.val(qualifiedName))))
            .fetchOne(0, Long.class);
        return bytes == null ? 0L : bytes;
    }
}

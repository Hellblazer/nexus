// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.Optional;
import java.util.concurrent.Callable;

/**
 * nexus-3s5r1: the engine's PostgreSQL floor, PostgreSQL 17, checked at boot.
 *
 * <p>Below 17 the engine already fails, but late and cryptically: the jsonpath {@code .number()} method in
 * {@code ManifestPushdown} and the numeric range predicates added with it do not exist before PG17, so the first
 * query that uses them errors at execution. Every supported deployment is PG17 today (the cloud service, the
 * bundled cluster, the test image). {@code Main} refuses to boot below the floor, with the version found and the
 * floor in the message, instead of serving until a request trips over it.
 *
 * <p>The decision ({@link #refusal}) is separate from reading the version ({@link #read}), so the decision is
 * tested against any version without a server of that version.
 */
public final class PgVersionFloor {

    private static final Logger log = LoggerFactory.getLogger(PgVersionFloor.class);

    /** PostgreSQL 17.0 as {@code server_version_num}. */
    public static final int MIN_SERVER_VERSION_NUM = 170_000;

    private PgVersionFloor() { }

    /**
     * @param versionNum  {@code server_version_num}, for example 170005
     * @param versionText {@code server_version}, for example {@code 17.5 (Debian 17.5-1.pgdg120+1)}
     */
    public record Found(int versionNum, String versionText) {}

    /** Raised when the server is below the floor, or its version cannot be read. */
    public static final class FloorException extends RuntimeException {
        public FloorException(String message) { super(message); }
    }

    /** The refusal text when {@code found} is below the floor; empty when it is at or above it. */
    static Optional<String> refusal(Found found) {
        if (found.versionNum() >= MIN_SERVER_VERSION_NUM) {
            return Optional.empty();
        }
        return Optional.of("PostgreSQL " + found.versionText() + " (server_version_num " + found.versionNum()
            + ") is below this engine's floor, PostgreSQL 17 (server_version_num " + MIN_SERVER_VERSION_NUM
            + "): the manifest pushdown's jsonpath .number() and the numeric range predicates fail at execution on"
            + " an older server. Point NX_DB_URL at a PostgreSQL 17 or later server (see docs/configuration.md,"
            + " \"Storage Service (Postgres) Prerequisites\"); if this is a restored or forked copy, check which"
            + " server version it was restored onto.");
    }

    /**
     * Reads the version through {@code reader} and throws {@link FloorException} when it is below the floor or
     * cannot be read: an engine that cannot tell what it runs on does not serve.
     *
     * @return what the reader saw, when it is at or above the floor
     */
    static Found verify(Callable<Found> reader) {
        Found found;
        try {
            found = reader.call();
        } catch (Exception e) {
            throw new FloorException("could not read server_version_num to check the PostgreSQL 17 floor: "
                + e.getMessage());
        }
        Optional<String> refusal = refusal(found);
        if (refusal.isPresent()) {
            throw new FloorException(refusal.get());
        }
        log.info("event=pg_version_floor_ok server_version_num={} server_version=\"{}\" floor={}",
            found.versionNum(), found.versionText(), MIN_SERVER_VERSION_NUM);
        return found;
    }

    /** {@link #verify} against the server {@code ds} connects to. Run it before the schema migration. */
    public static Found verifyAtBoot(DataSource ds) {
        return verify(() -> read(ds));
    }

    static Found read(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            var row = ctx.select(
                    DSL.function("current_setting", SQLDataType.VARCHAR, DSL.inline("server_version_num")),
                    DSL.function("current_setting", SQLDataType.VARCHAR, DSL.inline("server_version")))
                .fetchOne();
            return new Found(Integer.parseInt(row.value1()), row.value2());
        }
    }
}

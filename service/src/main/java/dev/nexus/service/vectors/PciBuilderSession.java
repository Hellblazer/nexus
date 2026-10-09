// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Locale;
import java.util.Objects;
import java.util.Properties;

/**
 * RDR-227 Step 2 (nexus-43ulx.16): the DDL half's database session. It builds and drops the per-collection HNSW
 * indexes ({@link PciCatalog#indexName}) and owns nothing else: what to build or drop is the reconciler's call.
 *
 * <p><b>A pass.</b> {@link #open()} connects, takes the builder lock and returns a {@link Pass}; {@link Pass#close()}
 * releases both. The caller opens one per sweep and closes it at the end, so the connection never outlives a pass.
 * The connection is a fresh {@code DriverManager} connection with the engine's {@code NX_DB_ADMIN_*} values
 * ({@link dev.nexus.service.db.AdminConnection}), never one from the pool and never through a pooler:
 * {@code CREATE INDEX CONCURRENTLY} needs a real session and cannot run in a transaction block, so the connection is
 * in autocommit and jOOQ's transaction wrapper is not used.
 *
 * <p><b>Why DDL goes out as text.</b> {@code CREATE INDEX CONCURRENTLY IF NOT EXISTS} and
 * {@code DROP INDEX CONCURRENTLY IF EXISTS} have no typed jOOQ form and cannot run inside a function. The text is
 * rendered by PostgreSQL ({@code format()} with {@code %I} and {@code %L}, called through {@code DSL.function}), so
 * identifiers and the collection are quoted by the server and Java concatenates none of them. Each statement
 * kind then goes out through one {@code Statement.execute(sql)} call ({@link #runBuild}, {@link #runDrop}), both
 * registered in {@code RawSqlGateTest.SANCTIONED_STATEMENTS}. Plain JDBC rather than jOOQ's {@code execute(String)}
 * because jOOQ parses a plain-SQL string for bind markers and braces, and a collection name may hold either.
 *
 * <p><b>Lock.</b> {@link #PCI_BUILDER_ADVISORY_LOCK_KEY} is a session advisory lock held for the whole pass, taken
 * with {@code pg_try_advisory_lock}: a second engine's pass finds it held, reports {@link BuilderState#STANDBY} and
 * does nothing. It sits outside the int4 range the repositories' {@code hashtext} transaction locks occupy, and is
 * not {@link SchemaMigrator#MIGRATION_ADVISORY_LOCK_KEY}.
 *
 * <p><b>Migration skip.</b> A migration walk and a concurrent build can deadlock on the same leaf, so the pass reads
 * {@code pg_locks} for the migration lock ({@link SchemaMigrator#migrationLockHolderPid}, the one copy of the
 * predicate) after it connects and again before every statement. If the migrator holds it the pass ends its DDL,
 * logs {@code event=pci_ddl_skipped reason=migration_in_progress} and closes. A migration that starts between the
 * check and the statement is the migrator's to handle (it terminates the builder backend; nexus-43ulx.17).
 *
 * <p><b>State.</b> {@link BuilderState} is what the status object reports as {@code builder_state}.
 */
public final class PciBuilderSession {

    private static final Logger log = LoggerFactory.getLogger(PciBuilderSession.class);

    /**
     * Session advisory lock the builder holds for a pass: the bytes of {@code "pcibuild"} as a bigint. Above the int4
     * range, and not {@link SchemaMigrator#MIGRATION_ADVISORY_LOCK_KEY}.
     */
    public static final long PCI_BUILDER_ADVISORY_LOCK_KEY = 0x7063696275696c64L;

    /** The builder backend's {@code application_name} is this plus the engine's per-boot nonce. */
    public static final String APPLICATION_NAME_PREFIX = "nexus-pci-builder-";

    /** HNSW {@code m} and {@code ef_construction}: must equal the leaf index's own (vectors-004, vectors-030). */
    public static final int BUILD_M = 16;
    public static final int BUILD_EF_CONSTRUCTION = 64;

    /** Above the 30-minute statement timeout, so the server gives up on a build before the socket does. */
    static final int SOCKET_TIMEOUT_SECONDS = 35 * 60;

    private static final String BUILD_LOCK_TIMEOUT = "0";
    private static final String BUILD_STATEMENT_TIMEOUT = "30min";
    private static final String DROP_LOCK_TIMEOUT = "5s";

    /** The pgvector operator class lives in schema {@code nexus} (search-path-001), and the builder has no search_path. */
    private static final String OPERATOR_CLASS = "nexus.vector_cosine_ops";

    private static final String BUILD_TEMPLATE =
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS %I ON %I.%I USING hnsw (%I " + OPERATOR_CLASS
            + ") WITH (m = " + BUILD_M + ", ef_construction = " + BUILD_EF_CONSTRUCTION
            + ") WHERE collection = %L";

    private static final String DROP_TEMPLATE = "DROP INDEX CONCURRENTLY IF EXISTS %I.%I";

    /** SQLSTATE: invalid_password. */
    private static final String INVALID_PASSWORD = "28P01";
    /** SQLSTATE: invalid_authorization_specification. */
    private static final String INVALID_AUTHORIZATION = "28000";
    /** SQLSTATE: insufficient_privilege. */
    private static final String INSUFFICIENT_PRIVILEGE = "42501";

    /** What the status object reports as {@code builder_state}. */
    public enum BuilderState {
        /** Holds the builder lock and has built (or can build) without a privilege or authentication error. */
        OK,
        /** The admin credentials were refused at connect; every pass logs it until it clears. */
        AUTH_FAILED,
        /** The connecting role cannot create an index on the leaf; logged once, nothing is built. */
        NO_PRIVILEGE,
        /** {@code NX_SEARCH_PCI=0}: no connection is opened. */
        OFF,
        /** A peer engine holds the builder lock. */
        STANDBY;

        /** The value in the status object. */
        public String wire() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    /** What one DDL request did. */
    public enum DdlOutcome {
        /** The statement ran (an index that already existed, or one already gone, counts). */
        DONE,
        /** The pass does not hold the builder role: off, auth failed or standby. Nothing was sent. */
        INACTIVE,
        /** The migrator holds its lock; the pass ended and nothing more will run in it. */
        SKIPPED_MIGRATION,
        /** The role may not do this; the pass ended and nothing more will run in it. */
        NO_PRIVILEGE,
        /** The request failed the name or shape check; nothing was sent. */
        REJECTED,
        /** The statement, or a check before it, failed; see the log. A failed build may leave an INVALID index. */
        FAILED
    }

    private final String url;
    private final String user;
    private final String password;
    private final String applicationName;
    private final PciSettings settings;
    private volatile BuilderState state = BuilderState.OK;
    private volatile boolean noPrivilegeLogged;

    /**
     * @param url      the admin JDBC URL ({@code NX_DB_ADMIN_URL}, else the application's)
     * @param user     the admin user
     * @param password the admin password; never logged
     * @param bootNonce this boot's id, from {@link BackendReaper#bootNonce}; the builder mints none of its own
     * @param settings the {@code NX_SEARCH_PCI*} settings, resolved at boot
     */
    public PciBuilderSession(String url, String user, String password, String bootNonce, PciSettings settings) {
        this.url = Objects.requireNonNull(url, "url");
        this.user = Objects.requireNonNull(user, "user");
        this.password = Objects.requireNonNull(password, "password");
        if (Objects.requireNonNull(bootNonce, "bootNonce").isBlank()) {
            throw new IllegalArgumentException("bootNonce must not be blank");
        }
        this.applicationName = APPLICATION_NAME_PREFIX + bootNonce;
        this.settings = Objects.requireNonNull(settings, "settings");
    }

    /** The state the most recent pass ended in; {@link BuilderState#OK} before the first. */
    public BuilderState state() {
        return state;
    }

    /**
     * Start a pass. Returns a pass in {@link BuilderState#OFF}, {@link BuilderState#AUTH_FAILED} or
     * {@link BuilderState#STANDBY} that refuses all DDL, or an active one. The caller closes it.
     *
     * @throws IllegalStateException when the connection fails for a reason other than authentication
     *                               (the database is unreachable); the previous state is left in place
     */
    public Pass open() {
        if (!settings.enabled()) {
            state = BuilderState.OFF;
            return Pass.inactive(this, BuilderState.OFF);
        }
        Connection conn;
        try {
            conn = connect();
        } catch (SQLException e) {
            String sqlState = e.getSQLState();
            if (INVALID_PASSWORD.equals(sqlState) || INVALID_AUTHORIZATION.equals(sqlState)) {
                state = BuilderState.AUTH_FAILED;
                log.warn("event=pci_builder_auth_failed sqlstate={} user={} hint=\"NX_DB_ADMIN_* are read once at boot; "
                    + "restart the engine after rotating the admin password\"", sqlState, user);
                return Pass.inactive(this, BuilderState.AUTH_FAILED);
            }
            throw new IllegalStateException("pci builder could not connect: sqlstate=" + sqlState, e);
        }
        Pass pass = null;
        try {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            if (!Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                    DSL.val(PCI_BUILDER_ADVISORY_LOCK_KEY))).fetchOne(0, Boolean.class))) {
                closeQuietly(conn);
                state = BuilderState.STANDBY;
                log.debug("event=pci_builder_standby reason=peer_holds_lock key={}", PCI_BUILDER_ADVISORY_LOCK_KEY);
                return Pass.inactive(this, BuilderState.STANDBY);
            }
            if (state != BuilderState.NO_PRIVILEGE) {
                state = BuilderState.OK;
            }
            pass = new Pass(this, conn, ctx);
            if (pass.migrationHolds()) {
                pass.endForMigration();
            }
            return pass;
        } catch (DataAccessException e) {
            if (pass != null) {
                pass.close();
            } else {
                closeQuietly(conn);
            }
            throw new IllegalStateException("pci builder could not start a pass: sqlstate=" + e.sqlState(), e);
        }
    }

    private Connection connect() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", user);
        props.setProperty("password", password);
        props.setProperty("ApplicationName", applicationName);
        props.setProperty("connectTimeout", Integer.toString(BackendReaper.CONNECT_TIMEOUT_SECONDS));
        props.setProperty("socketTimeout", Integer.toString(SOCKET_TIMEOUT_SECONDS));
        Connection conn = DriverManager.getConnection(url, props);
        try {
            conn.setAutoCommit(true);
        } catch (SQLException | RuntimeException e) {
            closeQuietly(conn);
            throw e;
        }
        return conn;
    }

    private static void closeQuietly(Connection conn) {
        try {
            conn.close();
        } catch (SQLException | RuntimeException e) {
            log.debug("event=pci_builder_close_failed cause=\"{}\"", e.toString());
        }
    }

    /** One pass: a connection and the builder lock, or an inert stand-in that refuses DDL. */
    public static final class Pass implements AutoCloseable {
        private final PciBuilderSession owner;
        private final BuilderState state;
        private Connection conn;
        private DSLContext ctx;
        /** The outcome every DDL request returns once the pass is over, or {@code null} while it is live. */
        private DdlOutcome ended;
        private boolean skippedForMigration;

        private Pass(PciBuilderSession owner, Connection conn, DSLContext ctx) {
            this.owner = owner;
            this.state = BuilderState.OK;
            this.conn = conn;
            this.ctx = ctx;
        }

        private static Pass inactive(PciBuilderSession owner, BuilderState state) {
            Pass p = new Pass(owner, null, null, state);
            p.ended = DdlOutcome.INACTIVE;
            return p;
        }

        private Pass(PciBuilderSession owner, Connection conn, DSLContext ctx, BuilderState state) {
            this.owner = owner;
            this.state = state;
            this.conn = conn;
            this.ctx = ctx;
        }

        /** The state this pass found or reached; a privilege failure moves it to {@link BuilderState#NO_PRIVILEGE}. */
        public BuilderState state() {
            return ended == DdlOutcome.NO_PRIVILEGE ? BuilderState.NO_PRIVILEGE : state;
        }

        /** True once the pass saw the migrator's lock and ended its DDL. */
        public boolean skippedForMigration() {
            return skippedForMigration;
        }

        /**
         * Build the index for {@code collection} on {@code leaf}, whose embedding column is {@code embedding_<dim>}.
         * The name is {@link PciCatalog#indexName} of the leaf's model and tenant and the collection.
         */
        public DdlOutcome build(PciCatalog.Leaf leaf, int dim, String collection) {
            if (ended != null) {
                return ended;
            }
            String name;
            String column;
            try {
                Objects.requireNonNull(leaf, "leaf");
                Objects.requireNonNull(collection, "collection");
                if (leaf.model() == null || leaf.tenant() == null || !DimTables.CHUNKS.containsKey(dim)) {
                    log.warn("event=pci_ddl_rejected op=build reason=leaf_or_dim leaf={} dim={}", leaf.name(), dim);
                    return DdlOutcome.REJECTED;
                }
                name = PciCatalog.indexName(leaf.model(), leaf.tenant(), collection);
                column = DimTables.embeddingColumn(dim);
            } catch (IllegalArgumentException e) {
                log.warn("event=pci_ddl_rejected op=build reason=name cause=\"{}\"", e.getMessage());
                return DdlOutcome.REJECTED;
            }
            if (!PciCatalog.isBuilderName(name)) {
                log.warn("event=pci_ddl_rejected op=build reason=name_shape name={}", name);
                return DdlOutcome.REJECTED;
            }
            return run("build", BUILD_LOCK_TIMEOUT, name, leaf, () -> {
                String sql = render(BUILD_TEMPLATE, name, leaf.schema(), leaf.name(), column, collection);
                runBuild(sql);
            });
        }

        /**
         * Drop {@code index} from {@code leaf}. Only a parsed index with the builder's name shape is touched; an
         * operator's or leftover {@code pci_} index is {@link DdlOutcome#REJECTED}.
         */
        public DdlOutcome drop(PciCatalog.Leaf leaf, PciCatalog.Index index) {
            if (ended != null) {
                return ended;
            }
            Objects.requireNonNull(leaf, "leaf");
            Objects.requireNonNull(index, "index");
            if (!index.parsed() || !PciCatalog.isBuilderName(index.name())) {
                log.warn("event=pci_ddl_rejected op=drop reason=not_builders_index name={} parsed={}",
                    index.name(), index.parsed());
                return DdlOutcome.REJECTED;
            }
            return run("drop", DROP_LOCK_TIMEOUT, index.name(), leaf, () -> {
                String sql = render(DROP_TEMPLATE, leaf.schema(), index.name());
                runDrop(sql);
            });
        }

        private interface Ddl {
            void execute() throws SQLException;
        }

        private DdlOutcome run(String op, String lockTimeout, String indexName, PciCatalog.Leaf leaf,
                               Ddl statement) {
            long startedNanos = System.nanoTime();
            try {
                if (migrationHolds()) {
                    endForMigration();
                    return ended;
                }
                setSession("lock_timeout", lockTimeout);
                setSession("statement_timeout", BUILD_STATEMENT_TIMEOUT);
                statement.execute();
            } catch (SQLException e) {
                return failed(op, indexName, leaf, e.getSQLState(), e);
            } catch (DataAccessException e) {
                return failed(op, indexName, leaf, e.sqlState(), e);
            }
            owner.noPrivilegeLogged = false;
            if (owner.state == BuilderState.NO_PRIVILEGE) {
                owner.state = BuilderState.OK;
            }
            log.info("event=pci_ddl_done op={} index={} leaf={}.{} took_ms={}", op, indexName, leaf.schema(),
                leaf.name(), (System.nanoTime() - startedNanos) / 1_000_000);
            return DdlOutcome.DONE;
        }

        private DdlOutcome failed(String op, String indexName, PciCatalog.Leaf leaf, String sqlState, Exception e) {
            if (INSUFFICIENT_PRIVILEGE.equals(sqlState)) {
                owner.state = BuilderState.NO_PRIVILEGE;
                if (!owner.noPrivilegeLogged) {
                    owner.noPrivilegeLogged = true;
                    log.warn("event=pci_builder_no_privilege op={} index={} leaf={}.{} user={} "
                        + "hint=\"the admin role cannot create or drop indexes on the leaf; nothing is built\"",
                        op, indexName, leaf.schema(), leaf.name(), owner.user);
                }
                ended = DdlOutcome.NO_PRIVILEGE;
                return ended;
            }
            log.warn("event=pci_ddl_failed op={} index={} leaf={}.{} sqlstate={} cause=\"{}\"", op, indexName,
                leaf.schema(), leaf.name(), sqlState, e.getMessage());
            if (sqlState != null && (sqlState.startsWith("08") || "57P01".equals(sqlState))) {
                // The connection is gone (network, or terminated by the migrator or shutdown): nothing more can run.
                ended = DdlOutcome.FAILED;
                close();
            }
            return DdlOutcome.FAILED;
        }

        private boolean migrationHolds() {
            // A failed read throws: unknown is not "not held", so it is never permission to run DDL.
            return SchemaMigrator.migrationLockHolderPid(ctx) >= 0;
        }

        private void endForMigration() {
            skippedForMigration = true;
            ended = DdlOutcome.SKIPPED_MIGRATION;
            log.info("event=pci_ddl_skipped reason=migration_in_progress key={}",
                SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);
            close();
        }

        private void setSession(String name, String value) {
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value),
                DSL.inline(false))).fetch();
        }

        /** The text of one statement, rendered by the server: {@code format(template, args...)}. */
        private String render(String template, String... args) {
            var params = new org.jooq.Field<?>[args.length + 1];
            params[0] = DSL.val(template);
            for (int i = 0; i < args.length; i++) {
                params[i + 1] = DSL.val(args[i]);
            }
            return ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
        }

        // SANCTIONED RAW (RDR-227, nexus-43ulx.16): CREATE INDEX CONCURRENTLY has no typed jOOQ form and cannot run
        // in a function or a transaction block. The text is rendered by format() with %I/%L on the server (render).
        private void runBuild(String sql) throws SQLException {
            try (java.sql.Statement st = conn.createStatement()) {
                st.execute(sql);
            }
        }

        // SANCTIONED RAW (RDR-227, nexus-43ulx.16): DROP INDEX CONCURRENTLY has no typed jOOQ form and cannot run in
        // a function or a transaction block. The text is rendered by format() with %I on the server (render).
        private void runDrop(String sql) throws SQLException {
            try (java.sql.Statement st = conn.createStatement()) {
                st.execute(sql);
            }
        }

        /** The session's current value of {@code name}, for tests. */
        String currentSetting(String name) {
            return ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val(name)))
                .fetchOne(0, String.class);
        }

        /** Release the builder lock and close the connection. Safe to call twice. */
        @Override
        public void close() {
            if (conn == null) {
                return;
            }
            Connection c = conn;
            DSLContext context = ctx;
            conn = null;
            ctx = null;
            if (ended == null) {
                ended = DdlOutcome.INACTIVE;
            }
            try {
                context.select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN,
                    DSL.val(PCI_BUILDER_ADVISORY_LOCK_KEY))).fetchOne();
            } catch (DataAccessException e) {
                // Closing the connection releases it anyway.
                log.debug("event=pci_builder_unlock_failed cause=\"{}\"", e.toString());
            }
            closeQuietly(c);
        }
    }
}

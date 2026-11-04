// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Properties;

/**
 * The chunk-table constraints of RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): adds them NOT VALID, VALIDATEs them, and
 * reports their state. Never a startup changeset: a boot-time VALIDATE crash-loops an install whose rows differ from the
 * measured one (rdr180-001, vectors-004), so the DDL goes out only when an operator's route call sends it, on the
 * engine's admin connection ({@link AdminConnection}, the values the migrator and the index builder use), because the
 * service role does not own {@code nexus.chunks}.
 *
 * <p><b>The five constraints.</b>
 * <ul>
 *   <li>{@link #ALLOW_LIST}: {@code (collection starts with a bypass prefix) OR (metadata - allowed) = '{}'}. The
 *       allowed array and the prefixes are the request's ({@link ChunkKeyPlan}), rendered by the server's {@code
 *       format()} with {@code %L}; a key-table change re-adds the constraint with {@code replace}, because a Liquibase
 *       changeset is immutable and so cannot carry the array.</li>
 *   <li>{@link #CHUNK_TEXT_HASH}: {@code metadata->>'chunk_text_hash' = encode(chash, 'hex')}.</li>
 *   <li>{@link #EMBEDDING_MODEL}: {@code metadata->>'embedding_model' = embedding_model}, the one uncensused
 *       chunk-table equality (6pfgb.42 counts it, this route ships it).</li>
 *   <li>{@link #QUARANTINE_KEYS}: the first key of {@link ChunkKeyPlan#quarantineImplies()} implies the second; added
 *       only when the request names the pair.</li>
 *   <li>{@link #CHASH_WIDTH}: {@code chunks_chash_octet_check}, which vectors-004 left NOT VALID on purpose; this route
 *       only validates it.</li>
 * </ul>
 * A CHECK passes when its expression is NULL, so a row without the key is not refused by the equalities.
 *
 * <p><b>Guards.</b> The migrator's advisory lock is read before each statement (a migration walk and DDL on the same
 * table can deadlock); {@code lock_timeout} is 5 s so the ACCESS EXCLUSIVE an ADD needs for an instant never queues
 * behind a long reader and, behind it, every writer; one run at a time on the {@link #ADVISORY_LOCK_KEY} lock. A
 * refusal is a {@link Refused} the handler answers with its status.
 *
 * <p>DATA EFFECT of {@code add}: none on existing rows (NOT VALID checks only rows written afterwards: a write that
 * carries a key outside the allow-list is then refused with SQLSTATE 23514). DATA EFFECT of {@code validate}: none; it
 * reads the whole table under SHARE UPDATE EXCLUSIVE, which lets writers through, and fails (leaving the constraint NOT
 * VALID) if a row violates it.
 */
public final class ChunkConstraintAdmin {

    private static final Logger log = LoggerFactory.getLogger(ChunkConstraintAdmin.class);

    /** The session advisory lock a constraint run holds: the bytes of {@code "ckconstr"} as a bigint. */
    public static final long ADVISORY_LOCK_KEY = 0x636b636f6e737472L;

    public static final String ALLOW_LIST = "chunks_metadata_keys_check";
    public static final String CHUNK_TEXT_HASH = "chunks_chunk_text_hash_check";
    public static final String EMBEDDING_MODEL = "chunks_embedding_model_check";
    public static final String QUARANTINE_KEYS = "chunks_quarantine_keys_check";
    /** Created NOT VALID by vectors-004; this route validates it. */
    public static final String CHASH_WIDTH = "chunks_chash_octet_check";

    /** Every constraint this class manages, in the order it adds and validates them. */
    public static final List<String> ALL = List.of(ALLOW_LIST, CHUNK_TEXT_HASH, EMBEDDING_MODEL, QUARANTINE_KEYS,
        CHASH_WIDTH);

    private static final String LOCK_TIMEOUT = "5s";
    private static final String ADD_STATEMENT_TIMEOUT = "60s";
    private static final String VALIDATE_STATEMENT_TIMEOUT = "30min";
    /** Above the validate bound, so the server gives up before the socket does. */
    private static final int SOCKET_TIMEOUT_SECONDS = 35 * 60;
    private static final int LOGIN_TIMEOUT_SECONDS = 30;
    private static final String APPLICATION_NAME = "nexus-chunk-constraints";

    /** SQLSTATE: lock_not_available (the 5 s lock_timeout tripped). */
    private static final String LOCK_NOT_AVAILABLE = "55P03";
    /** SQLSTATE: check_violation (a VALIDATE found a row that breaks the constraint). */
    private static final String CHECK_VIOLATION = "23514";
    /** SQLSTATE: insufficient_privilege. */
    private static final String INSUFFICIENT_PRIVILEGE = "42501";

    /** Why a call was refused; {@code status} is the HTTP status the handler answers. */
    public static final class Refused extends RuntimeException {
        private final int status;
        private final String reason;

        public Refused(int status, String reason, String message) {
            super(message);
            this.status = status;
            this.reason = reason;
        }

        public int status() {
            return status;
        }

        public String reason() {
            return reason;
        }
    }

    /** One constraint's state on {@code nexus.chunks}. */
    public record State(String name, boolean present, Boolean validated, String definition) {}

    /** What a call did to one constraint, and the state it left. */
    public record Action(String name, String action, State after) {}

    private final AdminConnection admin;

    public ChunkConstraintAdmin(AdminConnection admin) {
        this.admin = Objects.requireNonNull(admin, "admin");
    }

    // ---- public ------------------------------------------------------------------------------------------------

    /** The state of every managed constraint, read on the admin connection. */
    public List<State> states() {
        try (Session s = open(false)) {
            return s.states();
        }
    }

    /**
     * Add the constraints NOT VALID. A constraint that exists is left alone unless {@code replace}, which drops and
     * re-adds it in one statement (a changed allow-list); an existing constraint is reported with its definition so the
     * operator can see whether it matches.
     */
    public List<Action> add(ChunkKeyPlan plan, boolean replace) {
        try (Session s = open(true)) {
            s.guard(ADD_STATEMENT_TIMEOUT);
            List<Action> out = new ArrayList<>();
            List<State> before = s.states();
            for (String name : List.of(ALLOW_LIST, CHUNK_TEXT_HASH, EMBEDDING_MODEL, QUARANTINE_KEYS)) {
                if (QUARANTINE_KEYS.equals(name) && plan.quarantineImplies().isEmpty()) {
                    continue;
                }
                boolean exists = stateOf(before, name).present();
                if (exists && !replace) {
                    out.add(new Action(name, "present", null));
                    continue;
                }
                s.ddl(addStatement(name, plan, exists), args(name, plan, exists));
                out.add(new Action(name, exists ? "replaced" : "added", null));
            }
            List<State> after = s.states();
            List<Action> done = new ArrayList<>();
            for (Action a : out) {
                done.add(new Action(a.name(), a.action(), stateOf(after, a.name())));
            }
            log.info("event=chunk_constraints_added actions={}", done.stream().map(a -> a.name() + ":" + a.action())
                .toList());
            return done;
        }
    }

    /** VALIDATE every managed constraint that exists and is not yet valid; one that is absent is reported. */
    public List<Action> validate() {
        try (Session s = open(true)) {
            s.guard(VALIDATE_STATEMENT_TIMEOUT);
            List<Action> out = new ArrayList<>();
            // convalidated is read first: a VALIDATE of a valid constraint is a no-op that still takes the lock.
            List<State> before = s.states();
            for (String name : ALL) {
                State st = stateOf(before, name);
                if (!st.present()) {
                    out.add(new Action(name, "absent", st));
                } else if (Boolean.TRUE.equals(st.validated())) {
                    out.add(new Action(name, "already_valid", st));
                } else {
                    s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", validateArgs(name));
                    out.add(new Action(name, "validated", null));
                }
            }
            List<State> after = s.states();
            List<Action> done = new ArrayList<>();
            for (Action a : out) {
                done.add(new Action(a.name(), a.action(), a.after() != null ? a.after() : stateOf(after, a.name())));
            }
            log.info("event=chunk_constraints_validated actions={}",
                done.stream().map(a -> a.name() + ":" + a.action()).toList());
            return done;
        }
    }

    private static State stateOf(List<State> states, String name) {
        for (State s : states) {
            if (s.name().equals(name)) {
                return s;
            }
        }
        return new State(name, false, null, null);
    }

    // ---- statements ----------------------------------------------------------------------------------------------

    /** The {@code format()} template of one ADD; every value reaches it as an argument, never as text. */
    static String addStatement(String name, ChunkKeyPlan plan, boolean replace) {
        StringBuilder t = new StringBuilder("ALTER TABLE %I.%I ");
        if (replace) {
            t.append("DROP CONSTRAINT %I, ");
        }
        t.append("ADD CONSTRAINT %I CHECK (");
        switch (name) {
            case ALLOW_LIST -> {
                t.append('(');
                for (int i = 0; i < plan.bypassPrefixes().size(); i++) {
                    t.append("starts_with(collection, %L) OR ");
                }
                t.append("(metadata - %L::text[]) = '{}'::jsonb)");
            }
            case CHUNK_TEXT_HASH -> t.append("(metadata ->> 'chunk_text_hash') = encode(chash, 'hex')");
            case EMBEDDING_MODEL -> t.append("(metadata ->> 'embedding_model') = embedding_model");
            case QUARANTINE_KEYS -> t.append("NOT jsonb_exists(metadata, %L) OR jsonb_exists(metadata, %L)");
            default -> throw new IllegalArgumentException("not an addable constraint: " + name);
        }
        t.append(") NOT VALID");
        return t.toString();
    }

    private static List<Object> args(String name, ChunkKeyPlan plan, boolean replace) {
        List<Object> a = new ArrayList<>();
        a.add(CHUNKS.getSchema().getName());
        a.add(CHUNKS.getName());
        if (replace) {
            a.add(name);
        }
        a.add(name);
        switch (name) {
            case ALLOW_LIST -> {
                a.addAll(plan.bypassPrefixes());
                a.add(plan.allowed().toArray(new String[0]));
            }
            case QUARANTINE_KEYS -> a.addAll(plan.quarantineImplies());
            default -> { }
        }
        return a;
    }

    private static List<Object> validateArgs(String name) {
        return List.of(CHUNKS.getSchema().getName(), CHUNKS.getName(), name);
    }

    // ---- the admin session ---------------------------------------------------------------------------------------

    private Session open(boolean lock) {
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", APPLICATION_NAME);
        props.setProperty("connectTimeout", Integer.toString(BackendReaper.CONNECT_TIMEOUT_SECONDS));
        props.setProperty("loginTimeout", Integer.toString(LOGIN_TIMEOUT_SECONDS));
        props.setProperty("socketTimeout", Integer.toString(SOCKET_TIMEOUT_SECONDS));
        props.setProperty("tcpKeepAlive", "true");
        Connection conn;
        try {
            conn = DriverManager.getConnection(admin.url(), props);
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            throw new Refused(503, "admin_connection_failed",
                "the constraint run could not open the admin connection: sqlstate=" + e.getSQLState());
        }
        Session s = new Session(conn);
        if (lock) {
            try {
                s.lock();
            } catch (RuntimeException e) {
                s.close();
                throw e;
            }
        }
        return s;
    }

    /** One admin connection, holding the run lock when it was opened to write. */
    private static final class Session implements AutoCloseable {
        private final Connection conn;
        private final DSLContext ctx;
        private boolean locked;

        Session(Connection conn) {
            this.conn = conn;
            this.ctx = DSL.using(conn, SQLDialect.POSTGRES);
        }

        void lock() {
            if (!Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                    DSL.val(ADVISORY_LOCK_KEY))).fetchOne(0, Boolean.class))) {
                throw new Refused(409, "constraint_run_in_progress",
                    "another chunk-constraint run holds the lock; wait for it to finish");
            }
            locked = true;
        }

        /** Per-statement guards: no migration walk, a short lock wait, and the statement bound of this call. */
        void guard(String statementTimeout) {
            if (SchemaMigrator.migrationLockHolderPid(ctx) >= 0) {
                throw new Refused(503, "migration_in_progress",
                    "a schema migration holds its lock; run the constraint step after the engine has booted");
            }
            setSession("lock_timeout", LOCK_TIMEOUT);
            setSession("statement_timeout", statementTimeout);
        }

        private void setSession(String name, String value) {
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value),
                DSL.inline(false))).fetch();
        }

        List<State> states() {
            var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
            var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
            var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
            Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
            Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
            Field<String> def = DSL.function("pg_get_constraintdef", SQLDataType.VARCHAR,
                DSL.field(DSL.name("con", "oid")));
            var rows = ctx.select(conname, validated, def)
                .from(con)
                .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
                .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
                .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq(CHUNKS.getSchema().getName()))
                .and(DSL.field(DSL.name("cls", "relname"), String.class).eq(CHUNKS.getName()))
                .and(conname.in(ALL))
                .fetch();
            List<State> out = new ArrayList<>();
            for (String name : ALL) {
                State found = null;
                for (var r : rows) {
                    if (name.equals(r.value1())) {
                        found = new State(name, true, r.value2(), r.value3());
                    }
                }
                out.add(found != null ? found : new State(name, false, null, null));
            }
            return out;
        }

        /** Render {@code template} on the server (format() with %I and %L) and send the text on this connection. */
        void ddl(String template, List<Object> args) {
            Field<?>[] params = new Field<?>[args.size() + 1];
            params[0] = DSL.val(template);
            for (int i = 0; i < args.size(); i++) {
                Object a = args.get(i);
                params[i + 1] = a instanceof String[] arr
                    ? DSL.val(arr, SQLDataType.CLOB.array()) : DSL.val((String) a);
            }
            String sql = ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
            try {
                runDdl(sql);
            } catch (SQLException e) {
                throw refusal(e);
            }
        }

        // SANCTIONED RAW (RDR-236 Phase 4 Step 3, nexus-6pfgb.43): ALTER TABLE ... ADD CONSTRAINT ... NOT VALID and
        // VALIDATE CONSTRAINT have no typed jOOQ form that takes a rendered CHECK expression. The text is rendered by
        // PostgreSQL's format() with %I and %L (ddl), so no identifier, key or prefix is concatenated in Java. Plain
        // JDBC Statement.execute, as PciBuilderSession does. Single-homed: ddl.
        private void runDdl(String sql) throws SQLException {
            try (Statement st = conn.createStatement()) {
                st.execute(sql);
            }
        }

        private static Refused refusal(SQLException e) {
            String state = e.getSQLState();
            if (LOCK_NOT_AVAILABLE.equals(state)) {
                return new Refused(503, "lock_timeout",
                    "the table lock was not available within " + LOCK_TIMEOUT + "; nothing changed, run it again");
            }
            if (CHECK_VIOLATION.equals(state)) {
                return new Refused(409, "check_violation",
                    "VALIDATE found a row that breaks the constraint; it stays NOT VALID. Count the offenders with "
                        + "the dry run and fix them first");
            }
            if (INSUFFICIENT_PRIVILEGE.equals(state)) {
                return new Refused(503, "insufficient_privilege",
                    "the admin role may not alter nexus.chunks (NX_DB_ADMIN_* must name its owner)");
            }
            log.warn("event=chunk_constraint_ddl_failed sqlstate={} cause=\"{}\"", state, e.getMessage());
            return new Refused(500, "ddl_failed", "the constraint statement failed: sqlstate=" + state);
        }

        @Override
        public void close() {
            try {
                if (locked) {
                    ctx.select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN, DSL.val(ADVISORY_LOCK_KEY)))
                        .fetchOne();
                }
            } catch (DataAccessException e) {
                log.debug("event=chunk_constraint_unlock_failed cause=\"{}\"", e.toString());
            } finally {
                try {
                    conn.close();
                } catch (SQLException e) {
                    log.debug("event=chunk_constraint_close_failed cause=\"{}\"", e.toString());
                }
            }
        }
    }
}

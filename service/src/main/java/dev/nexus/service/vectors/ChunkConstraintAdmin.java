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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
 *   <li>{@link #EMBEDDING_MODEL}: {@code metadata->>'embedding_model' = embedding_model}, an equality the RF-16 census
 *       did not count; the probe counts it here and {@code add} refuses while it is broken. The other uncensused
 *       equalities of the RDR (manifest collection against the document's physical collection, chunk_index against
 *       position, labels_written) are not chunk-table CHECKs and not part of these routes.</li>
 *   <li>{@link #QUARANTINE_KEYS}: the first key of {@link ChunkKeyPlan#quarantineImplies()} implies the second (the
 *       pair is required of every request and pinned by digest).</li>
 *   <li>{@link #CHASH_WIDTH}: {@code chunks_chash_octet_check}, which vectors-004 left NOT VALID on purpose; this route
 *       only validates it.</li>
 * </ul>
 * A CHECK passes when its expression is NULL, so a row without the key is not refused by the equalities.
 *
 * <p><b>Guards.</b> The migrator's advisory lock is read once per run, before the first statement (a migration walk and
 * DDL on the same table can deadlock); {@code lock_timeout} is 5 s so the ACCESS EXCLUSIVE an ADD needs for an instant
 * never queues behind a long reader and, behind it, every writer; one run at a time on the {@link #ADVISORY_LOCK_KEY}
 * lock. A refusal is a {@link Refused} the handler answers with its status.
 *
 * <p><b>The table-wide probe.</b> {@link #probe} reads every tenant's counts in one pass on the admin session, so a
 * tenant with chunk rows and no {@code service_tokens} row is seen. It sees them ONLY when the session is not row
 * filtered, and {@code nexus.chunks} is FORCE row-level-security: a role that is neither superuser nor BYPASSRLS reads
 * every row only through a permissive SELECT policy whose qual is {@code true} that applies to it
 * ({@code chunks_gate_probe_owner_read}, vectors-029, which names the MIGRATING role, so it does not reach a different
 * {@code NX_DB_ADMIN_USER} and is skipped in the single-role posture). Without one the probe reads zero rows and nothing
 * looks wrong, so {@link #probe} first checks the posture and refuses with {@code admin_cannot_see_all_tenants}
 * ({@link #requireUnrestrictedView}); the handler also compares the probe with a tenant-scoped count for each tenant it
 * knows. The routes use the probe to find the tenants to check and to refuse before any DDL.
 *
 * <p><b>DATA EFFECT of {@code add}.</b> No row is changed, but a NOT VALID CHECK is evaluated against every new row
 * version, including an UPDATE that touches no metadata: a row that already breaks a constraint can no longer be
 * UPDATEd (SQLSTATE 23514) until it is fixed. The routes refuse an add while the equalities or the chash width are
 * broken anywhere, so what an add freezes is the retired-key rows the sweep deliberately leaves (those in a blocked
 * collection); the response names them. A write carrying a key outside the allow-list, an unequal hash or model, or
 * {@code by} without {@code at} is refused for every row written afterwards. DATA EFFECT of {@code validate}: none; it
 * reads the whole table under SHARE UPDATE EXCLUSIVE, which lets writers through, and fails (leaving the constraint NOT
 * VALID) if a row violates it. {@code replace} on a constraint that is VALID makes it NOT VALID until the next validate.
 *
 * <p><b>Capability flags.</b> The Phase 3 manifest-keyed read flags the sweep depends on are constants in this binary,
 * true by construction wherever this class exists; nothing here reads them from {@code /version}.
 */
public class ChunkConstraintAdmin {

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

    /**
     * What a call did to one constraint, and the state it left. {@code warning} is a consequence the operator should
     * know (a VALID constraint replaced is NOT VALID now); {@code expectedDefinition} is set when an existing
     * constraint's stored definition is not the one this plan would add ({@code present_differs}).
     */
    public record Action(String name, String action, State after, String warning, String expectedDefinition) {
        public Action(String name, String action, State after) {
            this(name, action, after, null, null);
        }

        Action withState(State state) {
            return new Action(name, action, state, warning, expectedDefinition);
        }
    }

    /** One tenant's standing in the table-wide probe, counted on the admin session. */
    public record TenantProbe(String tenant, long rows, long retiredRows, long hashMismatch, long modelMismatch,
                              long wrongWidth, long quarantineBreach) {
        /** True when any constraint count is non-zero. */
        public boolean violatesAny() {
            return retiredRows > 0 || hashMismatch > 0 || modelMismatch > 0 || wrongWidth > 0 || quarantineBreach > 0;
        }
    }

    /** Name of the session-private table a plan's expected definitions are rendered on. */
    private static final String PROBE_TABLE = "chunk_constraint_probe";
    private static final String PROBE_STATEMENT_TIMEOUT = "10min";

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
     * The table-wide probe: every tenant's counts, read in one pass on the admin session, so a tenant absent from
     * {@code service_tokens} is seen. Refuses ({@code Refused} 503, {@code admin_cannot_see_all_tenants}) unless the
     * session is not row filtered: superuser, BYPASSRLS, or a permissive SELECT policy with a {@code true} qual that
     * applies to the role. Per tenant: its rows, the rows outside every bypass prefix that hold a
     * key outside the allow-list, the rows whose {@code chunk_text_hash} or {@code embedding_model} differs from the
     * column, the rows whose chash is not 32 bytes and the rows that break the quarantine implication (the last four over
     * every collection, as the CHECKs apply to all). Tenants with no rows do not appear.
     */
    public List<TenantProbe> probe(ChunkKeyPlan plan) {
        try (Session s = open(false)) {
            s.requireUnrestrictedView();
            s.setSession("statement_timeout", PROBE_STATEMENT_TIMEOUT);
            return s.probe(plan);
        }
    }

    /**
     * Add the constraints NOT VALID. A constraint that exists is left alone unless {@code replace}, which drops and
     * re-adds it in one statement (a changed allow-list). An existing constraint is not reported as simply present: its
     * stored definition is compared with the definition this plan would add (rendered by the server on a session-private
     * table of the same columns), and a difference is reported as {@code present_differs} with the expected text.
     */
    public List<Action> add(ChunkKeyPlan plan, boolean replace) {
        try (Session s = open(true)) {
            s.guard(ADD_STATEMENT_TIMEOUT);
            List<Action> out = new ArrayList<>();
            List<State> before = s.states();
            Map<String, String> expected = null;
            for (String name : List.of(ALLOW_LIST, CHUNK_TEXT_HASH, EMBEDDING_MODEL, QUARANTINE_KEYS)) {
                State existing = stateOf(before, name);
                boolean exists = existing.present();
                if (exists && !replace) {
                    if (expected == null) {
                        expected = s.expectedDefinitions(plan);
                    }
                    String want = expected.get(name);
                    boolean same = sameDefinition(existing.definition(), want);
                    out.add(new Action(name, same ? "present" : "present_differs", null, null, same ? null : want));
                    continue;
                }
                String warning = exists && Boolean.TRUE.equals(existing.validated())
                    ? "was VALID and is now NOT VALID until validate-constraints runs again" : null;
                s.ddl(addStatement(name, plan, exists), args(name, plan, exists, CHUNKS.getSchema().getName(),
                    CHUNKS.getName()));
                out.add(new Action(name, exists ? "replaced" : "added", null, warning, null));
            }
            List<State> after = s.states();
            List<Action> done = new ArrayList<>();
            for (Action a : out) {
                done.add(a.withState(stateOf(after, a.name())));
            }
            log.info("event=chunk_constraints_added actions={}", done.stream().map(a -> a.name() + ":" + a.action())
                .toList());
            return done;
        }
    }

    /** True when two stored definitions are the same CHECK, whether or not either is NOT VALID. */
    static boolean sameDefinition(String stored, String expected) {
        return stored != null && expected != null && stripNotValid(stored).equals(stripNotValid(expected));
    }

    private static String stripNotValid(String def) {
        String suffix = " NOT VALID";
        return def.endsWith(suffix) ? def.substring(0, def.length() - suffix.length()) : def;
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
                done.add(a.after() != null ? a : a.withState(stateOf(after, a.name())));
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

    private static List<Object> args(String name, ChunkKeyPlan plan, boolean replace, String schema, String table) {
        List<Object> a = new ArrayList<>();
        a.add(schema);
        a.add(table);
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

        /**
         * Refuse unless this session reads every row of {@code nexus.chunks}: the role is a superuser or has BYPASSRLS,
         * or a PERMISSIVE policy FOR SELECT (or ALL) with the qual {@code true} applies to it (named, or PUBLIC, or a role
         * it inherits) and no RESTRICTIVE policy for SELECT (or ALL) with any other qual does (nexus-gy3vq: Postgres
         * ANDs every applicable restrictive policy onto the permissive ones, so one that is not {@code true} can hide
         * rows whatever the permissive policy admits). "true" means the qual as pg_policies renders it is the literal
         * {@code true}: an equivalent spelling such as {@code 1 = 1}, or a restrictive policy with no USING qual, is
         * read as narrowing and refused, which fails closed. Anything else reads the rows its (unset) tenant stamp
         * allows, which is none, and the probe would report an empty table as clean.
         */
        void requireUnrestrictedView() {
            String role = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            var attrs = ctx.select(DSL.field(DSL.name("r", "rolsuper"), Boolean.class),
                    DSL.field(DSL.name("r", "rolbypassrls"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_roles")).as("r"))
                .where(DSL.field(DSL.name("r", "rolname"), String.class).eq(role))
                .fetchOne();
            if (attrs != null && (Boolean.TRUE.equals(attrs.value1()) || Boolean.TRUE.equals(attrs.value2()))) {
                return;
            }
            for (var p : selectPolicies("RESTRICTIVE")) {
                if (!isTrue(p.value2()) && appliesToSession(p.value1())) {
                    throw new Refused(503, "admin_cannot_see_all_tenants",
                        "the admin session role " + role + " is narrowed by the RESTRICTIVE policy " + p.value3()
                            + " on nexus.chunks, whose qual is not true, so the table-wide probe would not read every "
                            + "tenant's rows. Point NX_DB_ADMIN_* at a superuser or BYPASSRLS role, or drop the policy");
                }
            }
            for (var p : selectPolicies("PERMISSIVE")) {
                if (isTrue(p.value2()) && appliesToSession(p.value1())) {
                    return;
                }
            }
            throw new Refused(503, "admin_cannot_see_all_tenants",
                "the admin session role " + role + " does not read every tenant's rows of nexus.chunks: it is neither "
                    + "a superuser nor BYPASSRLS, and no permissive SELECT policy with a true qual applies to it, so "
                    + "the table-wide probe would read zero rows and the constraint checks would pass over every "
                    + "tenant. Point NX_DB_ADMIN_* at the role that owns the tables and carries "
                    + "chunks_gate_probe_owner_read (vectors-029), or at a BYPASSRLS role");
        }

        /** The {@code nexus.chunks} policies of {@code kind} (PERMISSIVE or RESTRICTIVE) that govern SELECT: roles, qual, name. */
        private org.jooq.Result<org.jooq.Record3<String[], String, String>> selectPolicies(String kind) {
            return ctx.select(DSL.field(DSL.name("p", "roles"), SQLDataType.CLOB.array()),
                    DSL.field(DSL.name("p", "qual"), String.class),
                    DSL.field(DSL.name("p", "policyname"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_policies")).as("p"))
                .where(DSL.field(DSL.name("p", "schemaname"), String.class).eq(CHUNKS.getSchema().getName()))
                .and(DSL.field(DSL.name("p", "tablename"), String.class).eq(CHUNKS.getName()))
                .and(DSL.field(DSL.name("p", "permissive"), String.class).eq(kind))
                .and(DSL.field(DSL.name("p", "cmd"), String.class).in("SELECT", "ALL"))
                .fetch();
        }

        /** True when a policy's qual, as pg_policies renders it, is the constant true. */
        private static boolean isTrue(String qual) {
            return "true".equalsIgnoreCase(qual == null ? "" : qual.replaceAll("[()\\s]", ""));
        }

        /** True when a policy naming {@code roles} applies to this session (PUBLIC, or a role it is or inherits). */
        private boolean appliesToSession(String[] roles) {
            if (roles == null) {
                return false;
            }
            for (String named : roles) {
                if ("public".equalsIgnoreCase(named) || memberOf(named)) {
                    return true;
                }
            }
            return false;
        }

        /** True when the session role has the privileges of {@code named} (is it, or inherits it). */
        private boolean memberOf(String named) {
            return Boolean.TRUE.equals(ctx.select(DSL.function("pg_has_role", SQLDataType.BOOLEAN, DSL.currentUser(),
                    DSL.cast(DSL.val(named), SQLDataType.VARCHAR), DSL.inline("USAGE"))).fetchOne(0, Boolean.class));
        }

        /** The probe query: one pass over {@code nexus.chunks}, grouped by tenant, typed jOOQ on the admin connection. */
        List<TenantProbe> probe(ChunkKeyPlan plan) {
            var rows = ctx.select(CHUNKS.TENANT_ID, DSL.count(),
                    DSL.count().filterWhere(ChunkKeySweep.notBypass(CHUNKS.COLLECTION, plan).and(ChunkKeySweep.retired(plan))),
                    DSL.count().filterWhere(ChunkKeySweep.hashMismatch()),
                    DSL.count().filterWhere(ChunkKeySweep.modelMismatch()),
                    DSL.count().filterWhere(ChunkKeySweep.width(false)),
                    DSL.count().filterWhere(ChunkKeySweep.quarantineBreach(plan)))
                .from(CHUNKS)
                .groupBy(CHUNKS.TENANT_ID)
                .orderBy(CHUNKS.TENANT_ID)
                .fetch();
            List<TenantProbe> out = new ArrayList<>();
            for (var r : rows) {
                out.add(new TenantProbe(r.get(0, String.class), r.get(1, Long.class), r.get(2, Long.class),
                    r.get(3, Long.class), r.get(4, Long.class), r.get(5, Long.class), r.get(6, Long.class)));
            }
            return out;
        }

        /**
         * The definitions the plan's ADDs produce, as the server prints them: the same statements run on a session-private
         * table with the columns of {@code nexus.chunks} (nothing on the real table is touched), read back from
         * {@code pg_constraint}. The temp table vanishes with the connection.
         */
        Map<String, String> expectedDefinitions(ChunkKeyPlan plan) {
            ddl("CREATE TEMP TABLE %I (LIKE %I.%I)", List.of(PROBE_TABLE, CHUNKS.getSchema().getName(), CHUNKS.getName()));
            Map<String, String> out = new LinkedHashMap<>();
            for (String name : List.of(ALLOW_LIST, CHUNK_TEXT_HASH, EMBEDDING_MODEL, QUARANTINE_KEYS)) {
                ddl(addStatement(name, plan, false), args(name, plan, false, "pg_temp", PROBE_TABLE));
            }
            var rows = ctx.select(DSL.field(DSL.name("con", "conname"), String.class),
                    DSL.function("pg_get_constraintdef", SQLDataType.VARCHAR, DSL.field(DSL.name("con", "oid"))))
                .from(DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con"))
                .where(DSL.field(DSL.name("con", "conrelid")).eq(
                    DSL.function("to_regclass", SQLDataType.OTHER, DSL.val("pg_temp." + PROBE_TABLE))))
                .fetch();
            for (var r : rows) {
                out.put(r.value1(), r.value2());
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
                    "the table lock was not available within " + LOCK_TIMEOUT + ". Earlier constraints of this run "
                        + "may already have landed (a run is several statements, none rolled back); a rerun is safe: "
                        + "add skips a constraint that exists and validate skips one that is valid");
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

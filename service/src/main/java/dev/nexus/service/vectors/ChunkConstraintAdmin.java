// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.AdminDdlSession.State;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

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
 * lock. A refusal is a {@link AdminDdlSession.Refused} the handler answers with its status.
 *
 * <p><b>The table-wide probe.</b> {@link #probe} reads every tenant's counts in one pass on the admin session, so a
 * tenant with chunk rows and no {@code service_tokens} row is seen. It sees them ONLY when the session is not row
 * filtered, and {@code nexus.chunks} is FORCE row-level-security: a role that is neither superuser nor BYPASSRLS reads
 * every row only through a permissive SELECT policy whose qual is {@code true} that applies to it
 * ({@code chunks_gate_probe_owner_read}, vectors-029, which names the MIGRATING role, so it does not reach a different
 * {@code NX_DB_ADMIN_USER} and is skipped in the single-role posture). Without one the probe reads zero rows and nothing
 * looks wrong, so {@link #probe} first checks the posture and refuses with {@code admin_cannot_see_all_tenants}
 * ({@link AdminDdlSession#requireUnrestrictedView}); the handler also compares the probe with a tenant-scoped count for each tenant it
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

    private static final String ADD_STATEMENT_TIMEOUT = "60s";
    private static final String VALIDATE_STATEMENT_TIMEOUT = "30min";

    /** The connection, lock and refusal words of a chunk-constraint run ({@link AdminDdlSession}). */
    private static final AdminDdlSession.Settings SETTINGS = new AdminDdlSession.Settings(
        "nexus-chunk-constraints", ADVISORY_LOCK_KEY, "chunk-constraint", CHUNKS.getSchema().getName(),
        CHUNKS.getName(), "dry run");

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
        return new Session(AdminDdlSession.open(admin, SETTINGS, lock));
    }

    /** One admin session on {@code nexus.chunks}: the shared plumbing plus the chunk table's probe and definitions. */
    private static final class Session implements AutoCloseable {
        private final AdminDdlSession base;
        private final DSLContext ctx;

        Session(AdminDdlSession base) {
            this.base = base;
            this.ctx = base.ctx();
        }

        void guard(String statementTimeout) {
            base.guard(statementTimeout);
        }

        void setSession(String name, String value) {
            base.setSession(name, value);
        }

        List<State> states() {
            return base.states(ALL);
        }

        void ddl(String template, List<Object> args) {
            base.ddl(template, args);
        }

        /**
         * Refuse unless this session reads every row of {@code nexus.chunks}; {@code chunks_gate_probe_owner_read}
         * (vectors-029) is the policy that lets the migrating owner role do so without BYPASSRLS.
         */
        void requireUnrestrictedView() {
            base.requireUnrestrictedView("the role that owns the tables and carries chunks_gate_probe_owner_read "
                + "(vectors-029), or at a BYPASSRLS role");
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

        @Override
        public void close() {
            base.close();
        }
    }
}

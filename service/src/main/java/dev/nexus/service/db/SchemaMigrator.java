package dev.nexus.service.db;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.TimeZone;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Applies the Liquibase master changelog to a target {@link DataSource}.
 *
 * <p>Called from {@code Main.java} after HikariCP pool creation and before
 * {@code NexusService.start()}, so the service never serves requests against
 * an unmigrated database.
 *
 * <p><strong>Idempotency.</strong> Liquibase tracks applied changesets in the
 * {@code DATABASECHANGELOG} table; re-running against an already-migrated
 * database is a verified no-op (zero changesets applied, no DDL issued).
 *
 * <p><strong>Privilege requirement.</strong> The connection borrowed from
 * {@code ds} must have DDL privileges: {@code CREATE SCHEMA}, {@code CREATE
 * TABLE}, {@code ALTER TABLE ... ENABLE ROW LEVEL SECURITY}, and
 * {@code CREATE POLICY}. The {@code nexus_svc} role (NOSUPERUSER NOBYPASSRLS)
 * has only DML rights on the application tables and therefore cannot run
 * migrations. In production the caller must supply a <em>separate</em>
 * migration datasource whose credentials hold schema-owner or superuser
 * rights. {@code Main.java} reads {@code NX_DB_ADMIN_*} variables for this
 * purpose, falling back to the regular {@code NX_DB_*} credentials when they
 * are absent (useful in development / single-role setups where the service
 * role also owns the schema).
 *
 * <p><strong>Phase-5 provisioning note.</strong> When the production
 * deployment uses two roles (schema-owner + service role), the Phase-5 {@code
 * nx} provisioning step must:
 * <ol>
 *   <li>Install extensions as superuser BEFORE the first migration run:
 *       {@code CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT
 *       EXISTS pg_trgm;} Neither is a trusted extension and the schema-owner
 *       role below is NOSUPERUSER, so changeset {@code vectors-001-1} fails
 *       without this DBA pre-step (it becomes an idempotent no-op once the
 *       extensions exist).</li>
 *   <li>Create the schema-owner role (e.g. {@code nexus_admin}) with
 *       {@code CREATE ON DATABASE nexus} and ownership of the {@code nexus}
 *       and {@code t1} schemas.</li>
 *   <li>Create {@code nexus_svc} as a NOSUPERUSER NOBYPASSRLS LOGIN role.</li>
 *   <li>Supply {@code NX_DB_ADMIN_*} credentials as the schema-owner role and
 *       {@code NX_DB_*} credentials as {@code nexus_svc}.</li>
 * </ol>
 * The changelogs' post-DDL grant DO-blocks (changeset suffix {@code -5} in
 * each baseline) then grant DML rights to {@code nexus_svc} automatically
 * during the first migration run.
 *
 * <p>RDR-152 bead nexus-net63.
 */
public final class SchemaMigrator {

    private static final Logger log = LoggerFactory.getLogger(SchemaMigrator.class);

    /** Classpath location of the master changelog bundled in the service jar. */
    static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    private SchemaMigrator() { /* static utility */ }

    /**
     * What a walk actually did, in truthfully named counts (nexus-x0s52).
     *
     * <p>The old {@code schema_migration_complete} line logged the PRE-update
     * pending count under the name {@code applied_changesets}. Measured on the
     * v0.1.86 PITR fork walk (2026-08-27): the line said 12 where 1 genuinely
     * new changeset landed and 25 rows were touched (24 {@code runAlways}
     * re-runs) — the logged number corresponded to NONE of the three
     * quantities an operator might mean by "applied". These three fields are
     * the real ones:
     *
     * @param pendingAtStart  {@code listUnrunChangeSets()} BEFORE the update.
     *                        NOT "new changesets waiting": Liquibase counts the
     *                        {@code runAlways} re-run plan here too (measured:
     *                        11 on a no-op walk of this changelog), which is
     *                        exactly how the old line came to claim 12 applied
     *                        where 1 landed
     * @param newChangesets   {@code databasechangelog} row-count delta across
     *                        the update — changesets that genuinely landed for
     *                        the first time ("did my one changeset land" reads
     *                        THIS field)
     * @param reexecutedChangesets rows whose {@code dateexecuted} moved during
     *                        the walk minus the new rows — the {@code runAlways}
     *                        / {@code runOnChange} re-runs (a clean walk proves
     *                        they executed, not that their content is right)
     */
    public record MigrationOutcome(
            int pendingAtStart, long newChangesets, long reexecutedChangesets) {}

    /**
     * Applies all pending Liquibase changesets from the master changelog to the
     * database reachable via {@code ds}.
     *
     * <p>Borrows one connection from the pool, runs the full
     * {@link Liquibase#update(Contexts, LabelExpression)} call, then closes the
     * connection. The HikariCP pool returns it to the pool; subsequent service
     * requests use it normally.
     *
     * @param ds migration-capable datasource (schema-owner or superuser rights)
     * @return the walk's real counts — also logged as
     *         {@code event=schema_migration_complete}
     * @throws MigrationException if Liquibase fails or the connection cannot be
     *                             obtained; caller should treat this as fatal
     */
    public static MigrationOutcome migrate(DataSource ds) {
        log.info("event=schema_migration_start changelog={}", MASTER_CHANGELOG);
        pinJvmTimeZoneToUtc();

        try (Connection conn = ds.getConnection()) {
            // nexus-rph82: Liquibase stamps databasechangelog.dateexecuted with the
            // SERVER's now() rendered in the connection's SESSION zone — and pgjdbc
            // negotiates that zone from the JVM default at CONNECT time, so a pool
            // opened before the pin above still carries the old zone. Pin the
            // session too; the JVM pin covers client-side formatting, this covers
            // the stamp itself, and together they hold for every entry point.
            try (var st = conn.createStatement()) {
                st.execute("SET TIME ZONE 'UTC'");
            }
            preflightChashConstraints(conn);

            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));

            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG,
                    new ClassLoaderResourceAccessor(),
                    database)) {

                // Count pending changesets for the structured log entry.
                int pending = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression()).size();
                log.info("event=schema_migration_pending changesets={}", pending);

                // nexus-x0s52: capture the changelog's pre-walk state so the
                // completion line can report what the walk actually DID, not
                // the pre-update plan under a name promising a result. Row
                // count is -1 on first boot (table not created yet); walkStart
                // is the server's own clock in this UTC-pinned session, the
                // same clock Liquibase stamps dateexecuted from (nexus-rph82).
                long rowsBefore = countChangelogRows(conn);
                java.sql.Timestamp walkStart = serverNow(conn);

                liquibase.update(new Contexts(), new LabelExpression());

                long rowsAfter = countChangelogRows(conn);
                long newChangesets = rowsBefore < 0
                        ? Math.max(rowsAfter, 0)
                        : rowsAfter - rowsBefore;
                long touched = countChangelogRowsSince(conn, walkStart);
                long reexecuted = Math.max(0, touched - newChangesets);
                // The old line logged the PRE-update pending count as
                // applied_changesets — a quantity the walk never computed
                // (12x overstatement measured on the v0.1.86 fork walk). The
                // misleading field name is deliberately GONE, not repaired in
                // place: a deploy grep for it should find nothing and force a
                // read of the real fields, never silently match new semantics.
                log.info("event=schema_migration_complete new_changesets={} "
                        + "reexecuted_changesets={} pending_at_start={}",
                        newChangesets, reexecuted, pending);
                return new MigrationOutcome(pending, newChangesets, reexecuted);
            }

        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed", e);
        }
    }

    // ── nexus-x0s52: truthful walk counts ────────────────────────────────────
    // Unqualified table references, deliberately: these run on the SAME
    // connection Liquibase itself uses, so they resolve to exactly the
    // databasechangelog Liquibase reads and writes.

    /** Rows in {@code databasechangelog}, or -1 when the table does not exist
     * yet (first boot — Liquibase creates it during the update). */
    private static long countChangelogRows(Connection conn) throws SQLException {
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT to_regclass('databasechangelog')")) {
            rs.next();
            if (rs.getString(1) == null) {
                return -1L;
            }
        }
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT count(*) FROM databasechangelog")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    /** Rows whose {@code dateexecuted} is at or after {@code since} — every row
     * this walk touched (new rows plus {@code runAlways}/{@code runOnChange}
     * re-stamps). Valid because the session and JVM are both UTC-pinned
     * (nexus-rph82), so the stamp and the comparison share one clock. */
    private static long countChangelogRowsSince(Connection conn, java.sql.Timestamp since)
            throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT count(*) FROM databasechangelog WHERE dateexecuted >= ?")) {
            ps.setTimestamp(1, since);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /** The server's own clock as a session-zone (UTC) timestamp — the same
     * clock Liquibase stamps {@code dateexecuted} from. */
    private static java.sql.Timestamp serverNow(Connection conn) throws SQLException {
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT now()::timestamp")) {
            rs.next();
            return rs.getTimestamp(1);
        }
    }

    /**
     * The five {@code length(chash)=32} CHECK constraints (catalog-002-hygiene.xml
     * + catalog-013-1) and their owning table, in {@code nexus} schema.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 item 6 /
     * bead nexus-o8dil.43's F14c concern): VERIFIED NO CHANGE NEEDED here despite
     * {@code nexus.chunks_384/768/1024} collapsing into the unified {@code
     * nexus.chunks} table. The three {@code chunks_<dim>_chash_len_check}
     * entries below already reference constraint NAMES dropped by rdr180-2 —
     * true before RDR-191 and unchanged after it, since {@link
     * #preflightChashConstraints} probes by CONSTRAINT NAME
     * ({@code pg_constraint WHERE conname = ?}), not by whether the owning
     * TABLE exists. {@code nexus.chunks} is bytea-chash from creation and has
     * no {@code length(text)=32} concept at all (only the separate octet
     * family applies, added {@code NOT VALID} by {@code
     * vectors-004-unify-chunks.xml} step 4) — so this preflight's per-entry
     * lookup returns "not present" for the three {@code chunks_<dim>} rows
     * exactly as it did pre-repoint, a no-op either way. See that changeset's
     * own "LEN-CHECK FAMILY DISPOSITION" header note for the original
     * verification this comment reaffirms.
     */
    private static final Map<String, String> CHASH_LEN_CONSTRAINTS = new LinkedHashMap<>();
    static {
        CHASH_LEN_CONSTRAINTS.put("chunks_384_chash_len_check", "chunks_384");
        CHASH_LEN_CONSTRAINTS.put("chunks_768_chash_len_check", "chunks_768");
        CHASH_LEN_CONSTRAINTS.put("chunks_1024_chash_len_check", "chunks_1024");
        CHASH_LEN_CONSTRAINTS.put("catalog_document_chunks_chash_len_check", "catalog_document_chunks");
        // chash_index entry KEPT past the RDR-187 DROP — deliberately, and
        // contrary to the .5 pin's first reading: an AGED box upgrading to
        // head still crosses catalog-013 (VALIDATE) with the table present
        // en route to the rdr187-2 drop, and a genuinely-violating row there
        // must fail CLEAN at this preflight (named table/count/runbook), not
        // crash-loop at VALIDATE. The preflight is existence-gated, so on
        // modern boxes (constraint validated) and post-drop boxes (table
        // gone) it is a no-op. Pinned by SchemaMigratorIntegrationTest's
        // presentButViolating scenario.
        CHASH_LEN_CONSTRAINTS.put("chash_index_chash_len_check", "chash_index");
    }

    /**
     * nexus-c4143 (root fix): probe for present-but-VIOLATING chash-length
     * constraints BEFORE invoking Liquibase, and fail clean instead of letting
     * catalog-013-2/-3's bare {@code VALIDATE CONSTRAINT} crash-loop.
     *
     * <p>Tests 5/6/8 in {@code SchemaMigratorIntegrationTest} (ms57z / GH#1390,
     * nexus-4m6i0.1/.13) cover a constraint that is MISSING when the VALIDATE
     * changesets first run — the defensive {@code IF EXISTS} guards tolerate that
     * case. This preflight covers the OPPOSITE condition those guards do not
     * help with: the constraint EXISTS (added {@code NOT VALID}) but at least one
     * row genuinely violates it. A bare {@code VALIDATE CONSTRAINT} on a
     * genuinely-violating row is a hard Postgres ERROR regardless of any
     * {@code IF EXISTS} guard around it — same crash-loop mechanism, narrower
     * trigger condition.
     *
     * <p>Only constraints that EXIST and are NOT YET {@code convalidated} are
     * checked: an already-VALID constraint has already been proven, and a
     * missing one is handled separately (and correctly) by the defensive
     * per-table guards already shipped in catalog-013-3 / fk-002-7..11 /
     * fk-003-7..11. On a fresh, not-yet-migrated database none of these
     * constraints exist yet, so every check is a cheap no-op — this preflight
     * costs nothing on the common (happy) path.
     *
     * <p>Violation counting temporarily disables {@code FORCE ROW LEVEL
     * SECURITY} on the affected table (mirroring catalog-013-1b's own pattern)
     * so the count is TRUE regardless of RLS — closing the EXACT visibility gap
     * that caused the 2026-07-08 v0.1.33 production incident (nexus-1wjmq): the
     * migration role is the table owner but holds no BYPASSRLS, so a plain
     * {@code SELECT}/{@code DELETE}/{@code UPDATE} under FORCE RLS silently sees
     * zero rows while the subsequent {@code VALIDATE} (a physical scan, RLS-exempt
     * for DDL) still finds and crashes on the true violating rows. The toggle
     * happens on the SAME migration connection this method already holds
     * schema-owner rights on ({@code ds} is documented as
     * migration-capable/schema-owner), so no additional privilege is required.
     *
     * @throws MigrationException with the violating table/constraint/count named
     *     directly (so an operator does not need to reproduce the RLS-blind
     *     diagnostic dead-end the 2026-07-08 incident hit), or wrapping a genuine
     *     {@link SQLException} from the preflight query itself
     */
    // SANCTIONED RAW (nexus-mzuj9): two of this method's three query shapes have no
    // jOOQ typed-DSL form at all -- (1) pg_constraint is a Postgres SYSTEM CATALOG;
    // jOOQ codegen (service/pom.xml) only covers the nexus/t1 APPLICATION schemas, not
    // pg_catalog, so there is no generated table/field to select against; (2) ALTER
    // TABLE ... {NO} FORCE ROW LEVEL SECURITY is DDL with no jOOQ DSL equivalent
    // whatsoever (jOOQ does not model RLS toggles). The third shape (a per-table
    // SELECT COUNT(*) WHERE length(chash)!=32) COULD be expressed via the generated
    // table references, but is sanctioned as part of the SAME method rather than
    // split out: it runs bracketed between the FORCE-toggle DDL pair inside one
    // logical unit (mirrors the existing PgVectorRepository.rawVectorFetch /
    // TaxonomyCentroidRepository.annQuery precedent of sanctioning a whole method
    // rather than fragmenting a tightly-coupled raw-SQL sequence).
    private static void preflightChashConstraints(Connection conn) {
        List<String> violations = new ArrayList<>();
        try {
            for (Map.Entry<String, String> entry : CHASH_LEN_CONSTRAINTS.entrySet()) {
                String constraint = entry.getKey();
                String table = entry.getValue();

                boolean existsNotValid;
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT NOT convalidated FROM pg_constraint WHERE conname = ?")) {
                    ps.setString(1, constraint);
                    try (ResultSet rs = ps.executeQuery()) {
                        existsNotValid = rs.next() && rs.getBoolean(1);
                    }
                }
                if (!existsNotValid) {
                    continue;
                }

                long violatingCount;
                boolean autoCommit = conn.getAutoCommit();
                conn.setAutoCommit(false);
                try {
                    try (Statement alter = conn.createStatement()) {
                        alter.execute("ALTER TABLE nexus." + table + " NO FORCE ROW LEVEL SECURITY");
                    }
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT COUNT(*) FROM nexus." + table + " WHERE length(chash) != 32");
                         ResultSet rs = ps.executeQuery()) {
                        rs.next();
                        violatingCount = rs.getLong(1);
                    }
                    try (Statement alter = conn.createStatement()) {
                        alter.execute("ALTER TABLE nexus." + table + " FORCE ROW LEVEL SECURITY");
                    }
                    conn.commit();
                } catch (SQLException e) {
                    // Postgres DDL is transactional: an uncommitted NO FORCE rolls back
                    // with everything else, so a mid-block failure leaves FORCE RLS
                    // exactly as it was found -- no separate restore step needed.
                    conn.rollback();
                    throw e;
                } finally {
                    conn.setAutoCommit(autoCommit);
                }

                if (violatingCount > 0) {
                    violations.add(table + " (" + constraint + "): " + violatingCount + " violating row(s)");
                    log.error(
                        "event=chash_preflight_violation table={} constraint={} count={}",
                        table, constraint, violatingCount);
                }
            }
        } catch (SQLException e) {
            throw new MigrationException("chash-length preflight query failed", e);
        }

        if (!violations.isEmpty()) {
            throw new MigrationException(
                "chash-length preflight found present-but-violating constraint(s) — refusing to run "
                + "Liquibase (would crash-loop on VALIDATE CONSTRAINT): " + String.join("; ", violations)
                + ". Remediate the violating rows per "
                + "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
                + "#81-recovering-a-store-that-already-migrated-legacy-ids-nexus-pnwu0 before retrying.",
                null);
        }
    }

    /** The one zone this service's clocks agree on. */
    static final String UTC_ID = "UTC";

    /**
     * Pin the JVM default time zone to UTC (nexus-rph82).
     *
     * <p>Liquibase writes {@code databasechangelog.dateexecuted} (a
     * {@code TIMESTAMP WITHOUT TIME ZONE}) in the JVM's default zone. The
     * managed database runs GMT and every post-deploy audit windows that
     * column against {@code now()}, so a JVM-local write from a box seven
     * hours behind reads as seven hours in the past and the audit reports
     * "nothing was applied" for a walk that applied everything — measured
     * 2026-08-27 on a PITR fork of production (conexus-a4, v0.1.86). The
     * failure points the wrong way (towards a spurious rollback or re-run),
     * which is why it is fixed at the source rather than documented.
     *
     * <p>Lives here, not only in {@code Main}, so every entry point that runs
     * the changelog (the service, the migration rehearsals, the test suite)
     * gets the same clock. Idempotent; logs the transition when it happens.
     * {@code Main} also pins it before any datasource is built, because a
     * pooled connection negotiates its session zone at connect time.
     */
    public static void pinJvmTimeZoneToUtc() {
        TimeZone before = TimeZone.getDefault();
        if (!UTC_ID.equals(before.getID())) {
            TimeZone.setDefault(TimeZone.getTimeZone(UTC_ID));
            System.setProperty("user.timezone", UTC_ID);
            log.info("event=schema_migration_jvm_timezone_pinned from={} to={}", before.getID(), UTC_ID);
        }
    }

    /**
     * Unchecked exception thrown when {@link #migrate(DataSource)} cannot
     * complete. {@code Main.java} catches this and calls {@code System.exit(1)}.
     */
    public static final class MigrationException extends RuntimeException {
        public MigrationException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}

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
     * Applies all pending Liquibase changesets from the master changelog to the
     * database reachable via {@code ds}.
     *
     * <p>Borrows one connection from the pool, runs the full
     * {@link Liquibase#update(Contexts, LabelExpression)} call, then closes the
     * connection. The HikariCP pool returns it to the pool; subsequent service
     * requests use it normally.
     *
     * @param ds migration-capable datasource (schema-owner or superuser rights)
     * @throws MigrationException if Liquibase fails or the connection cannot be
     *                             obtained; caller should treat this as fatal
     */
    public static void migrate(DataSource ds) {
        log.info("event=schema_migration_start changelog={}", MASTER_CHANGELOG);

        try (Connection conn = ds.getConnection()) {
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

                liquibase.update(new Contexts(), new LabelExpression());

                log.info("event=schema_migration_complete applied_changesets={}", pending);
            }

        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed", e);
        }
    }

    /**
     * The five {@code length(chash)=32} CHECK constraints (catalog-002-hygiene.xml
     * + catalog-013-1) and their owning table, in {@code nexus} schema.
     */
    private static final Map<String, String> CHASH_LEN_CONSTRAINTS = new LinkedHashMap<>();
    static {
        CHASH_LEN_CONSTRAINTS.put("chunks_384_chash_len_check", "chunks_384");
        CHASH_LEN_CONSTRAINTS.put("chunks_768_chash_len_check", "chunks_768");
        CHASH_LEN_CONSTRAINTS.put("chunks_1024_chash_len_check", "chunks_1024");
        CHASH_LEN_CONSTRAINTS.put("catalog_document_chunks_chash_len_check", "catalog_document_chunks");
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
                boolean toggledForceOff = false;
                try {
                    try (Statement alter = conn.createStatement()) {
                        alter.execute("ALTER TABLE nexus." + table + " NO FORCE ROW LEVEL SECURITY");
                    }
                    toggledForceOff = true;
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT COUNT(*) FROM nexus." + table + " WHERE length(chash) != 32");
                         ResultSet rs = ps.executeQuery()) {
                        rs.next();
                        violatingCount = rs.getLong(1);
                    }
                } finally {
                    if (toggledForceOff) {
                        try (Statement alter = conn.createStatement()) {
                            alter.execute("ALTER TABLE nexus." + table + " FORCE ROW LEVEL SECURITY");
                        }
                    }
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
                + ". Remediate the violating rows (see catalog-013 runbook) before retrying.",
                null);
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

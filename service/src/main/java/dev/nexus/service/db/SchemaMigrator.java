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
import java.sql.SQLException;

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
     * Unchecked exception thrown when {@link #migrate(DataSource)} cannot
     * complete. {@code Main.java} catches this and calls {@code System.exit(1)}.
     */
    public static final class MigrationException extends RuntimeException {
        public MigrationException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.SQLException;


/**
 * Shared factory for per-class Testcontainers PostgreSQL containers.
 *
 * <p>RDR-155 P1.0 (nexus-22man): replaces io.zonky EmbeddedPostgres throughout the
 * service test suite.  Each test class creates its own container via {@link #start()}
 * (PER_CLASS lifecycle mirrors the previous EmbeddedPostgres.builder().start() pattern).
 *
 * <p>The image is {@code pgvector/pgvector:pg17} declared compatible with {@code postgres},
 * which allows PostgreSQLContainer to perform its normal wait-strategy and connection
 * checks.  The container runs stock PostgreSQL 17 with the pgvector extension available
 * but not yet loaded — {@code CREATE EXTENSION vector} lands in a later bead (nexus-mf447).
 */
public final class PgContainerHelper {

    /** Image used for all service-module test containers. */
    public static final String IMAGE = "pgvector/pgvector:pg17";

    /** Superuser database name (matches io.zonky default). */
    public static final String DATABASE = "postgres";

    /** Superuser username (matches io.zonky default). */
    public static final String USERNAME = "postgres";

    /** Superuser password. */
    public static final String PASSWORD = "postgres";

    /**
     * Production service role (NOSUPERUSER NOBYPASSRLS) — the credential the app layer
     * should run under in tests so it is subject to the same RLS as production, rather
     * than the BYPASSRLS superuser (nexus-5j7pb). The role is created by each test's
     * startAll() and granted DML by the grants-nexus-svc.xml changeset.
     */
    public static final String SVC_USERNAME = "nexus_svc";
    /** Password for {@link #SVC_USERNAME}. */
    public static final String SVC_PASSWORD = "nexus_svc_pass";

    private PgContainerHelper() {}

    /**
     * Create a CONFIGURED, UNSTARTED container (nexus-1hj1d).
     *
     * <p>The single hardened boot recipe — every test boot path (this
     * helper's {@link #start()} AND the network-attached raw boot in
     * {@code PgBouncerTenantIsolationTest}) must construct through here or
     * the SSL-handshake startup flake survives in the bypassing path:
     *
     * <ul>
     *   <li>{@code sslmode=disable}: the flake signature was the JDBC
     *       startup probe failing "setting up the SSL connection" for the
     *       whole 120s window against a restarting/half-up server under
     *       Docker pressure (pg JDBC defaults to sslmode=prefer; a local
     *       throwaway container needs no TLS). The testcontainers default
     *       wait strategy ALREADY waits for the ready line twice, so the
     *       classic initdb-restart fix is a no-op here — the URL param is
     *       the load-bearing change, propagating via getJdbcUrl() to every
     *       pool and createConnection call.</li>
     *   <li>{@code withStartupAttempts(3)}: native belt — a genuinely
     *       failed startup recreates the whole container (fresh initdb)
     *       instead of flaking the class.</li>
     * </ul>
     *
     * <p>Returns a {@link FailFastPostgreSQLContainer} (nexus-soqa8, hardening (c) of
     * nexus-lgdy1): a foreign server squatting the published port — e.g. a leaked host
     * Postgres from the atexit-only-teardown class of bug — rejects the container's
     * credentials with a deterministic auth SQLSTATE, and this fails in one attempt
     * instead of burning the full ~120s connect-retry budget on a connection that will
     * never succeed. See {@link FailFastPostgreSQLContainer}'s class doc for the full
     * mechanism.
     */
    public static PostgreSQLContainer<?> newContainer() {
        return new FailFastPostgreSQLContainer(
            DockerImageName.parse(IMAGE).asCompatibleSubstituteFor("postgres"))
            .withDatabaseName(DATABASE)
            .withUsername(USERNAME)
            .withPassword(PASSWORD)
            .withUrlParam("sslmode", "disable")
            .withStartupAttempts(3);
    }

    /**
     * System property opt-out for the nexus-yhmav shared-cluster reuse below
     * ({@code -Dnexus.test.pg.shared=false}). Debugging escape hatch only -- the
     * default (unset / any value other than the literal {@code "false"}) is shared
     * reuse ON, since it is a strict subset of what a fresh container already
     * guarantees (see {@link SharedCluster}'s javadoc for the safety argument).
     */
    private static final String SHARED_PROPERTY = "nexus.test.pg.shared";

    /**
     * Create and start a container.
     *
     * <p><b>nexus-yhmav (per-fork container reuse):</b> by default this returns a
     * {@link SharedDatabaseHandle} onto a fresh, already-migrated database cloned
     * from a per-fork shared cluster (one real container boot per surefire fork,
     * not one per class) -- see {@link SharedCluster} for the full design and the
     * safety argument. Set {@code -Dnexus.test.pg.shared=false} to fall back to the
     * pre-yhmav behavior (a genuinely fresh container every call), e.g. while
     * debugging a suspected cross-class interference report.
     *
     * <p>Replaces {@code EmbeddedPostgres.builder().start()}.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> start() {
        if ("false".equals(System.getProperty(SHARED_PROPERTY))) {
            return startDedicated();
        }
        try {
            return SharedCluster.acquireDatabase();
        } catch (SQLException e) {
            throw new IllegalStateException("nexus-yhmav shared-cluster acquire failed", e);
        }
    }

    /**
     * Always boots a genuinely fresh, independently-managed container -- the
     * explicit opt-out from {@link #start()}'s shared-cluster reuse.
     *
     * <p>Required by any class whose test asserts on the migration PROCESS itself
     * (not just an already-migrated schema's shape) -- ownership of relations
     * created "from scratch" by a non-superuser admin role, changeset-count deltas
     * across successive {@code migrate()} calls, rollback depth/order, or a bare
     * (non-{@code IF NOT EXISTS}) {@code CREATE ROLE} that would collide against a
     * cluster another class already bootstrapped. Roster, as of nexus-yhmav
     * (2026-08-09), each confirmed via a hardcoded {@code GRANT CREATE ON DATABASE
     * postgres} and/or a bare {@code CREATE ROLE} in a repo-wide sweep:
     * <ul>
     *   <li>{@code SchemaMigratorIntegrationTest} -- two-phase DBA-then-Liquibase
     *       provisioning from an unmigrated cluster; "aged box" divergence tests
     *       that must inject a defect BEFORE a changeset first executes.</li>
     *   <li>{@code SchemaRollbackRoundTripIntegrationTest} -- rollback-to-zero and
     *       {@code runAlways}-changeset execution-order assertions that depend on a
     *       specific, fresh single-pass apply history.</li>
     *   <li>{@code SchemaUpgradeRehearsalIntegrationTest} -- old-changelog-tree to
     *       HEAD upgrade rehearsal; asserts changeset counts applied by each leg.</li>
     *   <li>{@code GrantsSvcForeignOwnedRelationTest} -- GH #1402 replay: the
     *       non-superuser admin role must OWN every relation it creates, which
     *       requires it to create them all itself against a virgin database.</li>
     *   <li>{@code ServiceTokenScopeBackfillTest} -- hand-builds the PRE-003
     *       {@code nexus.service_tokens} shape and applies ONLY {@code
     *       service-tokens-003-scope-column.xml} (not the master changelog) to assert
     *       backfill behavior; a template already migrated to HEAD collides with the
     *       bare {@code CREATE TABLE} this test uses to reconstruct that pre-migration
     *       state.</li>
     *   <li>{@code ServiceIntegrationTest} -- a deliberately minimal "skeleton" harness
     *       that documents (in its own {@code @BeforeAll} comment) that it does NOT run
     *       the master changelog; a shared, already-fully-migrated cluster would
     *       silently contradict that hermeticity premise even where column shapes
     *       happen to overlap.</li>
     * </ul>
     *
     * <p>{@code PgBouncerTenantIsolationTest} (network-attached, multi-container)
     * is ALREADY excluded from shared-cluster reuse without needing this method --
     * it calls {@link #newContainer()} directly and attaches its own
     * {@code Network}/{@code withNetworkAliases} before starting, bypassing
     * {@link #start()} entirely.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> startDedicated() {
        PostgreSQLContainer<?> c = newContainer();
        c.start();
        return c;
    }


    /**
     * Return a pooled superuser DataSource.
     *
     * <p>Replaces {@code pg.getPostgresDatabase()} when used as a {@code DataSource} argument.
     * Returns {@link HikariDataSource} (not the {@code DataSource} interface) so the caller
     * MUST close it — unlike {@code EmbeddedPostgres.getPostgresDatabase()}, this pool is not
     * owned by the container lifecycle (review nexus-22man: close it in teardown / TWR).
     */
    public static HikariDataSource superuserDataSource(PostgreSQLContainer<?> c) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(c.getUsername());
        cfg.setPassword(c.getPassword());
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.HashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;


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

    /**
     * Grants a test-local service role the SAME schema access production
     * {@code nexus_svc} gets — {@code nexus} DML/sequences PLUS {@code
     * staging} DML (nexus-kl2z6 increment 2, nexus-vc6dh). Call AFTER the
     * master changelog has run (needs both schemas to exist) and BEFORE
     * building the role's own {@code DataSource}.
     *
     * <p><b>Why this exists</b>: {@link
     * dev.nexus.service.db.CatalogRepository#stagingHasRowsForTenant}
     * reads {@code staging.document_chunks} inside EVERY sweep
     * transaction now, unconditionally — a test-local role that only
     * mirrors the OLD (pre-kl2z6) minimal {@code nexus}-only grant set
     * fails that read with {@code permission denied for schema staging},
     * which the sweep's fail-open discipline (by design, correctly)
     * swallows into a silent {@code sweep_skipped}/{@code
     * reason=sweep_failed} outcome rather than a loud test error —
     * exactly the kind of masked signal nexus-vc6dh exists to keep
     * honest. Confirmed NOT a production gap: production's {@code
     * NX_DB_USER} is {@code nexus_svc} (default, {@code
     * src/nexus/db/pg_provision.py}), and {@code nexus_svc} already gets
     * this exact staging grant set from {@code staging-001-landing-
     * tables.xml}'s {@code staging-4-svc-grants} changeset ({@code
     * runAlways="true"}, unconditionally in the master changelog) — this
     * helper exists purely because hand-rolled test-local roles
     * (predating nexus-kl2z6) never mirrored that changeset. Found
     * independently by two test classes
     * ({@code CatalogManifestSweepRepositoryTest},
     * {@code CatalogHandlerSweepAndChashesManyTest}) before this helper
     * centralized the fix; a THIRD ({@code CombinedWriteRepositoryTest})
     * carried the same latent gap without yet tripping over it.
     *
     * @param su      superuser connection (the role owner / grantor)
     * @param svcRole the test-local service role name to grant
     * @deprecated (nexus-cbo4a batch 1a) — delegates to {@link
     *     #bootstrapServiceRole(Connection, String, String)} using this
     *     class's own {@link #SVC_PASSWORD} as the role's password, since
     *     this 2-arg signature has no password parameter of its own. Every
     *     existing caller already creates {@code svcRole} (or relies on it
     *     already existing) before invoking this method with SOME password —
     *     {@code bootstrapServiceRole}'s own {@code CREATE ROLE ... IF NOT
     *     EXISTS} guard is then a no-op and only the grants below actually
     *     execute, exactly as before. Kept only so this method's 7 existing
     *     call sites keep compiling unchanged; new callers should call
     *     {@code bootstrapServiceRole} directly with their own role/password.
     */
    @Deprecated
    public static void grantServiceSchemaAccess(Connection su, String svcRole) throws Exception {
        bootstrapServiceRole(su, svcRole, SVC_PASSWORD);
    }

    /**
     * Run the PRODUCT master changelog ({@code db/changelog/db.changelog-master.xml})
     * against {@code su} — the single place every test class's own hand-rolled
     * {@code new Liquibase("db/changelog/db.changelog-master.xml", ...)} call
     * used to live (nexus-cbo4a batch 1a). Creates the {@code nexus}/{@code staging}
     * schemas and every product table, and — via {@code role-001-nexus-svc.xml}, the
     * FIRST include in the master changelog — creates the {@code nexus_svc} role
     * itself if it does not already exist, so callers never need to pre-create it by
     * hand before this call (see {@link SharedCluster}'s template-bootstrap comment
     * for the same finding, made independently for the shared-cluster path).
     *
     * @param su superuser connection to run the migration under
     */
    public static void applyProductSchema(Connection su) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(), db);
        liquibase.update(new Contexts());
    }

    /**
     * Bootstrap a test-local service role via the {@code db/changelog-test/
     * db.changelog-test-role.xml} test changelog (nexus-cbo4a batch 1a) — replaces
     * the hand-rolled DO-block {@code CREATE ROLE}, schema/table/sequence
     * {@code GRANT}s, and {@code ALTER ROLE ... SET search_path} that 84 test
     * classes used to copy by hand. Creates {@code svcRole} (LOGIN, NOSUPERUSER,
     * NOBYPASSRLS) if absent, redundantly/idempotently ensures {@code nexus_svc}
     * exists too (see {@link #applyProductSchema}'s javadoc — always a no-op here
     * in practice), grants {@code svcRole} the same {@code nexus}+{@code staging}
     * DML/sequence access {@link #grantServiceSchemaAccess} used to hand-grant, and
     * sets {@code svcRole}'s {@code search_path}.
     *
     * <p><b>Call AFTER {@link #applyProductSchema}</b> — the {@code GRANT ... ON ALL
     * TABLES}/{@code ON ALL SEQUENCES} statements inside the test changelog require
     * the {@code nexus}/{@code staging} schemas and their tables to already exist.
     *
     * @param su      superuser connection (the role owner / grantor)
     * @param svcRole the test-local service role name to create and grant
     * @param svcPass the password for {@code svcRole}
     */
    public static void bootstrapServiceRole(Connection su, String svcRole, String svcPass) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog-test/db.changelog-test-role.xml", new ClassLoaderResourceAccessor(), db);
        Map<String, Object> params = new HashMap<>();
        params.put("svcRole", svcRole);
        params.put("svcPass", svcPass);
        for (var entry : params.entrySet()) {
            liquibase.setChangeLogParameter(entry.getKey(), entry.getValue());
        }
        liquibase.update(new Contexts());
    }

    /**
     * Seed one {@code nexus.service_tokens} row via generated jOOQ DSL (nexus-cbo4a
     * batch 1a) — replaces the hand-rolled {@code INSERT INTO nexus.service_tokens
     * (token_hash, tenant_id, label) VALUES (...) ON CONFLICT (token_hash) DO
     * NOTHING} six test classes used to build by string concatenation. The raw
     * token is hashed via {@link TokenHashing#sha256Hex}, matching production's own
     * issuance path exactly.
     *
     * @param dsl    a {@link DSLContext} over the same connection/role the schema
     *               was migrated under (e.g. {@code DSL.using(su, SQLDialect.POSTGRES)})
     * @param token  the raw bearer token to hash and store
     * @param tenant the tenant id to bind the token to
     * @param label  the token's {@code service_tokens.label} value
     */
    public static void seedServiceToken(DSLContext dsl, String token, String tenant, String label) {
        dsl.insertInto(SERVICE_TOKENS)
            .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
            .values(TokenHashing.sha256Hex(token), tenant, label)
            .onConflictDoNothing()
            .execute();
    }
}

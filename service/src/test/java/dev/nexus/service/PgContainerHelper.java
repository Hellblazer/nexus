// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.test.Routines;
import org.jooq.DSLContext;
import org.jooq.Name;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
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
     * <p><b>Leaves {@code su} with {@code autoCommit(true)} restored</b> (review
     * finding, nexus-cbo4a batch 1a follow-up): Liquibase manages its own
     * changeset-boundary commits and disables the connection's autoCommit while
     * {@code update()} runs, but does NOT restore it afterward. A caller that
     * issues a further raw statement on this SAME connection after this method
     * returns (e.g. a hand-kept {@code GRANT}) would otherwise run it inside an
     * open transaction that is silently rolled back when {@code su} closes —
     * exactly the bug {@link #bootstrapServiceRole}'s own note below documents.
     * Restoring it here, once, means every caller gets the fix for free instead
     * of re-adding {@code su.setAutoCommit(true)} at each call site.
     *
     * @param su superuser connection to run the migration under
     */
    public static void applyProductSchema(Connection su) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(), db);
        // nexus-cbo4a batch 9 item 0 (Sam's directive, 2026-09-05; REDESIGNED per T2
        // nexus/critique-nexus-cbo4a-batch-9-search-path): the master changelog's own
        // search-path-001-relocate-vector-extensions.xml GUARDS that vector/pg_trgm
        // are relocated into the nexus schema, attempting the ALTER EXTENSION
        // statements directly (its first-tier fallback) when they are not already
        // relocated. That direct attempt succeeds here because `su` IS a superuser
        // connection and superuser bypasses ownership checks entirely (same as the
        // jOOQ codegen plugin's own Testcontainers bootstrap, which runs this exact
        // changelog the same way) -- no separate Java-side relocation call, and no
        // SECURITY DEFINER helper function, is needed for this superuser-driven
        // walk. See that changeset's own header for the full three-tier derivation
        // (the other two tiers exist for a NOSUPERUSER migrating role, which `su`
        // here is not).
        liquibase.update(new Contexts());
        installTestObjects(su);
    }

    /**
     * Install the {@code nexus_test.*} schema objects (nexus-cbo4a batch 12) —
     * hoisted out of {@link #applyProductSchema} so a caller that migrates the
     * PRODUCT changelog a different way (e.g. via {@code SchemaMigrator.migrate}
     * as the real non-superuser migrating role, rather than this class's own
     * superuser-driven {@code applyProductSchema}) can still install the
     * {@code drop_constraint}/{@code add_fk_not_valid}/{@code
     * add_fk_not_valid_composite3}/{@code set_force_rls}/{@code
     * grant_execute_on_function} test-lifecycle functions {@link #dropConstraint}
     * and friends need. {@code nexus_test} is entirely separate from
     * {@code nexus}/{@code staging}/{@code public} and inert with respect to
     * product-schema migration testing — installing it changes nothing the
     * product changelog walk can observe. Never part of the product changelog;
     * the codegen-time counterpart is db.changelog-test-master.xml (see the
     * pom's generate-jooq-test-sources execution).
     *
     * <p><b>ORDERING / OWNERSHIP CONTRACT (found the hard way in batch 12):</b>
     * whichever role's Liquibase run creates {@code databasechangelog} first OWNS
     * it. A test that later migrates the product changelog as a NON-superuser
     * migrating role (SchemaMigratorIntegrationTest's aged-box walks) must call
     * this through THAT role's own connection, never the superuser's, or the
     * walk fails with "permission denied for table databasechangelog". Callers
     * that migrate via {@link #applyProductSchema} (superuser throughout) can
     * pass the same superuser connection, which is what {@code applyProductSchema}
     * itself does. Pass the connection of the role that will run the product
     * changelog on this database, whichever that is.
     *
     * @param conn connection of the role that will migrate the product changelog
     *             on this database (the superuser for applyProductSchema-driven
     *             tests; the migrating role for SchemaMigrator-driven tests)
     */
    public static void installTestObjects(Connection conn) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
        new Liquibase("db/changelog-test/db.changelog-test-objects.xml",
            new ClassLoaderResourceAccessor(), db).update(new Contexts());
        conn.setAutoCommit(true);
    }

    /**
     * Bootstrap a test-local service role via the {@code db/changelog-test/
     * db.changelog-test-role.xml} test changelog (nexus-cbo4a batch 1a) — replaces
     * the hand-rolled DO-block {@code CREATE ROLE} and schema/table/sequence
     * {@code GRANT}s that 84 test classes used to copy by hand. Creates
     * {@code svcRole} (LOGIN, NOSUPERUSER, NOBYPASSRLS) if absent,
     * redundantly/idempotently ensures {@code nexus_svc} exists too (see
     * {@link #applyProductSchema}'s javadoc — always a no-op here in practice), and
     * grants {@code svcRole} the same {@code nexus}+{@code staging} DML/sequence
     * access {@link #grantServiceSchemaAccess} used to hand-grant. Deliberately does
     * NOT set {@code svcRole}'s {@code search_path} (nexus-cbo4a batch 9 item 1,
     * Sam's directive nexus-zrcj7): every legitimate query already goes through
     * schema-qualified jOOQ generated Tables/Routines or a function-pinned
     * {@code SET search_path} in the function definition itself.
     *
     * <p><b>Call AFTER {@link #applyProductSchema}</b> — the {@code GRANT ... ON ALL
     * TABLES}/{@code ON ALL SEQUENCES} statements inside the test changelog require
     * the {@code nexus}/{@code staging} schemas and their tables to already exist.
     *
     * <p><b>Leaves {@code su} with {@code autoCommit(true)} restored</b> (real bug
     * found converting the six classes that also call {@link #seedServiceToken}, then
     * generalized here, nexus-cbo4a batch 1a follow-up): Liquibase's {@code update()}
     * manages its own changeset-boundary commits by disabling the connection's
     * autoCommit, and does NOT turn it back on when it returns. Every caller that then
     * runs a further raw statement on the SAME {@code su} — a {@code seedServiceToken}
     * insert, or a hand-kept {@code GRANT EXECUTE ON FUNCTION} the fixed grant set here
     * does not cover — was silently executing it inside an open transaction that got
     * rolled back the moment the try-with-resources closed {@code su}, with no
     * exception raised anywhere. The first version of this batch left the fix as a
     * copy-pasted {@code su.setAutoCommit(true)} at 11 call sites; restoring it here
     * once, at the one place every caller already goes through, means the fix cannot
     * be forgotten by a future caller and cannot rot into 11 independently-maintained
     * copies.
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
        su.setAutoCommit(true);
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

    /**
     * Seed one {@code nexus.service_tokens} row with {@code scope}/{@code
     * expires_at}/{@code revoked_at} set (nexus-cbo4a batch 9 item 1) — the overload
     * {@link #seedServiceToken(DSLContext, String, String, String)} does not cover,
     * for the sites that previously hand-rolled a wider column list via {@code
     * prepareStatement}/{@code createStatement().execute}. {@code scope}/{@code
     * expiresAt}/{@code revokedAt} are OMITTED from the insert (never set to a SQL
     * NULL) when the argument is {@code null} — {@code scope} carries a {@code NOT
     * NULL DEFAULT 'tenant'} CHECK constraint (service-tokens-003-scope-column.xml)
     * that a literal null would violate, and the two timestamp columns are simply
     * nullable-and-unset in that case, matching how a caller who never mentioned
     * them in a hand-rolled column list behaved.
     *
     * @param dsl       a {@link DSLContext} over the same connection/role the schema
     *                  was migrated under
     * @param token     the raw bearer token to hash and store
     * @param tenant    the tenant id to bind the token to
     * @param label     the token's {@code service_tokens.label} value
     * @param scope     {@code service_tokens.scope} ({@code root}/{@code tenant}/
     *                  {@code mint}/{@code data}), or {@code null} to take the
     *                  column default ({@code tenant})
     * @param expiresAt {@code service_tokens.expires_at}, or {@code null} to leave
     *                  it unset
     * @param revokedAt {@code service_tokens.revoked_at}, or {@code null} to leave
     *                  it unset
     */
    public static void seedServiceToken(DSLContext dsl, String token, String tenant, String label,
                                         String scope, java.time.OffsetDateTime expiresAt,
                                         java.time.OffsetDateTime revokedAt) {
        var step = dsl.insertInto(SERVICE_TOKENS)
            .set(SERVICE_TOKENS.TOKEN_HASH, TokenHashing.sha256Hex(token))
            .set(SERVICE_TOKENS.TENANT_ID, tenant)
            .set(SERVICE_TOKENS.LABEL, label);
        if (scope != null) {
            step = step.set(SERVICE_TOKENS.SCOPE, scope);
        }
        if (expiresAt != null) {
            step = step.set(SERVICE_TOKENS.EXPIRES_AT, expiresAt);
        }
        if (revokedAt != null) {
            step = step.set(SERVICE_TOKENS.REVOKED_AT, revokedAt);
        }
        step.onConflictDoNothing().execute();
    }

    /** The RDR-103 4-segment conformant name shape, {@code <ct>__<owner>__<model>__v<n>},
     *  same regex as hygiene-002-collection-attributes-walk.xml's own branch A --
     *  used only to derive constraint-satisfying attributes for {@link #insertCollection},
     *  never a general-purpose parser. */
    private static final Pattern CONFORMANT_COLLECTION_NAME = Pattern.compile(
        "^(code|docs|rdr|knowledge)__([a-zA-Z0-9-]+)__([a-z][a-z0-9-]*)__v[0-9]+$");

    /**
     * Seed a minimal {@code nexus.catalog_collections} row via generated jOOQ DSL
     * (nexus-cbo4a batch 10) — replaces the identical hand-rolled {@code INSERT INTO
     * nexus.catalog_collections (tenant_id, name) VALUES (...) ON CONFLICT (tenant_id,
     * name) DO NOTHING} that {@code CollectionRegistryFkTest} and
     * {@code CollectionRegistryFkExtraTest} each built by string concatenation
     * (identical shape, duplicated across files — the same class of duplication
     * {@link #seedServiceToken(DSLContext, String, String, String)} closed for
     * {@code service_tokens}).
     *
     * <p><b>nexus-ft04v.4/.5 fix (critic T2 critique-nexus-ft04v-4-walk-changeset-b42549f03
     * [24941] Critical):</b> hygiene-002-collection-attributes-walk.xml adds a non-empty
     * CHECK on {@code content_type}/{@code owner_id}/{@code embedding_model}, an
     * {@code embedding_model} FK to {@code nexus.embedding_models}, and a NOT NULL +
     * enum CHECK on {@code lifecycle_state} -- the bare two-column insert this method
     * used to do (still {@code ''}/{@code ''}/{@code ''}/{@code NULL}) violates all four
     * on the shared, fully-migrated test cluster, and this is the ONE place ~39 call
     * sites across the tree share, so it is fixed here rather than at each site. When
     * {@code name} matches the RDR-103 conformant shape
     * ({@code <ct>__<owner>__<model>__v<n>}, {@code ct} one of code/docs/rdr/knowledge),
     * {@code content_type}/{@code owner_id} come from the name and {@code embedding_model}
     * is the name's token when it is a real row in {@code embedding_models}, else the
     * seeded local fallback {@code bge-base-en-v15-768} (same rule
     * hygiene-002-1's own walk uses) -- {@code lifecycle_state} is {@code 'live'}.
     * Otherwise: {@code content_type} {@code 'unknown'}, {@code owner_id} the tenant,
     * {@code embedding_model} the fallback, and {@code lifecycle_state} {@code
     * 'quarantine'} when {@code name} carries the {@code quarantine-} prefix, else
     * {@code 'live'}. {@code model_version}/{@code display_name} stay at their {@code ''}
     * default -- neither is constrained and no caller of this method has ever needed
     * them set.
     *
     * <p><b>Column-existence guard (found while landing the fix above):</b> this
     * method is also called against databases migrated only PART WAY through the
     * changelog, well before catalog-036-4 even adds {@code dimension}/{@code
     * lifecycle_state} (e.g. {@code SchemaMigratorIntegrationTest}'s aged-fleet
     * scenarios, which stop at an early changeset like {@code catalog-013-0} on
     * purpose to reproduce a pre-constraint production timeline). Referencing
     * {@code CATALOG_COLLECTIONS.LIFECYCLE_STATE} unconditionally breaks those --
     * jOOQ's generated field exists at COMPILE time regardless of what the TARGET
     * database has actually walked to, so the INSERT fails with "column
     * lifecycle_state does not exist" there. {@link PgCatalogProbes#columnExists}
     * checks live, on the SAME connection, whether {@code lifecycle_state} exists
     * yet; when it does not, this method falls back to the original bare
     * {@code (tenant_id, name)} insert, preserving every early-migration caller's
     * prior behavior exactly.
     *
     * @param dsl      a {@link DSLContext} over the same connection/role the schema
     *                 was migrated under (e.g. {@code DSL.using(su, SQLDialect.POSTGRES)})
     * @param tenantId the tenant id to register the collection under
     * @param name     the collection name ({@code catalog_collections.name})
     */
    public static void insertCollection(DSLContext dsl, String tenantId, String name) {
        // nexus-cbo4a fix (ManifestCollectionStampTest's "null collection is rejected
        // the same way" case): catalog_collections.name is NOT NULL, so there is no
        // sensible row to seed for a null name -- a no-op here. Before this guard, a
        // null name reached the startsWith("quarantine-") check below and NPE'd,
        // masking the writer-under-test's own IllegalArgumentException contract
        // (requireNonBlank) behind an unrelated seeding-helper crash. Blank ("") is
        // deliberately NOT included here: an empty NAME is a valid, if unusual,
        // value that existing callers (e.g. this file's own blank-collection sibling
        // test) rely on actually registering a row under, same as any other name.
        if (name == null) {
            return;
        }
        if (!PgCatalogProbes.columnExists(dsl, "nexus", "catalog_collections", "lifecycle_state")) {
            dsl.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                .values(tenantId, name)
                .onConflictDoNothing()
                .execute();
            return;
        }

        String contentType = "unknown";
        String ownerId = tenantId;
        String embeddingModel = "bge-base-en-v15-768";
        String lifecycleState = name.startsWith("quarantine-") ? "quarantine" : "live";

        Matcher m = CONFORMANT_COLLECTION_NAME.matcher(name);
        if (m.matches()) {
            contentType = m.group(1);
            ownerId = m.group(2);
            String token = m.group(3);
            boolean modelKnown = dsl.fetchExists(dsl.selectOne().from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(token)));
            embeddingModel = modelKnown ? token : "bge-base-en-v15-768";
            lifecycleState = "live";
        }

        dsl.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(tenantId, name, contentType, ownerId, embeddingModel, lifecycleState)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Seed a minimal {@code nexus.catalog_documents} row via generated jOOQ DSL
     * (nexus-cbo4a batch 10) — the {@code catalog_documents} counterpart to {@link
     * #insertCollection}, replacing the identical hand-rolled {@code INSERT INTO
     * nexus.catalog_documents (tenant_id, tumbler, title) VALUES (...) ON CONFLICT
     * (tenant_id, tumbler) DO NOTHING} duplicated the same way. {@code
     * catalog_documents} PK is {@code (tenant_id, tumbler)}; {@code title} is
     * required NOT NULL and is synthesized as {@code "Test Doc " + tumbler}, matching
     * every hand-rolled call site's own literal exactly.
     *
     * @param dsl      a {@link DSLContext} over the same connection/role the schema
     *                 was migrated under
     * @param tenantId the tenant id to register the document under
     * @param tumbler  the document's {@code catalog_documents.tumbler}
     */
    public static void insertCatalogDocument(DSLContext dsl, String tenantId, String tumbler) {
        dsl.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE)
            .values(tenantId, tumbler, "Test Doc " + tumbler)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Seed one {@code nexus.chunks} row at dim 384 via generated jOOQ DSL (nexus-cbo4a
     * batch 10) — hoisted from the byte-for-byte identical {@code insertChunk384}
     * duplicated in {@code CatalogDeleteCollectionCascadeTest} and {@code
     * CatalogRenameCollectionTest} (batch-4's own critique named this exact
     * duplication class for the seed-insert shape). {@code chashBytes} is the raw
     * {@code chash} column value (callers control the 32-byte-vs-64-hex-decoded
     * distinction; see {@code chashBytes}/{@code hexChashBytes} helper pairs in the
     * callers), {@code chunk_text} is fixed at {@code "text"} — every existing call
     * site's own literal — since no caller has ever needed a different value.
     *
     * @param ctx        a {@link DSLContext} over the same connection/role the schema
     *                   was migrated under
     * @param tenant     the tenant id
     * @param collection the collection name
     * @param chashBytes the raw {@code chash} bytea value
     * @param v          the 384-dim embedding vector
     */
    public static void insertChunk384(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    /** {@code nexus.chunks} seed at dim 768 — see {@link #insertChunk384} for the full contract. */
    public static void insertChunk768(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_768)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    /** {@code nexus.chunks} seed at dim 1024 — see {@link #insertChunk384} for the full contract. */
    public static void insertChunk1024(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_1024)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    /**
     * Allowlist of GUC names {@link #setTenant} may stamp — the same two names {@link
     * TenantScope#PERMITTED_GUCS} enforces (that field is package-private inside {@code
     * dev.nexus.service.db}, unreachable from this package, so this is a second copy of
     * the same guard rather than a shared reference; drift between the two would be a
     * silent RLS-context miss either way, and both are derived from {@link
     * TenantScope#DEFAULT_TENANT_GUC}/{@link TenantScope#T1_TENANT_GUC} so a future third
     * GUC needs a coordinated edit in both places, not a lone one here).
     */
    private static final Set<String> TENANT_GUC_ALLOWLIST =
        Set.of(TenantScope.DEFAULT_TENANT_GUC, TenantScope.T1_TENANT_GUC);

    /**
     * Stamp {@code gucName} on an existing, test-owned {@link Connection} via jOOQ's typed
     * {@code set_config(...)} function call (nexus-cbo4a batch 2) — the test-tree counterpart
     * to {@link TenantScope#withTenant}, for call sites that hold a raw {@link Connection}
     * they already own (bootstrap superuser connections, multi-connection cross-tenant
     * isolation probes, {@code RESET}-then-reassert sequences) and need to stamp or clear a
     * tenant GUC on it directly, with no lambda-scoped connection lifecycle. {@link
     * TenantScope#withTenant} does not fit that shape at all — it BORROWS its own connection
     * from a {@link javax.sql.DataSource} and commits/closes it before returning, whereas every
     * caller here already has the connection open and keeps driving it afterward.
     *
     * <p>Replaces the raw {@code SET nexus.tenant = '...'} / {@code SET LOCAL nexus.tenant =
     * '...'} / {@code SELECT set_config('nexus.tenant', ..., ...)} / {@code RESET nexus.tenant}
     * string literals these call sites used to build by hand (Sam's no-raw-SQL-strings-in-Java
     * directive, nexus-zrcj7).
     *
     * @param conn    the connection to stamp; its transaction/autocommit state is left exactly
     *                as the caller set it — this method neither opens nor commits a transaction
     * @param gucName the GUC name, restricted to {@link #TENANT_GUC_ALLOWLIST} — the same
     *                defense-in-depth guard {@link TenantScope#withTenant} itself enforces, so
     *                this helper cannot become a second, unguarded path to an arbitrary session
     *                GUC
     * @param tenant  the tenant value to set, or {@code null} to RESET the GUC to its default
     *                (Postgres: {@code set_config(name, NULL, is_local)} performs exactly the
     *                {@code RESET name} operation — see the {@code set_config} documentation)
     * @param isLocal {@code true} for {@code SET LOCAL} (transaction-scoped — {@code conn} must
     *                have an open, not-yet-committed transaction, i.e. {@code autoCommit=false});
     *                {@code false} for session-scoped {@code SET}/{@code RESET}
     */
    public static void setTenant(Connection conn, String gucName, String tenant, boolean isLocal) {
        if (!TENANT_GUC_ALLOWLIST.contains(gucName)) {
            throw new IllegalArgumentException(
                "gucName not permitted: " + gucName + " (allowed: " + TENANT_GUC_ALLOWLIST + ")");
        }
        DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(gucName), DSL.val(tenant, SQLDataType.VARCHAR), DSL.val(isLocal)))
            .fetch();
    }

    /**
     * {@code ANALYZE table} on an existing, test-owned {@link Connection} (nexus-cbo4a
     * batch 3/4) -- the test-tree counterpart to {@link TenantScope#vacuumAnalyze}.
     *
     * <p>Batch 4 (nexus-zrcj7): retired the raw {@code stmt.execute("ANALYZE " + ...)}
     * onto {@code nexus_test.analyze_table(regclass)}, a TEST-SCHEMA function
     * (db/changelog-test/db.changelog-test-objects.xml, applied by
     * {@link #applyProductSchema} after the product master; jOOQ codegen for it runs
     * as a second plugin execution at generate-test-sources into
     * target/generated-test-sources/jooq, so nothing test-only ever enters the
     * product schema or the product changelog -- batch 5, Sam's ruling 2026-09-05)
     * -- ANALYZE is still PostgreSQL maintenance syntax with no typed jOOQ DSL form (same
     * category {@link TenantScope#vacuumAnalyze}'s own SANCTIONED RAW comment documents
     * for VACUUM), but that raw statement now lives server-side, inside a plpgsql wrapper
     * function, rather than being assembled client-side. Table identity comes from a
     * generated jOOQ {@link Table}, rendered through {@code ctx.render(table)} (properly
     * quoted/qualified for the dialect) into the function's {@code regclass} argument --
     * never a hand-typed schema-qualified string literal, so unlike {@code vacuumAnalyze}'s
     * allowlist (needed there because VACUUM's callers pass a bare string) there is no
     * caller-controlled name to validate: only a compile-time-checked generated
     * {@code Table} reaches this method. {@code Routines.analyzeTable}'s generated
     * {@code target} parameter is typed {@code Object} ({@code @Deprecated}, "Unknown data
     * type") because {@code regclass} has no jOOQ-recognized Java mapping -- the same
     * shape {@code CatalogRepository#searchDocuments}'s {@code Routines.catalogFtsMatch}
     * call already carries for its {@code tsvector} parameter; jOOQ still renders an
     * explicit {@code CAST(? AS "pg_catalog"."regclass")} around the bind value, so the
     * rendered table-name string is parsed by PostgreSQL's own regclass input function,
     * never string-concatenated into the query text.
     *
     * @param conn  the connection to run ANALYZE on; unlike {@code vacuumAnalyze}, ANALYZE
     *              has no autocommit/transaction-block restriction, so this method neither
     *              inspects nor changes the connection's autocommit state
     * @param table the generated jOOQ table to analyze (e.g. {@code Tables.CHUNKS})
     */
    public static void analyzeTable(Connection conn, Table<?> table) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.analyzeTable(ctx.configuration(), ctx.render(table));
    }

    /**
     * Overload for a table outside jOOQ codegen scope -- the {@code staging} schema is not
     * one of service/pom.xml's jOOQ codegen {@code <schemata>} (only {@code nexus}/{@code t1}
     * are), so no generated {@link Table} exists for e.g. {@code staging.document_chunks}.
     * Takes a jOOQ-constructed qualified {@link Name} instead (e.g.
     * {@code DSL.name("staging", "document_chunks")}) -- still never a hand-typed SQL
     * string -- rendered via {@code ctx.render(DSL.table(qualifiedName))} exactly like the
     * generated-{@code Table} overload above before reaching {@code nexus.analyze_table}.
     *
     * @param conn          the connection to run ANALYZE on
     * @param qualifiedName the schema-qualified table identifier (e.g.
     *                      {@code DSL.name("staging", "document_chunks")})
     */
    public static void analyzeTable(Connection conn, Name qualifiedName) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.analyzeTable(ctx.configuration(), ctx.render(DSL.table(qualifiedName)));
    }

    /**
     * {@code ALTER TABLE .. ADD CONSTRAINT .. FOREIGN KEY (tenant_id, column) REFERENCES
     * ref (tenant_id, refColumn) extraClause NOT VALID} (nexus-cbo4a batch 10 review
     * fold-in) -- the test-tree counterpart to {@link #analyzeTable}, same idiom: the
     * raw statement moves server-side into {@code nexus_test.add_fk_not_valid}
     * (db/changelog-test/db.changelog-test-objects.xml), a Postgres-only {@code ALTER
     * TABLE} extension ({@code NOT VALID}) with no jOOQ typed-DSL form (confirmed
     * against jOOQ 3.20.11/3.21's manual: {@code alterConstraint().enforced()/
     * notEnforced()} renders MySQL-style {@code [NOT] ENFORCED}, not Postgres's
     * {@code NOT VALID}). {@code table}/{@code refTable} are generated jOOQ
     * {@link Table}s, rendered through {@code ctx.render(...)} into the function's
     * {@code regclass} arguments -- never a hand-typed schema-qualified string. Every
     * caller's composite FK is {@code (tenant_id, X) -> (tenant_id, Y)}, so
     * {@code tenant_id} is hardcoded as the first column on both sides inside the
     * function body rather than parameterizing a shape no call site actually varies.
     *
     * @param conn           the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table          the table gaining the FK (e.g. {@code Tables.CHUNKS})
     * @param constraintName the FK constraint name
     * @param column         the second FK column (after {@code tenant_id})
     * @param refTable       the referenced table (e.g. {@code Tables.CATALOG_COLLECTIONS})
     * @param refColumn      the second referenced column (after {@code tenant_id})
     * @param extraClause    the {@code ON UPDATE}/{@code ON DELETE} clause fragment
     *                       (e.g. {@code "ON DELETE RESTRICT"}), or {@code ""} for none
     */
    public static void addFkNotValid(Connection conn, Table<?> table, String constraintName, String column,
                                      Table<?> refTable, String refColumn, String extraClause) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.addFkNotValid(ctx.configuration(), ctx.render(table), constraintName, column,
            ctx.render(refTable), refColumn, extraClause);
    }

    /**
     * {@code ALTER TABLE .. VALIDATE CONSTRAINT} (nexus-cbo4a batch 10 review fold-in)
     * -- see {@link #addFkNotValid} for the full contract this shares. {@code
     * VALIDATE CONSTRAINT} has no jOOQ typed-DSL form.
     *
     * @param conn           the connection to run the ALTER TABLE on
     * @param table          the table whose constraint is being validated
     * @param constraintName the constraint name
     */
    public static void validateConstraint(Connection conn, Table<?> table, String constraintName) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.validateConstraint(ctx.configuration(), ctx.render(table), constraintName);
    }

    /**
     * {@code ALTER TABLE .. [NO] FORCE ROW LEVEL SECURITY} (nexus-cbo4a batch 10
     * review fold-in) -- see {@link #addFkNotValid} for the full contract this
     * shares. {@code [NO] FORCE ROW LEVEL SECURITY} is a Postgres-only RLS DDL
     * extension with no jOOQ typed-DSL form.
     *
     * @param conn  the connection to run the ALTER TABLE on (table owner)
     * @param table the table to toggle {@code FORCE ROW LEVEL SECURITY} on
     * @param force {@code true} for {@code FORCE ROW LEVEL SECURITY}, {@code false}
     *              for {@code NO FORCE ROW LEVEL SECURITY}
     */
    public static void setForceRls(Connection conn, Table<?> table, boolean force) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.setForceRls(ctx.configuration(), ctx.render(table), force);
    }

    /**
     * {@code ALTER TABLE .. DROP CONSTRAINT IF EXISTS ..} (nexus-cbo4a batch 11) --
     * a genuinely dangling manifest row can only be seeded by momentarily dropping its
     * FK, inserting the dangling row, then re-adding the constraint NOT VALID (see
     * {@link #addFkNotValidComposite3}) -- the insert happens BETWEEN this call and
     * that one, so the two are separate methods rather than one combined drop-then-add.
     * {@code table} is a generated jOOQ {@link Table}, rendered through
     * {@code ctx.render(...)} into the function's {@code regclass} argument -- never a
     * hand-typed schema-qualified string.
     *
     * @param conn           the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table          the table whose constraint is being dropped
     * @param constraintName the constraint name
     */
    public static void dropConstraint(Connection conn, Table<?> table, String constraintName) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.dropConstraint(ctx.configuration(), ctx.render(table), constraintName);
    }

    /**
     * {@code ALTER TABLE .. ADD CONSTRAINT .. FOREIGN KEY (tenant_id, col2, col3)
     * REFERENCES .. (tenant_id, refCol2, refCol3) extraClause NOT VALID} (nexus-cbo4a
     * batch 11) -- the THREE-column composite-FK sibling of {@link #addFkNotValid},
     * which only covers a {@code (tenant_id, X) -> (tenant_id, Y)} shape. A
     * dangling-manifest-row test needs to re-add {@code fk_catalog_chunks_chunk}'s real
     * {@code (tenant_id, collection, chash)} composite after {@link #dropConstraint} and
     * an intervening dangling-row insert -- moved server-side into {@code
     * nexus_test.add_fk_not_valid_composite3} (db.changelog-test-objects.xml).
     * {@code table}/{@code refTable} are generated jOOQ {@link Table}s, rendered
     * through {@code ctx.render(...)} into the function's {@code regclass} arguments --
     * never a hand-typed schema-qualified string.
     *
     * @param conn        the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table       the table gaining the FK (e.g. {@code Tables.CATALOG_DOCUMENT_CHUNKS})
     * @param constraintName the FK constraint name
     * @param column2     the second FK column (after {@code tenant_id})
     * @param column3     the third FK column
     * @param refTable    the referenced table (e.g. {@code Tables.CHUNKS})
     * @param refColumn2  the second referenced column (after {@code tenant_id})
     * @param refColumn3  the third referenced column
     * @param extraClause the {@code ON UPDATE}/{@code ON DELETE} clause fragment
     *                    (e.g. {@code "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE"}),
     *                    or {@code ""} for none
     */
    public static void addFkNotValidComposite3(Connection conn, Table<?> table, String constraintName,
                                                String column2, String column3, Table<?> refTable,
                                                String refColumn2, String refColumn3, String extraClause)
            throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.addFkNotValidComposite3(ctx.configuration(), ctx.render(table), constraintName, column2, column3,
            ctx.render(refTable), refColumn2, refColumn3, extraClause);
    }

    /**
     * {@code GRANT EXECUTE ON FUNCTION functionSignature TO role} (nexus-cbo4a
     * batch 11) -- jOOQ's typed GRANT DSL ({@link DSLContext#grant}/{@code
     * GrantOnStep#on}) targets tables, not a function's parenthesized
     * argument-type signature, which Postgres's {@code GRANT ... ON FUNCTION}
     * syntax requires -- moved server-side into {@code
     * nexus_test.grant_execute_on_function} (db.changelog-test-objects.xml).
     *
     * @param conn              the connection to run the GRANT on (superuser)
     * @param functionSignature the function name plus its argument-type list
     *                          (e.g. {@code "nexus.gc_quarantine_orphans(int, text,
     *                          text, text, text, int)"}) -- a fixed Java literal,
     *                          never end-user input
     * @param role              the role to grant EXECUTE to
     */
    public static void grantExecuteOnFunction(Connection conn, String functionSignature, String role)
            throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.grantExecuteOnFunction(ctx.configuration(), functionSignature, role);
    }
}

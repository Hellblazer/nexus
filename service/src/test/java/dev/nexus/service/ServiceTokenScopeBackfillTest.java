package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-868dq Gate-A critique — the service-tokens-003 BACKFILL against a genuine
 * pre-003 cluster state (the production upgrade scenario every other test skips:
 * they all insert rows AFTER the full chain, with scope already stamped).
 *
 * <p>Builds the pre-003 table shape by hand (001's CREATE TABLE + 002's
 * single-root partial unique index, verbatim), seeds a root-labelled row and an
 * ordinary row THE WAY A LIVE 6.3.x CLUSTER HOLDS THEM (no scope column at all),
 * then applies ONLY the 003 changelog and asserts:
 * <ul>
 *   <li>the root-labelled row is backfilled to {@code scope='root'} (the deployed
 *       operator credential keeps its privilege across the upgrade), and</li>
 *   <li>every ordinary row lands on the {@code 'tenant'} default (exact prior
 *       authority, no privilege drift).</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ServiceTokenScopeBackfillTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // nexus-yhmav opt-out: this class hand-builds the PRE-003 table shape and
        // applies ONLY service-tokens-003-scope-column.xml (not the master changelog)
        // -- a shared, already-fully-migrated-to-HEAD cluster is incompatible with
        // that premise by construction (the bare CREATE TABLE below would collide
        // with the template's already-migrated nexus.service_tokens).
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Pre-003 shape, verbatim from service-tokens-001 + 002.
            su.createStatement().execute("CREATE SCHEMA IF NOT EXISTS nexus");
            su.createStatement().execute(
                "CREATE TABLE nexus.service_tokens ("
                + "  token_hash  TEXT        NOT NULL,"
                + "  tenant_id   TEXT        NOT NULL,"
                + "  label       TEXT,"
                + "  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),"
                + "  expires_at  TIMESTAMPTZ,"
                + "  revoked_at  TIMESTAMPTZ,"
                + "  CONSTRAINT service_tokens_pk PRIMARY KEY (token_hash))");
            su.createStatement().execute(
                "CREATE UNIQUE INDEX idx_service_tokens_single_root "
                + "ON nexus.service_tokens (label) WHERE label = 'bootstrap-legacy-token'");
            // A live cluster's rows: the operator credential + an ordinary tenant token.
            // Literal (fake) hashes, not a real token's sha256 -- this test asserts on
            // the exact hash string, so it inserts via the typed jOOQ DSL directly
            // rather than PgContainerHelper.seedServiceToken (which always hashes its
            // token argument). Only the three PRE-003 columns are referenced; the table
            // built above genuinely has no scope column yet at this point in the test.
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.insertInto(SERVICE_TOKENS)
                .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
                .values("upgrade-root-hash", "default", "bootstrap-legacy-token")
                .values("upgrade-plain-hash", "tenant-a", "ci")
                .execute();
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    @Test
    void backfill_flipsDeployedRootRow_leavesOrdinaryRowsOnTenantDefault() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/service-tokens-003-scope-column.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            var rootScope = dsl.select(SERVICE_TOKENS.SCOPE).from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TOKEN_HASH.eq("upgrade-root-hash"))
                .fetch(SERVICE_TOKENS.SCOPE);
            assertThat(rootScope).hasSize(1);
            assertThat(rootScope.get(0))
                .as("the deployed operator credential must keep its privilege across the upgrade")
                .isEqualTo("root");

            var plainScope = dsl.select(SERVICE_TOKENS.SCOPE).from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TOKEN_HASH.eq("upgrade-plain-hash"))
                .fetch(SERVICE_TOKENS.SCOPE);
            assertThat(plainScope).hasSize(1);
            assertThat(plainScope.get(0))
                .as("ordinary rows keep their exact prior authority via the 'tenant' default")
                .isEqualTo("tenant");
        }
    }
}

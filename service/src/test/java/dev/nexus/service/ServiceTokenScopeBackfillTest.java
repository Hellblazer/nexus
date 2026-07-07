package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;

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
        pg = PgContainerHelper.start();
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
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES "
                + "('upgrade-root-hash', 'default', 'bootstrap-legacy-token'), "
                + "('upgrade-plain-hash', 'tenant-a', 'ci')");
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
            ResultSet root = su.createStatement().executeQuery(
                "SELECT scope FROM nexus.service_tokens WHERE token_hash = 'upgrade-root-hash'");
            assertThat(root.next()).isTrue();
            assertThat(root.getString("scope"))
                .as("the deployed operator credential must keep its privilege across the upgrade")
                .isEqualTo("root");

            ResultSet plain = su.createStatement().executeQuery(
                "SELECT scope FROM nexus.service_tokens WHERE token_hash = 'upgrade-plain-hash'");
            assertThat(plain.next()).isTrue();
            assertThat(plain.getString("scope"))
                .as("ordinary rows keep their exact prior authority via the 'tenant' default")
                .isEqualTo("tenant");
        }
    }
}

package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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
import java.util.HashSet;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.32.1 — Phase A schema integration test for the
 * bridge token lifecycle credential tables.
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker. Applies the
 * Liquibase master changelog and asserts the structural and runtime properties
 * of {@code nexus.service_tokens} and {@code nexus.session_tokens}.
 *
 * <p><b>Design under test (locked: T2 nexus_rdr/gmiaf.32-token-design-DECISIONS).</b>
 * Both tables are credential-resolution lookup tables read by the auth layer
 * BEFORE any tenant context (GUC) exists — the presented bearer/session token is
 * what resolves the tenant. A tenant RLS policy keyed on a GUC would therefore
 * make authentication structurally impossible (a pre-context read would return
 * zero rows). So NEITHER table enables RLS. The sole grantee is the service's own
 * auth layer ({@code nexus_svc}); isolation of the DATA these tokens authorize
 * remains enforced on the domain tables (nexus.* T2 FORCE-RLS, t1.scratch
 * FORCE-RLS + session_id filter), not on the credential tables.
 *
 * <p>Coverage:
 * <ol>
 *   <li>service_tokens exact column set</li>
 *   <li>session_tokens exact column set</li>
 *   <li>service_tokens has RLS DISABLED (relrowsecurity=f) — readable pre-context</li>
 *   <li>session_tokens has RLS DISABLED (relrowsecurity=f)</li>
 *   <li>READABILITY INVARIANT: nexus_svc (NOSUPERUSER NOBYPASSRLS) SELECTs
 *       service_tokens with NO nexus.tenant GUC set and sees ALL rows</li>
 *   <li>READABILITY INVARIANT: same for session_tokens</li>
 *   <li>nexus_svc is defensively NOT superuser and NOT bypassrls (so #5/#6 prove
 *       RLS-off, not a role escape hatch)</li>
 *   <li>indexes: service_tokens(tenant_id), session_tokens(tenant_id, session_id),
 *       plus the two primary keys</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ServiceTokenSchemaLiquibaseTest {

    private static final Set<String> SERVICE_TOKEN_COLUMNS = Set.of(
        "token_hash", "tenant_id", "label", "created_at", "expires_at", "revoked_at"
    );

    private static final Set<String> SESSION_TOKEN_COLUMNS = Set.of(
        "session_token_hash", "tenant_id", "session_id", "created_at", "expires_at"
    );

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";

    EmbeddedPostgres pg;
    HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Create nexus_svc with the production posture: LOGIN, NOSUPERUSER,
        // NOBYPASSRLS. The grants-nexus-svc.xml changeset (runAlways, LAST) then
        // grants it DML on ALL TABLES in the nexus schema, including the two new
        // credential tables.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' " +
                "      NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        svcDs = buildSvcDs();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.close();
    }

    // ── Test 1: service_tokens exact column set ──────────────────────────────

    @Test
    void serviceTokens_hasExactColumnSet() throws Exception {
        assertThat(columnsOf("nexus", "service_tokens"))
            .as("nexus.service_tokens must have exactly the expected columns")
            .isEqualTo(SERVICE_TOKEN_COLUMNS);
    }

    // ── Test 2: session_tokens exact column set ──────────────────────────────

    @Test
    void sessionTokens_hasExactColumnSet() throws Exception {
        assertThat(columnsOf("nexus", "session_tokens"))
            .as("nexus.session_tokens must have exactly the expected columns")
            .isEqualTo(SESSION_TOKEN_COLUMNS);
    }

    // ── Test 3: service_tokens RLS DISABLED ──────────────────────────────────

    @Test
    void serviceTokens_rlsDisabled() throws Exception {
        assertThat(rlsEnabled("nexus", "service_tokens"))
            .as("service_tokens must NOT enable RLS — it is read before tenant "
                + "context exists; a tenant policy would make auth impossible")
            .isFalse();
    }

    // ── Test 4: session_tokens RLS DISABLED ──────────────────────────────────

    @Test
    void sessionTokens_rlsDisabled() throws Exception {
        assertThat(rlsEnabled("nexus", "session_tokens"))
            .as("session_tokens must NOT enable RLS — it is a credential-resolution "
                + "table read before tenant context exists")
            .isFalse();
    }

    // ── Test 5: READABILITY INVARIANT — nexus_svc reads service_tokens, no GUC ─

    @Test
    void serviceTokens_readableByServiceRole_withoutTenantGuc() throws Exception {
        // Seed two rows for two different tenants via superuser.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES "
                + "('hash-tenant-a', 'tenant-a', 'a-root'), "
                + "('hash-tenant-b', 'tenant-b', 'b-root')");
        }

        // nexus_svc reads with NO nexus.tenant GUC stamped — must see BOTH rows.
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.service_tokens");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("nexus_svc must read ALL service_tokens rows with no tenant GUC "
                    + "(the readability invariant that makes token->tenant resolution possible)")
                .isEqualTo(2L);
        }
    }

    // ── Test 6: READABILITY INVARIANT — nexus_svc reads session_tokens, no GUC ─

    @Test
    void sessionTokens_readableByServiceRole_withoutTenantGuc() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.session_tokens "
                + "(session_token_hash, tenant_id, session_id, expires_at) VALUES "
                + "('sess-hash-a', 'tenant-a', 'session-a', now() + interval '1 hour'), "
                + "('sess-hash-b', 'tenant-b', 'session-b', now() + interval '1 hour')");
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.session_tokens");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("nexus_svc must read ALL session_tokens rows with no tenant GUC")
                .isEqualTo(2L);
        }
    }

    // ── Test 7: service role defensive — not superuser, not bypassrls ─────────

    @Test
    void serviceRole_notSuperuserNotBypassRls() throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getBoolean("rolsuper"))
                .as("service role must NOT be superuser").isFalse();
            assertThat(rs.getBoolean("rolbypassrls"))
                .as("service role must NOT have BYPASSRLS (so the readability invariant "
                    + "proves RLS-off, not a role escape hatch)").isFalse();
        }
    }

    // ── Test 8: indexes present ──────────────────────────────────────────────

    @Test
    void indexes_present() throws Exception {
        Set<String> stIdx = indexDefsOf("nexus", "service_tokens");
        assertThat(stIdx)
            .as("service_tokens must have a PK on token_hash")
            .anyMatch(d -> d.contains("(token_hash)") && d.toLowerCase().contains("unique"));
        assertThat(stIdx)
            .as("service_tokens must have an index on (tenant_id) for token listing")
            .anyMatch(d -> d.contains("(tenant_id)"));

        Set<String> sessIdx = indexDefsOf("nexus", "session_tokens");
        assertThat(sessIdx)
            .as("session_tokens must have a PK on session_token_hash")
            .anyMatch(d -> d.contains("(session_token_hash)") && d.toLowerCase().contains("unique"));
        assertThat(sessIdx)
            .as("session_tokens must have an index on (tenant_id, session_id)")
            .anyMatch(d -> d.contains("(tenant_id, session_id)"));
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private Set<String> columnsOf(String schema, String table) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.getMetaData().getColumns(null, schema, table, null);
            Set<String> actual = new HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            return actual;
        }
    }

    private boolean rlsEnabled(String schema, String table) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT relrowsecurity FROM pg_class c "
                + "JOIN pg_namespace n ON c.relnamespace = n.oid "
                + "WHERE n.nspname = '" + schema + "' AND c.relname = '" + table + "'");
            assertThat(rs.next()).as(schema + "." + table + " must exist in pg_class").isTrue();
            return rs.getBoolean("relrowsecurity");
        }
    }

    private Set<String> indexDefsOf(String schema, String table) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT indexdef FROM pg_indexes "
                + "WHERE schemaname = '" + schema + "' AND tablename = '" + table + "'");
            Set<String> defs = new HashSet<>();
            while (rs.next()) defs.add(rs.getString("indexdef"));
            return defs;
        }
    }

    private HikariDataSource buildSvcDs() {
        var config = new HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        config.setConnectionInitSql("SET search_path TO nexus, t1, public");
        return new HikariDataSource(config);
    }
}

package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.Network;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.utility.DockerImageName;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4pz86 (conexus RDR-001): prove the engine works correctly through a REAL
 * transaction-mode PgBouncer with the server-connection pool capped at one, AND that
 * tenant context does not bleed across the reused server backend. This is the production
 * topology (Crunchy Standard fronts PostgreSQL with PgBouncer); the engine-side analog of
 * the conexus GucLeakTripwire.
 *
 * <p>Complements {@link TenantPoolingIsolationTest} (which proves the leak property
 * deterministically via a 1-connection HikariCP pool, no external pooler). This class
 * adds the two things only a real txn-mode pooler can exercise:
 * <ul>
 *   <li><b>(a) functional correctness</b> — multiple transactions multiplexed by
 *       PgBouncer onto a single server backend must succeed. A reliance on session-scoped
 *       server state that txn-mode pooling resets between transactions (server-prepared
 *       statements, session {@code SET}, advisory locks, LISTEN/NOTIFY) would surface as
 *       errors here.</li>
 *   <li><b>(b) leak-safety</b> — a second tenant's transaction on the reused backend sees
 *       none of the first's rows.</li>
 * </ul>
 *
 * <p><b>Sensitivity note.</b> Like {@code TenantPoolingIsolationTest}'s isolation tests,
 * the leak test here re-stamps the GUC each transaction, so it would not detect a
 * session-scoped {@code set_config} regression on its own (and PgBouncer's
 * {@code server_reset_query} would mask it too). The assertion actually sensitive to the
 * transaction-local property is {@code TenantPoolingIsolationTest#tenantGucIsResetAfter
 * TransactionCommit}, which reads the GUC on a raw, un-pooler-mediated borrow.
 *
 * <p>Topology: PG (pgvector) and PgBouncer share a Docker network; PgBouncer runs in
 * {@code transaction} mode with {@code default_pool_size=1} + {@code max_db_connections=1}
 * so every client transaction is forced onto the one server backend. The app
 * {@link TenantScope}/{@link MemoryRepository} connect through PgBouncer's mapped port.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgBouncerTenantIsolationTest {

    // Pinned by digest for reproducibility (edoburu/pgbouncer:latest at 2026-06-16).
    private static final DockerImageName PGBOUNCER_IMAGE = DockerImageName.parse(
        "edoburu/pgbouncer@sha256:4c1ca296ef525f108f5d3552cc337c0c09587cf8dae7f0067fd93349e47dc1cd");

    private static final String PG_ALIAS = "pgdb";
    private static final String TENANT_A = "pgb-tenant-a";
    private static final String TENANT_B = "pgb-tenant-b";
    private static final String PROJECT = "pgb-isolation";

    Network net;
    PostgreSQLContainer<?> pg;
    GenericContainer<?> pgbouncer;
    HikariDataSource ds;
    MemoryRepository repo;

    @BeforeAll
    @SuppressWarnings("resource")
    void startAll() throws Exception {
        net = Network.newNetwork();
        pg = new PostgreSQLContainer<>(
                DockerImageName.parse(PgContainerHelper.IMAGE).asCompatibleSubstituteFor("postgres"))
            .withDatabaseName(PgContainerHelper.DATABASE)
            .withUsername(PgContainerHelper.USERNAME)
            .withPassword(PgContainerHelper.PASSWORD)
            .withNetwork(net)
            .withNetworkAliases(PG_ALIAS);
        pg.start();

        // Create the production service role + apply the schema (grants-nexus-svc.xml
        // grants nexus_svc DML), exactly as the other PG tests do.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='"
                + PgContainerHelper.SVC_USERNAME + "') THEN CREATE ROLE "
                + PgContainerHelper.SVC_USERNAME + " LOGIN PASSWORD '"
                + PgContainerHelper.SVC_PASSWORD + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (var lq = new Liquibase("db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                lq.update(new Contexts());
            }
        }

        pgbouncer = new GenericContainer<>(PGBOUNCER_IMAGE)
            .withNetwork(net)
            .withEnv("DB_HOST", PG_ALIAS)
            .withEnv("DB_PORT", "5432")
            .withEnv("DB_NAME", PgContainerHelper.DATABASE)
            .withEnv("DB_USER", PgContainerHelper.SVC_USERNAME)
            .withEnv("DB_PASSWORD", PgContainerHelper.SVC_PASSWORD)
            .withEnv("AUTH_TYPE", "plain")
            .withEnv("POOL_MODE", "transaction")   // the production / leak-relevant mode
            .withEnv("DEFAULT_POOL_SIZE", "1")      // one server backend...
            .withEnv("MAX_DB_CONNECTIONS", "1")     // ...forced, so reuse is guaranteed
            .withEnv("MAX_CLIENT_CONN", "20")
            .withExposedPorts(5432)
            .waitingFor(Wait.forListeningPort().withStartupTimeout(Duration.ofSeconds(60)));
        pgbouncer.start();

        String jdbc = "jdbc:postgresql://" + pgbouncer.getHost() + ":"
            + pgbouncer.getMappedPort(5432) + "/" + PgContainerHelper.DATABASE;
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(jdbc);
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);   // several CLIENT conns; PgBouncer multiplexes onto 1 server
        cfg.setAutoCommit(true);
        cfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        // PgBouncer transaction mode is incompatible with JDBC server-side prepared
        // statements that outlive a transaction; disable them so the driver uses the
        // simple/unnamed path (the documented client setting for txn-mode pooling, what a
        // correct production deployment configures).
        // SCOPE BOUNDARY (substantive-critic 2026-06-16): this test verifies the engine
        // works WHEN correctly configured; it does NOT assert that OMITTING
        // prepareThreshold=0 fails. Catching a deployment that forgets this flag is a
        // deployment-config concern, not covered here (a negative test would be
        // JDBC-driver-version-brittle).
        cfg.addDataSourceProperty("prepareThreshold", "0");
        ds = new HikariDataSource(cfg);

        repo = new MemoryRepository(new TenantScope(ds));
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pgbouncer != null) pgbouncer.stop();
        if (pg != null) pg.stop();
        if (net != null) net.close();
    }

    @BeforeEach
    void cleanMemory() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("TRUNCATE nexus.memory");
        }
    }

    @Test
    void functionsCorrectlyThroughTransactionModePgBouncer() {
        // (a) Multiple transactions multiplexed onto the single server backend must work.
        for (int i = 0; i < 6; i++) {
            long id = repo.upsert(TENANT_A, PROJECT, "fn-" + i, "v-" + i, "", null, null, null);
            assertThat(id).as("upsert %s through PgBouncer must succeed", i).isPositive();
        }
        assertThat(repo.findByProject(TENANT_A, PROJECT))
            .as("all writes are visible to their tenant through the pooler").hasSize(6);
    }

    @Test
    void tenantContextDoesNotBleedThroughPgBouncer() {
        // (b) Tenant A writes; tenant B (its txn multiplexed onto the SAME server backend
        // by PgBouncer) must see none of A's rows.
        repo.upsert(TENANT_A, PROJECT, "secret", "A-only", "", null, null, null);
        assertThat(repo.findByProject(TENANT_B, PROJECT))
            .as("tenant B must not see tenant A's rows through txn-mode PgBouncer")
            .isEmpty();
        assertThat(repo.findByProject(TENANT_A, PROJECT))
            .as("tenant A sees its own row (positive control)").hasSize(1);
    }
}

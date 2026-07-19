/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.RekeyOps;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * BUG-0148 reproduction harness (conexus relay 2026-07-19, conexus-xpg7):
 * after the v0.1.48 cloud deploy applied the RDR-180 bytea conversion at
 * boot, {@code /v1/vectors/hybrid-search} REGRESSED to 0 rows on selective
 * gates while pure vector search kept serving the same rows. The cloud is
 * in the chash WINDOW (conversion applied, rekey rung not yet run): every
 * pre-existing row carries its 16-byte legacy key.
 *
 * <p>The window contract (nexus-p78a0) is LOUD + SAFE: every read surface
 * serves window rows. Pure search got that treatment (a65e291e,
 * enrichSearchRows); this test asks the same question of BOTH hybrid plan
 * branches (nexus-lcogi selective chash-IN rank + HNSW-first dense), and
 * then proves the planned cloud remediation: {@link RekeyOps#rekey}
 * closes the window and hybrid serves the SAME content canonically.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class WindowHybridSearchIntegrationTest {

    private static final String SVC_ROLE = "svc_window_hybrid_test";
    private static final String SVC_PASS = "svc_window_hybrid_test_pass";
    private static final String TENANT = "t-window-hy";

    private static final String COL = "rdr__window__voyage-context-3__v1";

    private static final String T_ALPHA = "gpu batch scheduling design alpha";
    private static final String T_BETA  = "gpu batch scheduling design beta";
    private static final String T_GAMMA = "gpu batch scheduling design gamma";
    private static final String T_DELTA = "gpu batch scheduling design delta";
    private static final String QUERY   = "gpu batch scheduling";

    private static final String DENSE_QUERY = "dense window corpus";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    RekeyOps rekeyOps;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    private static byte[] sha256(String text) {
        try {
            return MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The window key: 16 bytes = decode(sha256_hex[:32], 'hex'). */
    private static byte[] legacyKey(String text) {
        byte[] half = new byte[16];
        System.arraycopy(sha256(text), 0, half, 0, 16);
        return half;
    }

    private static String legacyHex(String text) {
        return HexFormat.of().formatHex(legacyKey(text));
    }

    private static String canonicalHex(String text) {
        return HexFormat.of().formatHex(sha256(text));
    }

    private static String unitVec(float x, float y) {
        StringBuilder sb = new StringBuilder("[").append(x).append(',').append(y);
        for (int i = 2; i < 1024; i++) sb.append(",0");
        return sb.append(']').toString();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : new String[] {SVC_ROLE, "nexus_svc"}) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                    + role + "') THEN CREATE ROLE " + role + " LOGIN PASSWORD '"
                    + (role.equals(SVC_ROLE) ? SVC_PASS : "nexus_svc_pass")
                    + "'; END IF; END $$");
            }
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        scope = new TenantScope(svcDs);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(QUERY, 1.0f, 0.0f);
        embedder.register(DENSE_QUERY, 1.0f, 0.0f);
        embedder.register(T_DELTA, 1.0f, 0.0f);
        repo = new PgVectorRepository(scope, embedder, embedder);
        rekeyOps = new RekeyOps(scope);

        seedWindowState();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Reconstruct the exact post-boot cloud state: the bytea conversion has
     * run (schema is at HEAD), every pre-existing row carries its 16-byte
     * legacy key. The octet CHECK is NOT VALID for existing rows in that
     * state; on this fresh cluster it would fire on our INSERTs, so it is
     * dropped around seeding and re-added NOT VALID — the same
     * reconstruction RekeyOpsIntegrationTest uses.
     */
    private void seedWindowState() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type) "
                + "VALUES ('" + TENANT + "', '" + COL + "', 'rdr') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "ALTER TABLE nexus.chunks_1024 DROP CONSTRAINT chunks_1024_chash_octet_check");
            try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) "
                + "VALUES (?, ?, ?, ?, ?::vector)")) {
                for (Map.Entry<String, String> e : Map.of(
                        T_ALPHA, unitVec(1.0f, 0.0f),
                        T_BETA,  unitVec(0.8f, 0.6f),
                        T_GAMMA, unitVec(0.6f, 0.8f)).entrySet()) {
                    ps.setString(1, TENANT);
                    ps.setString(2, COL);
                    ps.setBytes(3, legacyKey(e.getKey()));
                    ps.setString(4, e.getKey());
                    ps.setString(5, e.getValue());
                    ps.executeUpdate();
                }
                // The dense-gate corpus: > SELECTIVE_GATE_MAX matching rows so
                // the HNSW-first branch dispatches.
                for (int i = 0; i < 200; i++) {
                    String text = "dense window corpus row " + i;
                    ps.setString(1, TENANT);
                    ps.setString(2, COL);
                    ps.setBytes(3, legacyKey(text));
                    ps.setString(4, text);
                    ps.setString(5, unitVec(0.6f, 0.8f));
                    ps.executeUpdate();
                }
            } finally {
                su.createStatement().execute(
                    "ALTER TABLE nexus.chunks_1024 ADD CONSTRAINT chunks_1024_chash_octet_check "
                    + "CHECK (octet_length(chash) = 32) NOT VALID");
            }
        }
        // One POST-cohort row via the real serving upsert (canonical 64-hex id)
        // — the mixed-era store every real window install has.
        repo.upsertChunks(TENANT, COL,
            List.of(canonicalHex(T_DELTA)), List.of(T_DELTA), List.of(Map.of()));
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    // ── Order 1: the field case — selective gate over a window store ─────────

    @Test
    @Order(1)
    void selectiveGate_servesWindowRows_besideCanonical() {
        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, QUERY, List.of(COL), 10, null);
        assertThat(ids(rows))
            .as("BUG-0148: the selective hybrid branch must serve 16-byte window "
                + "rows exactly as pure search does (window contract: LOUD + SAFE)")
            .containsExactlyInAnyOrder(
                legacyHex(T_ALPHA), legacyHex(T_BETA), legacyHex(T_GAMMA),
                canonicalHex(T_DELTA));
    }

    // ── Order 2: the dense gate (HNSW-first branch) over window rows ─────────

    @Test
    @Order(2)
    void denseGate_servesWindowRows() {
        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, DENSE_QUERY, List.of(COL), 10, null);
        assertThat(rows)
            .as("the HNSW-first dense branch must serve window rows too")
            .hasSize(10);
        assertThat(ids(rows))
            .as("every dense hit is a 32-hex window identity pre-rekey")
            .allMatch(id -> id.length() == 32);
    }

    // ── Order 3: the cloud remediation — rekey closes the window ─────────────

    @Test
    @Order(3)
    void rekey_closesTheWindow_hybridServesCanonically() {
        Map<String, Object> counts = rekeyOps.rekey(TENANT, false);
        assertThat((int) counts.get("residual_mismatched")).isZero();

        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, QUERY, List.of(COL), 10, null);
        assertThat(ids(rows))
            .as("post-rekey the SAME content serves under canonical identities")
            .containsExactlyInAnyOrder(
                canonicalHex(T_ALPHA), canonicalHex(T_BETA), canonicalHex(T_GAMMA),
                canonicalHex(T_DELTA));
    }
}

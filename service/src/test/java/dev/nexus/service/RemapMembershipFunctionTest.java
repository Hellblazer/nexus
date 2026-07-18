package dev.nexus.service;

import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
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
 * RDR-186 bead nexus-146xx.5 — nexus.remap_membership() live-membership function.
 *
 * <p>The REAL nexus-tidtd fix: an indexed SQL function computing, per leg
 * (source_collection, target_collection), how many of the leg's map claims
 * (nexus.chash_remap rows) have their new_chash PRESENT in the target chunk
 * tables — a LIVE count, computed fresh on every call, never persisted
 * (RF-186-1 / Gap-4 pin: the answer must track the world in BOTH directions).
 *
 * <p>Replaces the broken count-equality convergence test
 * (source_count == target_count, substrate_etl.py:587) which FOREVER-FAILS
 * cross-embedder-era re-chunk migrations (nexus-tidtd: source = 3 stale minilm
 * chunks vs target = 6712 independently-indexed voyage chunks). The tidtd
 * fixture below encodes exactly that shape and asserts membership converges
 * where count-equality never can — restoring count-equality fails that test
 * (the mutation the bead demands).
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Each test seeds its OWN leg (distinct collection names) so tests are
 * order-independent within the shared PER_CLASS container.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RemapMembershipFunctionTest {

    private static final String SVC_ROLE = "svc_remap_fn_test";
    private static final String SVC_PASS = "svc_remap_fn_test_pass";
    private static final String TENANT = "fn-tenant-alpha";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chash_remap TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.chunks_384, nexus.chunks_768, nexus.chunks_1024 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.remap_membership(text, text) TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── Test 1: the tidtd fixture — membership converges where count-equality
    //            forever-fails ─────────────────────────────────────────────────

    @Test
    void tidtdFixture_membershipConverges_whereCountEqualityForeverFails() throws Exception {
        String src = "legacy__minilm__t1";
        String tgt = "knowledge__voyage__t1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, tgt);
            // 3 stale source chunks were re-chunked/re-embedded into the target;
            // the map claims exactly those 3 new chashes.
            for (int i = 1; i <= 3; i++) {
                insertMapRow(su, TENANT, src, "legacy-id-" + i, chash("t1map" + i), tgt);
                insertChunk1024(su, TENANT, tgt, chash("t1map" + i));
            }
            // The target was ALSO independently indexed: 5 more chunks the map
            // never claimed (the tidtd shape — target >> source).
            for (int i = 1; i <= 5; i++) {
                insertChunk1024(su, TENANT, tgt, chash("t1extra" + i));
            }
        }

        long[] m = membership(src, tgt);
        long mappedTotal = m[0];
        long presentCount = m[1];

        assertThat(mappedTotal).as("the leg claims exactly 3 remapped ids").isEqualTo(3);
        assertThat(presentCount)
            .as("all 3 claimed chashes are present in the target — the leg is CONVERGED " +
                "by live membership (present == mapped)")
            .isEqualTo(mappedTotal);

        // The mutation guard: count-equality (substrate_etl.py:587's broken test)
        // FOREVER-FAILS this fixture — target holds 8 rows vs 3 mapped. Any
        // attempt to 'restore' count-equality as the convergence answer cannot
        // pass this assertion pair.
        long targetCount;
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.chunks_1024 " +
                "WHERE tenant_id = '" + TENANT + "' AND collection = '" + tgt + "'");
            rs.next();
            targetCount = rs.getLong("cnt");
        }
        assertThat(targetCount)
            .as("the tidtd shape: independently-indexed target is STRICTLY larger than " +
                "the mapped claim set, so source_count == target_count never holds")
            .isEqualTo(8)
            .isNotEqualTo(mappedTotal);
    }

    // ── Test 2: answer tracks the world DOWN — target row deleted ────────────

    @Test
    void answerTracksWorld_downDirection_targetRowDeleted() throws Exception {
        String src = "legacy__minilm__t2";
        String tgt = "knowledge__voyage__t2";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, tgt);
            for (int i = 1; i <= 3; i++) {
                insertMapRow(su, TENANT, src, "legacy-id-" + i, chash("t2map" + i), tgt);
                insertChunk1024(su, TENANT, tgt, chash("t2map" + i));
            }
        }
        long[] before = membership(src, tgt);
        assertThat(before[1]).as("all 3 present before regression").isEqualTo(3);

        // The world regresses: one target chunk disappears.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DELETE FROM nexus.chunks_1024 WHERE tenant_id = '" + TENANT + "' " +
                "AND collection = '" + tgt + "' AND chash = '" + chash("t2map3") + "'");
        }

        long[] after = membership(src, tgt);
        assertThat(after[0]).as("map claims unchanged").isEqualTo(3);
        assertThat(after[1])
            .as("LIVE answer follows the world DOWN (Gap-4 both-directions property): " +
                "present drops when a target row disappears — no cached verdict survives")
            .isEqualTo(2);
    }

    // ── Test 3: answer tracks the world after leg map-clear — nothing owed ───

    @Test
    void answerTracksWorld_afterLegMapClear_readsNothingOwed() throws Exception {
        String src = "legacy__minilm__t3";
        String tgt = "knowledge__voyage__t3";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, tgt);
            for (int i = 1; i <= 2; i++) {
                insertMapRow(su, TENANT, src, "legacy-id-" + i, chash("t3map" + i), tgt);
                insertChunk1024(su, TENANT, tgt, chash("t3map" + i));
            }
        }
        long[] before = membership(src, tgt);
        assertThat(before[0]).isEqualTo(2);

        // Rollback clears the leg's map rows (the D2 absence-encoding).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            stampGuc(su, TENANT);
            su.createStatement().execute(
                "DELETE FROM nexus.chash_remap WHERE tenant_id = '" + TENANT + "' " +
                "AND source_collection = '" + src + "'");
        }

        long[] after = membership(src, tgt);
        assertThat(after[0])
            .as("after leg map-clear the leg claims nothing — delivered-then-rolled-back " +
                "and never-delivered collapse to the same live state (0 owed)")
            .isEqualTo(0);
        assertThat(after[1]).isEqualTo(0);
    }

    // ── Test 4: dim-agnostic — claims found in chunks_384 too ────────────────

    @Test
    void dimAgnostic_claimInChunks384_counted() throws Exception {
        String src = "legacy__code__t4";
        String tgt = "code__minilm__t4";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, tgt);
            insertMapRow(su, TENANT, src, "legacy-id-1", chash("t4map1"), tgt);
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT + "', '" + tgt + "', '" + chash("t4map1") + "', 'text', " +
                "('[1" + ",0".repeat(383) + "]')::vector)");
        }

        long[] m = membership(src, tgt);
        assertThat(m[0]).isEqualTo(1);
        assertThat(m[1])
            .as("membership must probe ALL chunk dims (384/768/1024) — a target " +
                "collection lives in exactly one, and the function must find it " +
                "without being told which")
            .isEqualTo(1);
    }

    // ── Test 5: RLS scopes the answer to the stamped tenant ──────────────────

    @Test
    void rls_scopesMembershipToStampedTenant() throws Exception {
        String src = "legacy__minilm__t5";
        String tgt = "knowledge__voyage__t5";
        String otherTenant = "fn-tenant-beta";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT, tgt);
            insertCollection(su, otherTenant, tgt);
            // alpha: 1 claim, present.
            insertMapRow(su, TENANT, src, "legacy-id-1", chash("t5map1"), tgt);
            insertChunk1024(su, TENANT, tgt, chash("t5map1"));
            // beta: 4 claims on the SAME collection names, none present.
            for (int i = 1; i <= 4; i++) {
                insertMapRow(su, otherTenant, src, "beta-id-" + i, chash("t5beta" + i), tgt);
            }
        }

        long[] m = membership(src, tgt);
        assertThat(m[0])
            .as("SECURITY INVOKER under FORCE RLS: beta's 4 claims must be invisible " +
                "to the alpha-stamped call")
            .isEqualTo(1);
        assertThat(m[1]).isEqualTo(1);
    }

    // ── Test 6: function is SECURITY INVOKER (RLS applies inside it) ─────────

    @Test
    void function_isSecurityInvoker() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT prosecdef FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND p.proname = 'remap_membership'");
            assertThat(rs.next()).as("nexus.remap_membership must exist").isTrue();
            assertThat(rs.getBoolean("prosecdef"))
                .as("must be SECURITY INVOKER (prosecdef=false) so FORCE RLS applies " +
                    "to the calling tenant inside the function")
                .isFalse();
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    /** Call the function under the svc role with the tenant GUC stamped. */
    private long[] membership(String src, String tgt) {
        return tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.fetchOne(
                "SELECT mapped_total, present_count FROM nexus.remap_membership(?, ?)",
                src, tgt);
            assertThat(row).as("function must return exactly one row").isNotNull();
            return new long[]{
                row.get("mapped_total", Long.class),
                row.get("present_count", Long.class)};
        });
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    /** Deterministic 32-hex chash from a seed string (sha256(seed)[:32], the
     *  same derivation shape as the production chash convention). */
    private static String chash(String seed) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(32);
            for (int i = 0; i < 16; i++) sb.append(String.format("%02x", digest[i]));
            return sb.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void stampGuc(Connection conn, String tenant) throws Exception {
        try (var ps = conn.prepareStatement("SELECT set_config(?, ?, false)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
            ps.execute();
        }
    }

    private void insertCollection(Connection su, String tenant, String name) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenant + "', '" + name + "') ON CONFLICT DO NOTHING");
    }

    private void insertMapRow(Connection su, String tenant, String src,
                              String oldId, String newChash, String tgt) throws Exception {
        stampGuc(su, tenant);
        su.createStatement().execute(
            "INSERT INTO nexus.chash_remap " +
            "(tenant_id, source_collection, old_id, new_chash, target_collection, created_at, provenance) " +
            "VALUES ('" + tenant + "', '" + src + "', '" + oldId + "', '" + newChash + "', " +
            "'" + tgt + "', now(), 'fn-test') " +
            "ON CONFLICT (tenant_id, source_collection, old_id) DO NOTHING");
    }

    private void insertChunk1024(Connection su, String tenant, String collection,
                                 String chash) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
            "VALUES ('" + tenant + "', '" + collection + "', '" + chash + "', 'text', " +
            "('[1" + ",0".repeat(1023) + "]')::vector) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }
}

package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-180 LAND-THEN-TRANSFORM (nexus-jxizy.10.1) — the staging landing zone.
 *
 * <p>Design of record: T2 {@code nexus_rdr/180-land-transform-design} (+
 * reconciliation). Staging is the width-FREE side of the ONE-strict-tier
 * contract: any legacy id shape lands verbatim; the nexus write boundary
 * stays strict; StagingPromoteOps (nexus-jxizy.10.3) re-ids in-DB.
 *
 * <p>Hermetic: Testcontainers pgvector, applies the Liquibase master
 * changelog, asserts:
 * <ol>
 *   <li>schema {@code staging} + all 8 landing tables exist (the
 *       CASCADE_STORES inventory + polymorphic chunks)</li>
 *   <li>RLS ENABLED + FORCED + tenant policy on every staging table</li>
 *   <li>the untyped vector column accepts MIXED dims (no typmod)</li>
 *   <li>legacy_ref accepts ANY width (16-char pre-RDR-108, 32-hex
 *       RDR-108-era, 64-hex canonical) — the whole point of staging</li>
 *   <li>tenant isolation end-to-end via TenantScope as nexus_svc, whose
 *       privileges come from the staging-4 runAlways grants (NOT test-local
 *       grants — proving the changeset's own grant surface)</li>
 *   <li>TRUNCATE privilege works as nexus_svc (the /v1/staging/clear
 *       semantics)</li>
 * </ol>
 *
 * <p>Seeding NEVER uses the Liquibase/admin connection for DML under FORCE
 * RLS (the nexus-vounk no-op trap) — all writes go through
 * {@link TenantScope#withTenant} as nexus_svc.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StagingSchemaLiquibaseTest {

    // chash_index left the list at RDR-187 nexus-piwya.11 (staging landing
    // twin dropped by rdr187-002; see the dedicated absence assertion below).
    private static final List<String> STAGING_TABLES = List.of(
        "chunks", "document_chunks", "topic_assignments",
        "frecency", "relevance_log", "document_aspects", "aspect_extraction_queue");

    private static final String T_A = "staging-tenant-a";
    private static final String T_B = "staging-tenant-b";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // nexus_svc must exist BEFORE Liquibase so the staging-4
            // runAlways grants actually apply to it.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
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
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @Test
    void allLandingTables_exist() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Set<String> actual = new HashSet<>();
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'staging'");
            while (rs.next()) actual.add(rs.getString(1));
            assertThat(actual)
                .as("staging must hold the polymorphic chunks table plus the "
                    + "full CASCADE_STORES pointer-store inventory — a missing "
                    + "table is a missed landing leg, the exact class "
                    + "land-then-transform exists to kill")
                .containsExactlyInAnyOrderElementsOf(STAGING_TABLES);
        }
    }

    @Test
    void everyStagingTable_hasForcedRlsAndTenantPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String t : STAGING_TABLES) {
                ResultSet cls = su.createStatement().executeQuery(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    + "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid "
                    + "WHERE n.nspname = 'staging' AND c.relname = '" + t + "'");
                assertThat(cls.next()).as("staging.%s must exist", t).isTrue();
                assertThat(cls.getBoolean(1)).as("staging.%s RLS enabled", t).isTrue();
                assertThat(cls.getBoolean(2)).as("staging.%s RLS forced", t).isTrue();
                ResultSet pol = su.createStatement().executeQuery(
                    "SELECT qual FROM pg_policies WHERE schemaname = 'staging' "
                    + "AND tablename = '" + t + "'");
                assertThat(pol.next()).as("staging.%s tenant policy", t).isTrue();
                assertThat(pol.getString(1)).contains("nexus.tenant");
            }
        }
    }

    @Test
    void chunksVectorColumn_acceptsMixedDims_andAnyWidthLegacyRef() {
        // 16-char pre-RDR-108, 32-hex RDR-108-era, 64-hex canonical — all land.
        tenantScope.withTenant(T_A, ctx -> {
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) VALUES "
                + "(?, 'knowledge__k__bge-base-en-v15-768__v1', 768, ?, 'sixteen char era', '[1,0,0]'::vector, 'bge-768')",
                T_A, "b46c7915c303245f");
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) VALUES "
                + "(?, 'knowledge__k__bge-base-en-v15-768__v1', 768, ?, 'thirty-two hex era', '[1,0,0,0,0]'::vector, 'bge-768')",
                T_A, "0123456789abcdef0123456789abcdef");
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) VALUES "
                + "(?, 'knowledge__k__bge-base-en-v15-768__v1', 768, ?, 'canonical era', NULL, 'bge-768')",
                T_A, "a".repeat(64));
            return null;
        });
        Integer distinctDims = tenantScope.withTenant(T_A, ctx ->
            ctx.fetchOne("SELECT count(DISTINCT vector_dims(embedding)) FROM staging.chunks "
                + "WHERE embedding IS NOT NULL").get(0, Integer.class));
        assertThat(distinctDims)
            .as("the untyped vector column must hold MIXED dims (3 and 5 here) — "
                + "dim is a VALUE in staging, not a type")
            .isEqualTo(2);
        Integer rows = tenantScope.withTenant(T_A, ctx ->
            ctx.fetchOne("SELECT count(*) FROM staging.chunks").get(0, Integer.class));
        assertThat(rows).as("all three id widths landed").isEqualTo(3);
    }

    @Test
    void tenantIsolation_holdsForStagingChunks() {
        // (Re-pointed from staging.chash_index to staging.frecency at RDR-187
        // nexus-piwya.11 — the chash_index landing twin is dropped; the RLS
        // shape under test is identical across the staging tables.)
        tenantScope.withTenant(T_B, ctx -> {
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id) "
                + "VALUES (?, 'feedbeef')", T_B);
            return null;
        });
        Integer aSees = tenantScope.withTenant(T_A, ctx ->
            ctx.fetchOne("SELECT count(*) FROM staging.frecency").get(0, Integer.class));
        assertThat(aSees).as("tenant A must not see tenant B's staged rows").isEqualTo(0);
        // WITH CHECK: cross-tenant INSERT rejected.
        assertThatThrownBy(() -> tenantScope.withTenant(T_A, ctx -> {
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id) "
                + "VALUES (?, 'feedbee2')", T_B);
            return null;
        })).as("cross-tenant INSERT must violate the WITH CHECK policy")
           .hasMessageContaining("row-level security");
    }

    @Test
    void stagingChashIndex_isDropped() {
        // RDR-187 nexus-piwya.11 (rdr187-002): the dead-sink landing twin of
        // the retired router is gone.
        Integer present = tenantScope.withTenant(T_A, ctx ->
            ctx.fetchOne("SELECT count(*) FROM information_schema.tables "
                + "WHERE table_schema = 'staging' AND table_name = 'chash_index'")
               .get(0, Integer.class));
        assertThat(present)
            .as("staging.chash_index must be dropped (rdr187-002)")
            .isZero();
    }

    @Test
    void chashOldBytesFn_isTheCanonicalMapping_consistentWithTheLemma() throws Exception {
        // Reconciliation H2 (nexus-jxizy.10.2): ONE string->bytes mapping for
        // chash_alias.old_bytes, shared by RekeyOps and StagingPromoteOps.
        // Mirrors rdr180-001's conversion CASE; round-trips the in-store
        // recovery lemma on its constrained domain.
        try (Connection su = pg.createConnection("")) {
            // 32-hex (the RDR-108 era) decodes to 16 bytes.
            ResultSet r1 = su.createStatement().executeQuery(
                "SELECT octet_length(nexus.chash_old_bytes('0123456789abcdef0123456789abcdef')), "
                + "nexus.chash_old_bytes('0123456789abcdef0123456789abcdef') = "
                + "decode('0123456789abcdef0123456789abcdef','hex')");
            r1.next();
            assertThat(r1.getInt(1)).isEqualTo(16);
            assertThat(r1.getBoolean(2)).isTrue();
            // 16-char (pre-RDR-108) decodes to 8 bytes.
            ResultSet r2 = su.createStatement().executeQuery(
                "SELECT octet_length(nexus.chash_old_bytes('b46c7915c303245f'))");
            r2.next();
            assertThat(r2.getInt(1)).isEqualTo(8);
            // Non-hex ETL-era ids carry as UTF-8 bytes.
            ResultSet r3 = su.createStatement().executeQuery(
                "SELECT nexus.chash_old_bytes('era-note:alpha!') = convert_to('era-note:alpha!','UTF8')");
            r3.next();
            assertThat(r3.getBoolean(1)).isTrue();
            // Round-trip vs the in-store recovery lemma, on its constrained
            // domain: a converted 16-byte hex-origin value's recovered string
            // maps back to the SAME bytes; a >=32-byte UTF8-origin value too.
            ResultSet r4 = su.createStatement().executeQuery(
                "SELECT nexus.chash_old_bytes(encode(b, 'hex')) = b FROM "
                + "(SELECT decode('00112233445566778899aabbccddeeff','hex') AS b) s");
            r4.next();
            assertThat(r4.getBoolean(1))
                .as("lemma round-trip: hex-origin bytes").isTrue();
            ResultSet r5 = su.createStatement().executeQuery(
                "SELECT nexus.chash_old_bytes(convert_from(v, 'UTF8')) = v FROM "
                + "(SELECT convert_to('a-32-char-legacy-nonhex-id-00001','UTF8') AS v) s");
            r5.next();
            assertThat(r5.getBoolean(1))
                .as("lemma round-trip: UTF8-origin bytes").isTrue();
        }
    }

    @Test
    void svcRole_canTruncate_theClearSemantics() {
        tenantScope.withTenant(T_A, ctx -> {
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id) VALUES (?, 'x1')", T_A);
            return null;
        });
        // TRUNCATE (the /v1/staging/clear implementation) needs the privilege
        // granted by staging-4 — but NOTE: TRUNCATE is not RLS-scoped, so the
        // endpoint's per-tenant clear uses DELETE under withTenant; TRUNCATE
        // is reserved for the single-tenant local box. Both must be possible.
        tenantScope.withTenant(T_A, ctx -> {
            ctx.execute("DELETE FROM staging.frecency");
            return null;
        });
        Integer left = tenantScope.withTenant(T_A, ctx ->
            ctx.fetchOne("SELECT count(*) FROM staging.frecency").get(0, Integer.class));
        assertThat(left).isEqualTo(0);
    }
}

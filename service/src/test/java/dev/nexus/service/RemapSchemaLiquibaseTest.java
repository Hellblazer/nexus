package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-186 bead nexus-146xx.3 — Liquibase chash_remap baseline integration test.
 *
 * <p>The PG twin of the client-side {@code chash_remap.db} migration artifact
 * (RDR-185 .16 lineage): the persisted old-id → new-chash map, mirrored 1:1
 * from the SQLite schema in {@code src/nexus/migration/wire_reid.py} with
 * tenant_id promoted from a DEFAULT-'' column to a first-class RLS
 * discriminator.
 *
 * <p><strong>RF-186-1 invariant (load-bearing):</strong> this table is a
 * RAW-FACT substrate ONLY. It must never grow a "converged" / "delivered" /
 * verdict column — a stored verdict consulted by rung detect() instead of
 * re-deriving collides with the Gap-4 two-mechanism pin
 * ({@code tests/upgrade/test_gap4_two_mechanisms.py}) regardless of substrate.
 * The exact-column-set assertion in test 1 is the structural tripwire: any
 * added column fails it and forces the reader back to this paragraph.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Applies the Liquibase master changelog and asserts:
 * <ol>
 *   <li>chash_remap exists with EXACTLY the mirrored + tenant columns</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; policy USING + WITH CHECK
 *       on the nexus.tenant GUC</li>
 *   <li>PK is (tenant_id, source_collection, old_id) — the SQLite natural key
 *       plus tenant</li>
 *   <li>CHECK rejects a new_chash whose length is not 32</li>
 *   <li>reverse index (tenant_id, new_chash) exists</li>
 *   <li>tenant isolation end-to-end via TenantScope</li>
 *   <li>BYPASSRLS (superuser) sees all tenants' rows — the integrity-count
 *       read path</li>
 *   <li>RLS fail-closed: unstamped service connection sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RemapSchemaLiquibaseTest {

    // RF-186-1: raw facts only. A verdict column added here MUST fail test 1.
    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "tenant_id", "source_collection", "old_id",
        "new_chash", "target_collection", "created_at", "provenance"
    );

    private static final String SVC_ROLE = "svc_remap_schema_test";
    private static final String SVC_PASS = "svc_remap_schema_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── Test 1: exact column set (the RF-186-1 structural tripwire) ──────────

    @Test
    void remapTable_hasExactColumnSet_noVerdictColumnEver() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "chash_remap", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            assertThat(actual)
                .as("nexus.chash_remap must have EXACTLY the mirrored raw-fact columns. " +
                    "RF-186-1: a 'converged'/'delivered'/verdict column is banned — a stored " +
                    "verdict consulted by rung detect() collides with the Gap-4 pin " +
                    "(test_gap4_two_mechanisms.py) regardless of substrate. Do not extend " +
                    "this set; the map holds raw facts a live computation interprets.")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void remapTable_rlsEnabledForcedAndPolicyOnTenantGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", "chash_remap");
            assertThat(cls).as("nexus.chash_remap must exist in pg_class").isNotNull();
            assertThat(cls.enabled())
                .as("RLS must be ENABLED").isTrue();
            assertThat(cls.forced())
                .as("RLS must be FORCED (owner is subject to policy too)").isTrue();

            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "chash_remap");
            assertThat(policies).as("a policy must exist on nexus.chash_remap").isNotEmpty();
            String qual = policies.get(0).qual();
            String withCheck = policies.get(0).withCheck();
            assertThat(qual)
                .as("USING predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(withCheck)
                .as("WITH CHECK predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(policies)
                .as("exactly one policy expected on nexus.chash_remap").hasSize(1);
        }
    }

    // ── Test 3: PK is (tenant_id, source_collection, old_id) ─────────────────

    @Test
    void remapTable_primaryKeyIsTenantSourceOldId() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", "chash_remap");
            String[] pk = new String[3];
            int count = 0;
            while (rs.next()) {
                int seq = rs.getInt("KEY_SEQ");
                pk[seq - 1] = rs.getString("COLUMN_NAME").toLowerCase();
                count++;
            }
            assertThat(count).as("PK must have exactly 3 columns").isEqualTo(3);
            assertThat(pk)
                .as("PK must be (tenant_id, source_collection, old_id) — the SQLite " +
                    "natural key (source_collection, old_id) plus the tenant discriminator")
                .containsExactly("tenant_id", "source_collection", "old_id");
        }
    }

    // ── Test 4: CHECK rejects a direct 16-byte (or any non-32-byte) insert ───

    /**
     * RDR-194 D3 (bead nexus-tk070.p2): new_chash is bytea now, CHECKed
     * {@code octet_length(new_chash) = 32} (remap-003-new-chash-bytea.xml),
     * replacing the pre-P2 TEXT length(32,64) CHECK this test used to pin.
     * A direct 16-byte insert is exactly the legacy-32-hex-decoded shape D3's
     * gate reasoning names (a 32-hex-char era fact decodes to 16 bytes) —
     * the case that must now be REJECTED rather than tolerated.
     */
    @Test
    void remapTable_checkRejects16ByteInsert() throws Exception {
        assertThatThrownBy(() -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                stampGuc(su, "check-tenant");
                DSL.using(su, SQLDialect.POSTGRES)
                   .insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION,
                           CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH, CHASH_REMAP.TARGET_COLLECTION,
                           CHASH_REMAP.CREATED_AT, CHASH_REMAP.PROVENANCE)
                   .values("check-tenant", "src-coll", "legacy-1", new byte[16],  // 16 bytes, not 32
                       "tgt-coll", OffsetDateTime.now(), "test")
                   .execute();
            }
        })
        .as("new_chash whose stored width != 32 bytes must be rejected by the CHECK "
            + "constraint (the legacy 16-byte-decoded shape D3's gate reasoning names)")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("check constraint");
    }

    /**
     * The companion positive case: a 32-byte value is accepted (this is the
     * conformant shape every live write now takes — RemapRepository always
     * binds exactly 32 bytes via {@code Chash.fromHex(...).toBytes()}).
     */
    @Test
    void remapTable_checkAccepts32ByteInsert() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            stampGuc(su, "check-tenant-ok");
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION,
                       CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH, CHASH_REMAP.TARGET_COLLECTION,
                       CHASH_REMAP.CREATED_AT, CHASH_REMAP.PROVENANCE)
               .values("check-tenant-ok", "src-coll", "legacy-1", new byte[32],
                   "tgt-coll", OffsetDateTime.now(), "test")
               .execute();
        }
    }

    // ── Test 5: reverse index (tenant_id, new_chash) ─────────────────────────

    @Test
    void remapTable_reverseIndexExists() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String def = PgCatalogProbes.indexDef(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "idx_chash_remap_new");
            assertThat(def)
                .as("reverse index idx_chash_remap_new must exist (mirrors SQLite; " +
                    "serves new_chash → old_id reverse lookups)")
                .isNotNull();
            assertThat(def).contains("tenant_id", "new_chash");
        }
    }

    // ── Test 6: tenant isolation end-to-end via TenantScope ──────────────────

    @Test
    void tenantIsolation_viaTenantScope() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "alpha", "coll-a", "old-a1", chashBytes((byte) 1));
            insertRow(su, "alpha", "coll-a", "old-a2", chashBytes((byte) 2));
            insertRow(su, "beta",  "coll-b", "old-b1", chashBytes((byte) 3));
            su.commit();
        }

        List<String> alphaIds = tenantScope.withTenant("alpha", ctx ->
            ctx.select(CHASH_REMAP.OLD_ID).from(CHASH_REMAP).orderBy(CHASH_REMAP.OLD_ID)
               .fetch(CHASH_REMAP.OLD_ID));
        assertThat(alphaIds)
            .as("tenant-alpha must see exactly its 2 rows")
            .containsExactly("old-a1", "old-a2");

        List<String> betaIds = tenantScope.withTenant("beta", ctx ->
            ctx.select(CHASH_REMAP.OLD_ID).from(CHASH_REMAP).orderBy(CHASH_REMAP.OLD_ID)
               .fetch(CHASH_REMAP.OLD_ID));
        assertThat(betaIds)
            .as("tenant-beta must see exactly its 1 row, none of alpha's")
            .containsExactly("old-b1");
    }

    // ── Test 7: BYPASSRLS (superuser) sees all tenants ───────────────────────

    @Test
    void bypassRls_superuserSeesAllTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "gamma-su", "coll-g", "old-g1", chashBytes((byte) 4));
            insertRow(su, "delta-su", "coll-d", "old-d1", chashBytes((byte) 5));
            su.commit();
        }

        try (Connection su = pg.createConnection("")) {
            Long tenants = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.countDistinct(CHASH_REMAP.TENANT_ID))
                .from(CHASH_REMAP)
                .where(CHASH_REMAP.TENANT_ID.in("gamma-su", "delta-su"))
                .fetchOne(0, Long.class);
            assertThat(tenants)
                .as("superuser (rolsuper → implicit RLS bypass) must see rows across " +
                    "tenants — the integrity-count read path (nexus-vounk shape)")
                .isEqualTo(2L);
        }
    }

    // ── Test 8: RLS fail-closed — unstamped connection sees zero rows ────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "failclosed-tenant", "coll-fc", "old-fc1", chashBytes((byte) 6));
            su.commit();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            Long cnt = DSL.using(svc, SQLDialect.POSTGRES)
                .selectCount().from(CHASH_REMAP).fetchOne(0, Long.class);
            assertThat(cnt)
                .as("unstamped service connection must see zero rows (RLS fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ── Test 9: WITH CHECK blocks cross-tenant INSERT ────────────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() throws Exception {
        assertThatThrownBy(() ->
            tenantScope.withTenant("epsilon", ctx ->
                ctx.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION,
                        CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH, CHASH_REMAP.TARGET_COLLECTION,
                        CHASH_REMAP.CREATED_AT, CHASH_REMAP.PROVENANCE)
                   .values("zeta",  // tenant_id mismatch — WITH CHECK must reject
                       "coll-x", "old-x1", chashBytes((byte) 7), "tgt-x",
                       OffsetDateTime.now(), "test")
                   .execute())
        )
        .as("INSERT with tenant_id != GUC value must be rejected by RLS WITH CHECK "
            + "(new_chash is a conformant 32-byte value so the CHECK constraint does "
            + "not mask the RLS failure this test targets)")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    private void stampGuc(Connection conn, String tenant) throws Exception {
        DSL.using(conn, SQLDialect.POSTGRES)
           .select(DSL.function("set_config", SQLDataType.VARCHAR,
               DSL.val(TenantConstants.GUC_NAME), DSL.val(tenant), DSL.inline(false)))
           .fetch();
    }

    /** Insert a map row via superuser connection (bypasses RLS for seeding). */
    private void insertRow(Connection su, String tenant, String sourceCollection,
                           String oldId, byte[] newChash) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
            DSL.val(TenantConstants.GUC_NAME), DSL.val(tenant), DSL.inline(true))).fetch();
        ctx.insertInto(CHASH_REMAP, CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION,
                CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH, CHASH_REMAP.TARGET_COLLECTION,
                CHASH_REMAP.CREATED_AT, CHASH_REMAP.PROVENANCE)
           .values(tenant, sourceCollection, oldId, newChash, "tgt-" + sourceCollection,
               OffsetDateTime.now(), "test-seed")
           .onConflict(CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
           .doNothing()
           .execute();
    }

    /** A conformant 32-byte new_chash value, distinct per fill byte (test fixtures only). */
    private static byte[] chashBytes(byte fill) {
        byte[] b = new byte[32];
        java.util.Arrays.fill(b, fill);
        return b;
    }
}

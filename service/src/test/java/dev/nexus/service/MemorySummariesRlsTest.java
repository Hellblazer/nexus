package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY_SUMMARIES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-207 bead nexus-l3yuc.1: row-level security on the memory-004 objects.
 *
 * <p>Row-level security is per table (RDR-207 research finding 4): the
 * {@code tenant_isolation} policy memory-001-3 put on {@code nexus.memory} does
 * nothing for {@code nexus.memory_summaries}, so memory-004-3 copies the whole
 * ENABLE + FORCE + policy block onto the new table. A changeset that carried
 * only {@code CREATE POLICY} would pass every existing RLS test, because those
 * tests name {@code nexus.memory}. This class names the new table and the new
 * column, and reads them on the two legs that each flag protects:
 *
 * <ul>
 *   <li><b>Service-role leg</b> (NOSUPERUSER NOBYPASSRLS, not the owner): a
 *       missing ENABLE shows up here as a cross-tenant read.</li>
 *   <li><b>Owner leg</b>: without FORCE the policy does not apply to the table
 *       owner, so only a read AS THE OWNER with another tenant's GUC stamped can
 *       catch a missing FORCE. The owner here is a NOSUPERUSER, NOBYPASSRLS
 *       role ({@link PgContainerHelper#bootstrapAdminRole}); the container
 *       superuser is BYPASSRLS and can seed, but cannot prove anything.</li>
 * </ul>
 *
 * <p>Non-vacuity: {@link #ownerLeg_isSensitiveToForce} flips FORCE off through
 * {@link PgContainerHelper#setForceRls} and asserts the owner leg then DOES see
 * the other tenant's rows, before restoring it. The ENABLE leg has no typed
 * toggle; its falsification (delete the ENABLE line, run this class, see
 * {@link #serviceRole_cannotSeeOtherTenantSummariesOrQuarantinedRows} go red)
 * was run by hand before this file was committed and is recorded in the bead.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemorySummariesRlsTest {

    private static final String ADMIN_ROLE = "nexus_admin_l3yuc_test";
    private static final String ADMIN_PASS = "nexus_admin_l3yuc_test_pass";
    private static final String SVC_ROLE = "svc_l3yuc_test";
    private static final String SVC_PASS = "svc_l3yuc_test_pass";

    private static final String TENANT_A = "l3yuc-tenant-a";
    private static final String TENANT_B = "l3yuc-tenant-b";
    private static final String PROJECT = "l3yuc-proj";

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;   // table OWNER, NOSUPERUSER NOBYPASSRLS
    HikariDataSource svcDs;     // DML-only service role, NOSUPERUSER NOBYPASSRLS
    TenantScope ownerScope;
    TenantScope svcScope;

    @BeforeAll
    void startAll() throws Exception {
        // Dedicated: bootstrapAdminRole reassigns table ownership, which is global
        // schema state and must not leak into the shared cluster.
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapAdminRole(su, ADMIN_ROLE, ADMIN_PASS);
        }
        adminDs = pool(ADMIN_ROLE, ADMIN_PASS, "l3yuc-owner");
        svcDs = pool(SVC_ROLE, SVC_PASS, "l3yuc-svc");
        ownerScope = new TenantScope(adminDs);
        svcScope = new TenantScope(svcDs);

        // Seed tenant A as the OWNER with A's GUC stamped (FORCE RLS applies to the
        // owner too, so WITH CHECK needs the stamp): one summary row, one quarantined
        // memory row and one live memory row.
        ownerScope.withTenant(TENANT_A, ctx -> {
            long liveId = ctx.insertInto(MEMORY,
                    MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                    MEMORY.TIMESTAMP, MEMORY.ACCESS_COUNT)
                .values(TENANT_A, PROJECT, "live row", "still live", OffsetDateTime.now(), 0)
                .returning(MEMORY.ID).fetchOne().getId();
            ctx.insertInto(MEMORY,
                    MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                    MEMORY.TIMESTAMP, MEMORY.ACCESS_COUNT, MEMORY.QUARANTINED_AT)
                .values(TENANT_A, PROJECT, "quarantined row", "past its ttl",
                    OffsetDateTime.now(), 0, OffsetDateTime.now())
                .execute();
            ctx.insertInto(MEMORY_SUMMARIES,
                    MEMORY_SUMMARIES.TENANT_ID, MEMORY_SUMMARIES.PROJECT, MEMORY_SUMMARIES.CONTENT,
                    MEMORY_SUMMARIES.SOURCE_IDS, MEMORY_SUMMARIES.MODEL, MEMORY_SUMMARIES.PRODUCED_BY)
                .values(TENANT_A, PROJECT, "summary of tenant A", new Long[] {liveId},
                    "test-model", "l3yuc")
                .execute();
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── Flags and policy on the new table ────────────────────────────────────

    @Test
    void memorySummaries_rlsEnabledForcedWithPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", "memory_summaries");
            assertThat(cls).as("nexus.memory_summaries must exist in pg_class").isNotNull();
            assertThat(cls.enabled())
                .as("ENABLE ROW LEVEL SECURITY must be set on nexus.memory_summaries").isTrue();
            assertThat(cls.forced())
                .as("FORCE ROW LEVEL SECURITY must be set on nexus.memory_summaries").isTrue();

            List<PgCatalogProbes.Policy> policies =
                PgCatalogProbes.policies(ctx, "nexus", "memory_summaries");
            assertThat(policies)
                .as("nexus.memory_summaries must carry its own RLS policy; the nexus.memory "
                    + "policy does not inherit").hasSize(1);
            PgCatalogProbes.Policy pol = policies.get(0);
            assertThat(pol.cmd()).as("policy must cover ALL commands").isEqualTo("ALL");
            assertThat(pol.qual()).as("USING must reference the tenant GUC").contains("current_setting");
            assertThat(pol.withCheck()).as("WITH CHECK must reference the tenant GUC").contains("current_setting");
        }
    }

    // ── Service-role leg: a missing ENABLE shows up here ─────────────────────

    @Test
    void serviceRole_cannotSeeOtherTenantSummariesOrQuarantinedRows() {
        // Positive control first: tenant A sees its own rows, so B's empties below
        // are isolation and not a mis-seeded fixture.
        assertThat(summariesSeenBy(svcScope, TENANT_A))
            .as("tenant A must see its own summary (positive control)").isEqualTo(1);
        assertThat(quarantinedSeenBy(svcScope, TENANT_A))
            .as("tenant A must see its own quarantined row (positive control)").isEqualTo(1);

        assertThat(summariesSeenBy(svcScope, TENANT_B))
            .as("service role as tenant B must not see tenant A's summaries").isZero();
        assertThat(quarantinedSeenBy(svcScope, TENANT_B))
            .as("service role as tenant B must not see tenant A's quarantined rows").isZero();
    }

    @Test
    void serviceRole_noGucStamp_failsClosedOnSummaries() throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            // Deliberately unstamped: current_setting('nexus.tenant', true) is NULL.
            int count = DSL.using(svc, SQLDialect.POSTGRES).fetchCount(MEMORY_SUMMARIES);
            assertThat(count)
                .as("an unstamped service connection must see zero summaries (fail-closed)")
                .isZero();
        }
    }

    // ── Owner leg: a missing FORCE shows up here ─────────────────────────────

    @Test
    void owner_cannotSeeOtherTenantSummariesOrQuarantinedRows() {
        assertThat(summariesSeenBy(ownerScope, TENANT_B))
            .as("the table OWNER as tenant B must not see tenant A's summaries "
                + "(FORCE ROW LEVEL SECURITY)").isZero();
        assertThat(quarantinedSeenBy(ownerScope, TENANT_B))
            .as("the table OWNER as tenant B must not see tenant A's quarantined rows "
                + "(FORCE ROW LEVEL SECURITY)").isZero();
    }

    @Test
    void ownerLeg_isSensitiveToForce() throws Exception {
        // The owner-leg assertion above proves nothing unless removing FORCE makes it
        // fail. Flip FORCE off as the owner, observe the cross-tenant read happen,
        // restore FORCE, observe it stop. Serialised on this class's own container.
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            PgContainerHelper.setForceRls(owner, MEMORY_SUMMARIES, false);
            PgContainerHelper.setForceRls(owner, MEMORY, false);
            try {
                assertThat(summariesSeenBy(ownerScope, TENANT_B))
                    .as("with NO FORCE the owner as tenant B sees tenant A's summary: the "
                        + "owner-leg assertion is FORCE-sensitive").isEqualTo(1);
                assertThat(quarantinedSeenBy(ownerScope, TENANT_B))
                    .as("with NO FORCE the owner as tenant B sees tenant A's quarantined row")
                    .isEqualTo(1);
            } finally {
                PgContainerHelper.setForceRls(owner, MEMORY, true);
                PgContainerHelper.setForceRls(owner, MEMORY_SUMMARIES, true);
            }
        }
        assertThat(summariesSeenBy(ownerScope, TENANT_B))
            .as("FORCE restored: the owner as tenant B sees nothing again").isZero();
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static int summariesSeenBy(TenantScope scope, String tenant) {
        Integer n = scope.withTenant(tenant, ctx -> ctx.fetchCount(MEMORY_SUMMARIES));
        return n;
    }

    private static int quarantinedSeenBy(TenantScope scope, String tenant) {
        Integer n = scope.withTenant(tenant,
            ctx -> ctx.fetchCount(MEMORY, MEMORY.QUARANTINED_AT.isNotNull()));
        return n;
    }

    private HikariDataSource pool(String user, String pass, String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);  // TenantScope toggles per borrow
        return new HikariDataSource(cfg);
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
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

import static dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-252 Step 1 (group-001-records.xml): row level security on {@code nexus.group_records}. The group tests filter
 * by tenant in their own WHERE clauses, so they pass with the policy dropped; this class is the one that fails when
 * ENABLE, FORCE or the {@code tenant_isolation} policy goes. Shaped on {@code MemorySchemaLiquibaseTest}: the
 * {@code pg_class} flags and {@code pg_policies}, then the behaviour as the service role (no superuser, no
 * BYPASSRLS): another tenant's rows are invisible, a row for another tenant cannot be written or moved, and a
 * connection with no tenant stamp sees nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupRecordsRlsTest {

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;

    @BeforeAll
    void up() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
    }

    @AfterAll
    void down() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private int count(String tenant, java.util.function.ToIntFunction<DSLContext> work) {
        Integer n = scope.withTenant(tenant, ctx -> work.applyAsInt(ctx));
        return n;
    }

    private void insert(DSLContext ctx, String tenant, String name) {
        ctx.insertInto(GROUP_RECORDS).set(GROUP_RECORDS.TENANT_ID, tenant).set(GROUP_RECORDS.NAME, name)
            .set(GROUP_RECORDS.LEADER, "i1").set(GROUP_RECORDS.FORMED_AT, OffsetDateTime.now()).execute();
    }

    @Test
    void groupRecordsHasRlsEnabledForcedAndTheTenantIsolationPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", "group_records");
            assertThat(cls).as("nexus.group_records must exist in pg_class").isNotNull();
            assertThat(cls.enabled()).as("relrowsecurity (ENABLE ROW LEVEL SECURITY)").isTrue();
            assertThat(cls.forced()).as("relforcerowsecurity (FORCE ROW LEVEL SECURITY)").isTrue();

            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "group_records");
            assertThat(policies).as("exactly the tenant_isolation policy").hasSize(1);
            PgCatalogProbes.Policy p = policies.get(0);
            assertThat(p.policyname()).isEqualTo("tenant_isolation");
            assertThat(p.cmd()).as("the policy covers every command").isEqualTo("ALL");
            assertThat(p.qual()).as("USING names the tenant stamp").contains("current_setting").contains("tenant_id");
            assertThat(p.withCheck()).as("WITH CHECK names the tenant stamp").contains("current_setting").contains("tenant_id");
        }
    }

    @Test
    void theServiceRoleSeesOnlyItsOwnTenantsGroupsAndCannotWriteAnothers() throws Exception {
        scope.withTenant("rls-a", ctx -> {
            insert(ctx, "rls-a", "crew");
            return null;
        });
        scope.withTenant("rls-b", ctx -> {
            insert(ctx, "rls-b", "crew");
            insert(ctx, "rls-b", "other");
            return null;
        });
        assertThat(count("rls-a", ctx -> ctx.fetchCount(GROUP_RECORDS))).as("tenant a reads its own row only").isEqualTo(1);
        assertThat(count("rls-b", ctx -> ctx.fetchCount(GROUP_RECORDS))).isEqualTo(2);
        // The WHERE clause names the other tenant, and the policy still hides its rows.
        assertThat(count("rls-a", ctx -> ctx.fetchCount(GROUP_RECORDS, GROUP_RECORDS.TENANT_ID.eq("rls-b"))))
            .as("a query that names tenant b sees none of b's rows").isZero();

        assertThatThrownBy(() -> scope.withTenant("rls-a", ctx -> {
            insert(ctx, "rls-b", "smuggled");
            return null;
        })).as("WITH CHECK: a row for another tenant is refused").hasMessageContaining("row-level security");
        assertThatThrownBy(() -> scope.withTenant("rls-a", ctx -> ctx.update(GROUP_RECORDS)
            .set(GROUP_RECORDS.TENANT_ID, "rls-b").where(GROUP_RECORDS.NAME.eq("crew")).execute()))
            .as("WITH CHECK: a row cannot be moved to another tenant").hasMessageContaining("row-level security");
        assertThat(count("rls-a", ctx -> ctx.update(GROUP_RECORDS).set(GROUP_RECORDS.PURPOSE, "x")
            .where(GROUP_RECORDS.TENANT_ID.eq("rls-b")).execute())).as("an update cannot reach another tenant's rows").isZero();
        assertThat(count("rls-a", ctx -> ctx.deleteFrom(GROUP_RECORDS).where(GROUP_RECORDS.TENANT_ID.eq("rls-b")).execute()))
            .as("nor a delete").isZero();
        assertThat(count("rls-b", ctx -> ctx.fetchCount(GROUP_RECORDS))).as("b's rows are all still there").isEqualTo(2);
    }

    @Test
    void aServiceConnectionWithNoTenantStampSeesNothing() throws Exception {
        scope.withTenant("rls-c", ctx -> {
            insert(ctx, "rls-c", "crew");
            return null;
        });
        try (Connection raw = ds.getConnection()) {
            assertThat(DSL.using(raw, SQLDialect.POSTGRES).fetchCount(GROUP_RECORDS)).as("fail closed: no stamp, no rows").isZero();
        }
    }
}

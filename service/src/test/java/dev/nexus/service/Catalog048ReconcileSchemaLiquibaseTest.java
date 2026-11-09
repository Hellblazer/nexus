// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-193 Part A1 (nexus-u5m5g.3): {@code catalog-048-reconcile.xml} adds the two landing tables
 * ({@code catalog_reconcile_runs}, {@code catalog_reconcile_rows}), {@code catalog_reconcile_commit(text, text)} and
 * {@code catalog_reconcile_reap(interval)}, and (catalog-048-4, fix round r1) the one link write
 * {@code catalog_link_write(...)} the commit's rename move calls. A walk: migrate to just before the changeset,
 * assert the objects are absent, apply, check columns, keys, forced row-level security, the tenant policy, the
 * function signatures and the grants, roll the four changesets back, reapply.
 */
class Catalog048ReconcileSchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    /** The last changeset of the changelog before catalog-048 (the tail it is placed after). */
    private static final String PRIOR_CHANGESET_ID = "vectors-056-2";
    private static final String LAST_TARGET_CHANGESET_ID = "catalog-048-4";

    @Test
    void addsTheTablesAndFunctions_rollsBackAndReapplies() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, PRIOR_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "catalog_reconcile_runs")).isFalse();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "catalog_reconcile_rows")).isFalse();
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_reconcile%")).isEmpty();
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_link_write")).isEmpty();
            }
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, LAST_TARGET_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                assertThat(columns(su, "catalog_reconcile_runs")).containsExactly(
                    "tenant_id:text:NO", "run_id:text:NO", "owner_tumbler:text:NO", "walk:text:NO",
                    "expected_rows:int4:NO", "state:text:NO", "outcome:jsonb:YES", "created_at:timestamptz:NO");
                assertThat(columns(su, "catalog_reconcile_rows")).containsExactly(
                    "tenant_id:text:NO", "run_id:text:NO", "seq:int4:NO", "kind:text:NO", "file_path:text:NO",
                    "content_type:text:NO", "title:text:NO", "physical_collection:text:NO", "head_hash:text:NO",
                    "source_mtime:float8:NO", "content_hash:text:NO", "base_metadata:jsonb:NO",
                    "stamp:jsonb:NO", "sensitive:bool:NO");

                assertThat(primaryKey(su, "catalog_reconcile_runs")).as("primary key (tenant_id, run_id)")
                    .isEqualTo("tenant_id,run_id");
                assertThat(primaryKey(su, "catalog_reconcile_rows")).as("primary key (tenant_id, run_id, file_path)")
                    .isEqualTo("tenant_id,run_id,file_path");
                assertThat(uniqueKeys(su, "catalog_reconcile_rows")).as("unique (tenant_id, run_id, seq)")
                    .containsExactly("tenant_id,run_id,seq");

                for (String t : List.of("catalog_reconcile_runs", "catalog_reconcile_rows")) {
                    var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", t);
                    assertThat(rls).as(t).isNotNull();
                    assertThat(rls.enabled()).as(t + " ENABLE ROW LEVEL SECURITY").isTrue();
                    assertThat(rls.forced()).as(t + " FORCE ROW LEVEL SECURITY").isTrue();
                    var policies = PgCatalogProbes.policies(ctx, "nexus", t);
                    assertThat(policies).as(t + " has the one tenant policy").hasSize(1);
                    assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
                    assertThat(policies.get(0).qual()).contains("nexus.tenant");
                    assertThat(policies.get(0).withCheck()).contains("nexus.tenant");
                }

                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_reconcile%"))
                    .containsExactlyInAnyOrder("catalog_reconcile_commit", "catalog_reconcile_reap");
                assertThat(routine(ctx, "catalog_reconcile_commit")).as("jsonb, plpgsql, VOLATILE, SECURITY INVOKER")
                    .containsExactly("v", false, "plpgsql", "jsonb");
                assertThat(routine(ctx, "catalog_reconcile_reap")).as("integer, plpgsql, VOLATILE, SECURITY INVOKER")
                    .containsExactly("v", false, "plpgsql", "int4");

                assertThat(execGranted(ctx, "nexus.catalog_reconcile_commit(text, text)"))
                    .as("nexus_svc may execute the commit").isTrue();
                assertThat(execGranted(ctx, "nexus.catalog_reconcile_reap(interval)"))
                    .as("nexus_svc may execute the reap").isTrue();

                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_link_write"))
                    .as("the one link write the commit calls (Step 3's batch link route reuses it)")
                    .containsExactly("catalog_link_write");
                assertThat(routine(ctx, "catalog_link_write")).as("text, plpgsql, VOLATILE, SECURITY INVOKER")
                    .containsExactly("v", false, "plpgsql", "text");
                assertThat(execGranted(ctx, "nexus.catalog_link_write(text, text, text, text, text, text, boolean)"))
                    .as("nexus_svc may execute the link write").isTrue();
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                try (Liquibase liquibase = new Liquibase(
                        MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(4, new Contexts(), new LabelExpression());
                }
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "catalog_reconcile_runs")).as("rollback drops the runs table").isFalse();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "catalog_reconcile_rows")).as("rollback drops the rows table").isFalse();
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_reconcile%")).as("rollback drops both functions").isEmpty();
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_link_write")).as("and the link write").isEmpty();
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
                liquibase.update(new Contexts(), new LabelExpression());
                su.setAutoCommit(true);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "catalog_reconcile_runs")).isTrue();
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_reconcile%")).hasSize(2);
                assertThat(PgCatalogProbes.routineNamesLike(ctx, "nexus", "catalog_link_write")).hasSize(1);
                // the runAlways grants loop is the last include, so only the full walk shows its DML grants
                assertThat(tableDmlGranted(ctx, "nexus.catalog_reconcile_runs")).as("the grants loop covers the runs table").isTrue();
                assertThat(tableDmlGranted(ctx, "nexus.catalog_reconcile_rows")).as("and the rows table").isTrue();
            }
        } finally {
            dedicated.stop();
        }
    }

    /** column_name:type_name:is_nullable in ordinal order, from the driver's metadata. */
    private static List<String> columns(Connection su, String table) throws Exception {
        List<String> out = new java.util.ArrayList<>();
        try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", table, null)) {
            while (rs.next()) {
                out.add(rs.getString("COLUMN_NAME") + ":" + rs.getString("TYPE_NAME") + ":" + rs.getString("IS_NULLABLE"));
            }
        }
        return out;
    }

    /** The primary key columns in key order, comma-joined. */
    private static String primaryKey(Connection su, String table) throws Exception {
        java.util.TreeMap<Short, String> cols = new java.util.TreeMap<>();
        try (ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", table)) {
            while (rs.next()) cols.put(rs.getShort("KEY_SEQ"), rs.getString("COLUMN_NAME"));
        }
        return String.join(",", cols.values());
    }

    /** The key columns, comma-joined, of each unique index other than the primary key. */
    private static List<String> uniqueKeys(Connection su, String table) throws Exception {
        java.util.Map<String, java.util.TreeMap<Short, String>> byIndex = new java.util.TreeMap<>();
        try (ResultSet rs = su.getMetaData().getIndexInfo(null, "nexus", table, true, false)) {
            while (rs.next()) {
                String name = rs.getString("INDEX_NAME");
                if (name == null || name.endsWith("_pk")) continue;
                byIndex.computeIfAbsent(name, k -> new java.util.TreeMap<>())
                    .put(rs.getShort("ORDINAL_POSITION"), rs.getString("COLUMN_NAME"));
            }
        }
        return byIndex.values().stream().map(m -> String.join(",", m.values())).toList();
    }

    /** volatility, security-definer flag, language, return type of the one overload of {@code name}. */
    private static List<Object> routine(DSLContext ctx, String name) {
        var traits = PgCatalogProbes.routineTraits(ctx, "nexus", name);
        assertThat(traits).as(name + " has one overload").hasSize(1);
        var t = traits.get(0);
        return List.of(t.volatility(), t.securityDefiner(), t.language(), t.returnType());
    }

    private static boolean execGranted(DSLContext ctx, String signature) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("has_function_privilege", Boolean.class,
            DSL.val("nexus_svc"), DSL.val(signature), DSL.val("EXECUTE"))).fetchOne(0, Boolean.class));
    }

    private static boolean tableDmlGranted(DSLContext ctx, String table) {
        for (String priv : List.of("SELECT", "INSERT", "UPDATE", "DELETE")) {
            if (!Boolean.TRUE.equals(ctx.select(DSL.function("has_table_privilege", Boolean.class,
                    DSL.val("nexus_svc"), DSL.val(table), DSL.val(priv))).fetchOne(0, Boolean.class))) return false;
        }
        return true;
    }

    private static void migrateUpTo(Connection conn, String targetChangesetId) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx).as(targetChangesetId + " must be present in the master changelog").isGreaterThanOrEqualTo(0);
        liquibase.update(idx + 1, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}

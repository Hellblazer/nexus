// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * bead nexus-h4w9o: {@code nexus.partition_copy_access} (vectors-030-1, redefined by vectors-039-1) copies the
 * parent table's ACL onto a new leaf or model partition. The parent {@code nexus.chunks} carries SELECT for
 * {@code nexus_diag} (BYPASSRLS), re-granted every boot by {@code grants-nexus-diag-3} for the
 * {@code diag_chash_conformance} view, and {@code grants-nexus-diag-2} strips leaves only at the NEXT boot. Before
 * vectors-039-1 a tenant or a model created between boots therefore got {@code nexus_diag} SELECT on its leaves.
 *
 * <p>The fixture is {@link GrantsNexusDiagViewAccessIntegrationTest}'s: two walks as a non-superuser owner, with a
 * superuser-created BYPASSRLS {@code nexus_diag}. Everything the test creates, it creates AFTER the second walk, so
 * nothing but the function under test stands between the parent's grant and the new relations. Non-vacuity: the
 * parent grant is asserted present first, and every list of relations checked is asserted non-empty.
 *
 * <p>No SQL text: the ACLs are read through JDBC metadata, the partition names come from the generated
 * {@code partition_name} routine, and the tenant and model are created through the generated tables and routines
 * (the test tree's raw-SQL ratchets).
 */
class PartitionCopyAccessDiagExclusionIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_pcadiag";
    private static final String ADMIN_PASS = "nexus_admin_pcadiag_pw";
    private static final String DIAG_ROLE = "nexus_diag";
    private static final String DIAG_PASS = "nexus_diag_pcadiag_pw";
    private static final String SVC_ROLE = "nexus_svc";

    private static final String TENANT = "h4w9o-tenant";
    private static final String MODEL = "h4w9o-model-1024";
    private static final List<String> PARENTS = List.of("chunks", "taxonomy_centroids");
    private static final List<String> DML = List.of("DELETE", "INSERT", "SELECT", "UPDATE");

    @Test
    void newTenantLeavesAndNewModelPartitionsCarryNoNexusDiagPrivilege() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(DIAG_ROLE, ADMIN_ROLE);
             Connection su = pg.createConnection("")) {
            provisionAndMigrate(pg, su);

            try (Connection admin = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);

                // Precondition: the parent holds the diag grant, so a copy that did not skip it would carry it.
                assertThat(privileges(admin, DIAG_ROLE, "chunks"))
                    .as("nexus_diag must hold SELECT on the nexus.chunks parent after the walk "
                        + "(grants-nexus-diag-3); without it this test proves nothing")
                    .contains("SELECT");
                assertThat(privileges(admin, SVC_ROLE, "chunks"))
                    .as("nexus_svc's DML on the nexus.chunks parent").containsAll(DML);

                // 1. A tenant created after boot: the service_tokens trigger creates its leaves.
                PgContainerHelper.seedServiceToken(ctx, "tok-h4w9o", TENANT, "h4w9o");
                for (String parent : PARENTS) {
                    List<String> leaves = tenantLeaves(admin, ctx, parent, TENANT);
                    assertThat(leaves).as("the new tenant's leaves under " + parent
                        + " (non-vacuity: the trigger made some)").isNotEmpty();
                    for (String leaf : leaves) {
                        assertThat(privileges(admin, DIAG_ROLE, leaf))
                            .as("nexus_diag on the new tenant's leaf " + leaf).isEmpty();
                        // Control: the skip is not a wholesale ACL drop. nexus_svc keeps the parent's DML.
                        assertThat(privileges(admin, SVC_ROLE, leaf))
                            .as("nexus_svc on the new tenant's leaf " + leaf + " matches its parent " + parent)
                            .isEqualTo(privileges(admin, SVC_ROLE, parent))
                            .containsAll(DML);
                    }
                }

                // 2. A model added after boot: create_model_partition copies the ACL onto the model partition
                // and, through partition_ensure_leaf, onto one leaf per existing tenant.
                ctx.insertInto(EMBEDDING_MODELS, EMBEDDING_MODELS.EMBEDDING_MODEL, EMBEDDING_MODELS.DIMENSION,
                        EMBEDDING_MODELS.PROVIDER)
                    .values(MODEL, 1024, "test")
                    .execute();
                for (String parent : PARENTS) {
                    String modelPartition = PartitionScratch.createModelPartition(ctx, parent, MODEL, true);
                    List<String> tree = List.of(
                        PartitionScratch.partitionName(ctx, parent, MODEL, null),
                        PartitionScratch.partitionName(ctx, parent, MODEL, "default"),
                        PartitionScratch.partitionName(ctx, parent, MODEL, TENANT));
                    assertThat(modelPartition).as("create_model_partition's answer").endsWith(tree.get(0));
                    assertThat(relationNames(admin, parent))
                        .as("the new model partition and its leaves exist "
                            + "(non-vacuity: itself, default's leaf and " + TENANT + "'s leaf)")
                        .containsAll(tree);
                    for (String rel : tree) {
                        assertThat(privileges(admin, DIAG_ROLE, rel))
                            .as("nexus_diag on " + rel + " under the new model partition of " + parent).isEmpty();
                        assertThat(privileges(admin, SVC_ROLE, rel))
                            .as("nexus_svc on " + rel + " matches " + parent)
                            .isEqualTo(privileges(admin, SVC_ROLE, parent))
                            .containsAll(DML);
                    }
                }

                // 3. The parent keeps its diag grant: the view still reads through it.
                assertThat(privileges(admin, DIAG_ROLE, "chunks"))
                    .as("the nexus.chunks parent keeps nexus_diag's SELECT").contains("SELECT");
            }
        }
    }

    /** Privileges the role holds on nexus.&lt;table&gt; by an explicit ACL entry (JDBC metadata), sorted. */
    private static List<String> privileges(Connection c, String role, String table) throws Exception {
        TreeSet<String> out = new TreeSet<>();
        try (ResultSet rs = c.getMetaData().getTablePrivileges(null, "nexus", table)) {
            while (rs.next()) {
                if (role.equals(rs.getString("GRANTEE"))) {
                    out.add(rs.getString("PRIVILEGE"));
                }
            }
        }
        return new ArrayList<>(out);
    }

    /** Every relation in schema nexus whose name starts with the parent's: the parent, its model partitions, leaves. */
    private static List<String> relationNames(Connection c, String parent) throws Exception {
        List<String> out = new ArrayList<>();
        try (ResultSet rs = c.getMetaData().getTables(null, "nexus", parent + "%", null)) {
            while (rs.next()) {
                out.add(rs.getString("TABLE_NAME"));
            }
        }
        return out;
    }

    /**
     * The tenant's leaves under every model partition of the parent: the relations whose name ends in the tenant's
     * suffix ({@code _t_<hash>}, the same under every model, read off the naming function).
     */
    private static List<String> tenantLeaves(Connection c, DSLContext ctx, String parent, String tenant)
            throws Exception {
        String sample = PartitionScratch.partitionName(ctx, parent, PartitionScratch.CODE_3, tenant);
        String suffix = sample.substring(sample.indexOf("_t_"));
        List<String> out = new ArrayList<>();
        for (String name : relationNames(c, parent)) {
            if (name.endsWith(suffix)) {
                out.add(name);
            }
        }
        return out;
    }

    /** Same two-boot provisioning as {@link GrantsNexusDiagViewAccessIntegrationTest}. */
    private static void provisionAndMigrate(PostgreSQLContainer<?> pg, Connection su) throws Exception {
        su.setAutoCommit(true);
        PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        PgContainerHelper.createLoginRole(su, DIAG_ROLE, DIAG_PASS, true);
        liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
        liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
    }

    private static void liquibaseUpdate(String url, String user, String pass) throws Exception {
        try (Connection conn = DriverManager.getConnection(url, user, pass)) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn)));
            lb.update(new Contexts());
        }
    }
}

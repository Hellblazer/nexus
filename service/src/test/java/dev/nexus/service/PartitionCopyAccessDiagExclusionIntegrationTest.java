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
import org.assertj.core.api.SoftAssertions;
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
 * nothing but the function under test stands between the parent's grant and the new relations. Three paths reach
 * the function: a tenant's leaves (the service_tokens trigger), a new model partition (create_model_partition) and
 * partition_sync_access; each is checked, softly, so a pre-fix run shows every path red. Non-vacuity: the parent
 * grant is asserted present first, and every list of relations checked is asserted non-empty. Only nexus.chunks
 * carries the diag grant; the taxonomy_centroids parent has none, so its checks are a guard, not evidence. A
 * PUBLIC grant on the chunks parent pins that a grantee with no pg_roles row is still copied.
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
    private static final String PUBLIC = "PUBLIC";
    private static final String PUBLIC_PRIVILEGE = "REFERENCES";

    @Test
    void newTenantLeavesNewModelPartitionsAndSyncedLeavesCarryNoNexusDiagPrivilege() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(DIAG_ROLE, ADMIN_ROLE);
             Connection su = pg.createConnection("")) {
            provisionAndMigrate(pg, su);

            try (Connection admin = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);

                // Precondition: the parent holds the diag grant, so a copy that did not skip it would carry it.
                // Only nexus.chunks has one (grants-nexus-diag-3); the taxonomy_centroids parent has none, so its
                // assertions below guard against a future parent grant and are not evidence for this fix.
                assertThat(privileges(admin, DIAG_ROLE, "chunks"))
                    .as("nexus_diag must hold SELECT on the nexus.chunks parent after the walk "
                        + "(grants-nexus-diag-3); without it this test proves nothing")
                    .contains("SELECT");
                assertThat(privileges(admin, SVC_ROLE, "chunks"))
                    .as("nexus_svc's DML on the nexus.chunks parent").containsAll(DML);
                // Control for PUBLIC, which has no pg_roles row: a grant to it must still be copied.
                ctx.grant(DSL.privilege(PUBLIC_PRIVILEGE)).on(DSL.table(DSL.name("nexus", "chunks"))).toPublic()
                    .execute();
                assertThat(privileges(admin, PUBLIC, "chunks"))
                    .as("PUBLIC's " + PUBLIC_PRIVILEGE + " on the nexus.chunks parent").contains(PUBLIC_PRIVILEGE);

                // Soft: a pre-fix run reports every path's failure, not only the first.
                SoftAssertions soft = new SoftAssertions();

                // 1. A tenant created after boot: the service_tokens trigger creates its leaves.
                PgContainerHelper.seedServiceToken(ctx, "tok-h4w9o", TENANT, "h4w9o");
                for (String parent : PARENTS) {
                    List<String> leaves = tenantLeaves(admin, ctx, parent, TENANT);
                    assertThat(leaves).as("the new tenant's leaves under " + parent
                        + " (non-vacuity: the trigger made some)").isNotEmpty();
                    for (String leaf : leaves) {
                        checkCopied(soft, admin, "tenant leaf", parent, leaf);
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
                    List<String> tree = modelTree(ctx, parent);
                    assertThat(modelPartition).as("create_model_partition's answer").endsWith(tree.get(0));
                    assertThat(relationNames(admin, parent))
                        .as("the new model partition and its leaves exist "
                            + "(non-vacuity: itself, default's leaf and " + TENANT + "'s leaf)")
                        .containsAll(tree);
                    for (String rel : tree) {
                        checkCopied(soft, admin, "model partition tree", parent, rel);
                    }
                }

                // 3. partition_sync_access (SchemaMigrator.repairPartitionAccessDrift's call) revokes and
                // re-copies the parent's ACL on every partition: it must not put nexus_diag back.
                for (String parent : PARENTS) {
                    int synced = PartitionScratch.syncAccess(ctx, parent, true);
                    assertThat(synced).as("partition_sync_access on " + parent
                        + " (non-vacuity: it touched partitions)").isPositive();
                    List<String> rels = new ArrayList<>(tenantLeaves(admin, ctx, parent, TENANT));
                    rels.addAll(modelTree(ctx, parent));
                    for (String rel : rels) {
                        checkCopied(soft, admin, "after partition_sync_access", parent, rel);
                    }
                }

                // 4. The parent keeps its diag grant: the view still reads through it.
                soft.assertThat(privileges(admin, DIAG_ROLE, "chunks"))
                    .as("the nexus.chunks parent keeps nexus_diag's SELECT").contains("SELECT");
                soft.assertAll();
            }
        }
    }

    /**
     * The skip is exact: nexus_diag (BYPASSRLS) gets nothing on {@code rel}, while nexus_svc gets exactly its parent
     * privileges and PUBLIC (no pg_roles row) keeps the grant the test put on the nexus.chunks parent.
     */
    private static void checkCopied(SoftAssertions soft, Connection admin, String path, String parent, String rel)
            throws Exception {
        soft.assertThat(privileges(admin, DIAG_ROLE, rel))
            .as(path + ": nexus_diag on " + rel + " under " + parent).isEmpty();
        soft.assertThat(privileges(admin, SVC_ROLE, rel))
            .as(path + ": nexus_svc on " + rel + " matches its parent " + parent)
            .isEqualTo(privileges(admin, SVC_ROLE, parent))
            .containsAll(DML);
        if (parent.equals("chunks")) {
            soft.assertThat(privileges(admin, PUBLIC, rel))
                .as(path + ": PUBLIC on " + rel + " keeps the parent's " + PUBLIC_PRIVILEGE)
                .contains(PUBLIC_PRIVILEGE);
        }
    }

    /** The model partition of {@link #MODEL} under the parent, then its default leaf and the tenant's leaf. */
    private static List<String> modelTree(DSLContext ctx, String parent) {
        return List.of(
            PartitionScratch.partitionName(ctx, parent, MODEL, null),
            PartitionScratch.partitionName(ctx, parent, MODEL, "default"),
            PartitionScratch.partitionName(ctx, parent, MODEL, TENANT));
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

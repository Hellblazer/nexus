// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.datamove.ShippedMoveRegistrations;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.CompositeResourceAccessor;
import liquibase.resource.DirectoryResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-f3yqd.2.12: a database migrated before {@code nexus.data_moves} holds the old
 * {@code grants-nexus-svc-1} text. This boots the new changelog on such a database and checks that the migration
 * succeeds, the registry exists, and {@code nexus_svc} holds no privilege on it.
 *
 * <p>This is also the only test that exercises the REVOKE in {@code datamoves-001-1}. On a fresh walk the default
 * ACL ({@code ALTER DEFAULT PRIVILEGES ... TO nexus_svc}) is installed by {@code grants-nexus-svc-1}, the last
 * include, after {@code data_moves} is created, so there is nothing to revoke. Here the default ACL already exists
 * when the table is created, so without the REVOKE {@code nexus_svc} would hold DML on it.
 *
 * <p>The old state is built by running the changelog as it stood at a27ab2055: the current master without the
 * {@code datamoves-001} include, and the current {@code grants-nexus-svc.xml} with the loop's exclusion and the
 * {@code validCheckSum} removed, filed under the real file's name ({@code logicalFilePath}) so Liquibase matches its
 * rows to the new file's changesets by identity, as it does on a real upgrade. The walk is checked to leave the old
 * text's checksum ({@link GrantsNexusSvcChecksumTest#OLD_CHECKSUM}) in {@code databasechangelog}, which proves the
 * derived text is the shipped one, not just something like it. The boot is {@link SchemaMigrator#migrate}, what the
 * engine calls.
 */
class GrantsNexusSvcUpgradeIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_svcgrants_upgrade";
    private static final String ADMIN_PASS = "nexus_admin_svcgrants_upgrade_pw";
    private static final String SVC_ROLE = "nexus_svc";
    private static final String GRANTS_ID = "grants-nexus-svc-1";
    private static final String GRANTS_FILE = "db/changelog/grants-nexus-svc.xml";
    private static final String OLD_GRANTS_FILE = "db/old-grants/grants-nexus-svc.xml";
    private static final String DATAMOVES_INCLUDE = "<include file=\"db/changelog/datamoves-001-registry.xml\"/>";
    private static final String REVOKE_MARKER = "-- nexus.data_moves (RDR-246) is admin-connection only: nexus_svc holds NO privilege";
    private static final List<String> DML = List.of("SELECT", "INSERT", "UPDATE", "DELETE");

    @TempDir
    Path tmp;

    @Test
    void aDatabaseMigratedBeforeTheRegistryBootsOnTheNewText_andNexusSvcStaysWithoutAPrivilege() throws Exception {
        writeOldChangelog();
        try (var pg = PgContainerHelper.startEmptyDatabase();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);

            // 1. the old state: the changelog as it stood before the registry
            update(pg.getJdbcUrl(), "old-master.xml");
            assertThat(PgCatalogProbes.relationOwner(sctx, "nexus", "data_moves"))
                .as("the old walk has no registry").isNull();
            assertThat(md5sums(sctx, GRANTS_ID))
                .as("the old walk stamps the old text's checksum, one row, filed under the real file's name")
                .containsExactly(GrantsNexusSvcChecksumTest.OLD_CHECKSUM);
            assertThat(filenames(sctx, GRANTS_ID)).containsExactly(GRANTS_FILE);
            assertThat(hasTablePriv(sctx, "nexus.chunks", "INSERT"))
                .as("non-vacuity: the old loop granted nexus_svc a sibling table").isTrue();
            // The default ACL is what makes this test a falsifier of the changeset's REVOKE: a table the admin role
            // creates NOW carries nexus_svc DML from the moment it exists, as data_moves will in the upgrade below.
            // (On a fresh walk the default ACL is installed by grants-nexus-svc-1, after data_moves, so the REVOKE
            // has nothing to remove and a test that only walks fresh cannot see it.)
            try (Connection as = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                DSLContext actx = DSL.using(as, SQLDialect.POSTGRES);
                actx.createTable(DSL.name("nexus", "default_acl_probe")).column("id", SQLDataType.INTEGER).execute();
                try {
                    assertThat(hasTablePriv(sctx, "nexus.default_acl_probe", "INSERT"))
                        .as("non-vacuity: a table the admin creates after the old walk carries nexus_svc DML "
                            + "through the default ACL").isTrue();
                } finally {
                    actx.dropTable(DSL.name("nexus", "default_acl_probe")).execute();
                }
            }

            // 2. the upgrade: the engine's own boot, on the new changelog
            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(ADMIN_ROLE);
            cfg.setPassword(ADMIN_PASS);
            cfg.setMaximumPoolSize(2);
            try (var ds = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                assertThatCode(() -> SchemaMigrator.migrate(ds))
                    .as("a database holding the old grants-nexus-svc-1 text must boot on the new one")
                    .doesNotThrowAnyException();
            }

            assertThat(PgCatalogProbes.relationOwner(sctx, "nexus", "data_moves"))
                .as("the registry exists after the upgrade, owned by the migrating role").isEqualTo(ADMIN_ROLE);
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus.data_moves", priv))
                    .as("after the upgrade nexus_svc holds no " + priv + " on nexus.data_moves").isFalse();
            }
            assertThat(hasTablePriv(sctx, "nexus.chunks", "INSERT"))
                .as("non-vacuity: the loop ran again and still grants a sibling table").isTrue();
            assertThat(md5sums(sctx, GRANTS_ID))
                .as("the boot re-stamps the row with the new text's checksum, still one row")
                .containsExactly(GrantsNexusSvcChecksumTest.NEW_CHECKSUM);
        }
    }

    /**
     * nexus-f3yqd.2.17: a rollback to engine-service-v0.1.194 re-grants DML on the registry (that loop has no exclusion), and
     * the exclusion alone does not take it back on the roll-forward. The boot after it must.
     */
    @Test
    void aRollbackThatRegrantsDmlOnTheRegistry_isUndoneByTheNextBoot() throws Exception {
        try (var pg = PgContainerHelper.startEmptyDatabase();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            migrate(pg.getJdbcUrl());
            assertThat(hasAnyPrivilege(sctx)).as("a fresh boot leaves nexus_svc nothing on the registry").isFalse();

            // what 0.1.194's loop does on its boot
            sctx.grant(DSL.privilege("SELECT"), DSL.privilege("INSERT"), DSL.privilege("UPDATE"), DSL.privilege("DELETE"))
                .on(DSL.table(DSL.name("nexus", "data_moves"))).to(DSL.role(DSL.name(SVC_ROLE))).execute();
            assertThat(hasAnyPrivilege(sctx)).as("non-vacuity: the re-grant took").isTrue();
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus.data_moves", priv)).as(priv + " re-granted").isTrue();
            }

            migrate(pg.getJdbcUrl());

            assertThat(hasAnyPrivilege(sctx)).as("the next boot takes every privilege back").isFalse();
            assertThat(hasTablePriv(sctx, "nexus.chunks", "INSERT"))
                .as("non-vacuity: the loop still grants a sibling table").isTrue();
            assertThat(PgCatalogProbes.relationOwner(sctx, "nexus", "data_moves")).isEqualTo(ADMIN_ROLE);
        }
    }

    /** The REVOKE's second branch: a grant on a column only, with nothing in the table's own ACL, is taken back too. */
    @Test
    void aColumnOnlyGrantToNexusSvcOnTheRegistry_isRevokedAtBoot() throws Exception {
        try (var pg = PgContainerHelper.startEmptyDatabase();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            migrate(pg.getJdbcUrl());

            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-data-moves-column-grant.xml",
                "databasechangelog_test_data_moves_column_grant", java.util.Map.of());
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus.data_moves", priv))
                    .as("non-vacuity: the table's own ACL gives nexus_svc no " + priv).isFalse();
            }
            assertThat(hasAnyPrivilege(sctx)).as("non-vacuity: the column grant took").isTrue();

            migrate(pg.getJdbcUrl());

            assertThat(hasAnyPrivilege(sctx)).as("the next boot takes the column grant back").isFalse();
        }
    }

    /**
     * The single-role posture: where the migrating role is nexus_svc itself it owns the registry, and an owner that
     * revoked its own rights would lock out the one connection that works it. The changeset, replayed as nexus_svc on a
     * registry nexus_svc owns, leaves its privileges alone.
     */
    @Test
    void whereTheMigratingRoleIsNexusSvcItself_theRevokeIsSkipped() throws Exception {
        try (var pg = PgContainerHelper.startEmptyDatabase();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-single-role-registry.xml",
                "databasechangelog_test_single_role_registry", java.util.Map.of());
            DSLContext sctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.relationOwner(sctx, "nexus", "data_moves")).isEqualTo(SVC_ROLE);
            assertThat(hasAnyPrivilege(sctx)).as("non-vacuity: the owner holds its own privileges").isTrue();

            try (Connection svc = DriverManager.getConnection(pg.getJdbcUrl(), SVC_ROLE, PgContainerHelper.SVC_PASSWORD)) {
                PgContainerHelper.replayChangeset(svc, GRANTS_FILE, GRANTS_ID);
            }

            assertThat(hasAnyPrivilege(sctx)).as("the owner keeps its privileges").isTrue();
            for (String priv : DML) {
                assertThat(hasTablePriv(sctx, "nexus.data_moves", priv)).as(priv).isTrue();
            }
        }
    }

    /** What the registry's ACL gives nexus_svc: any table privilege, or any column privilege. */
    private static boolean hasAnyPrivilege(DSLContext ctx) {
        for (String priv : List.of("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")) {
            if (hasTablePriv(ctx, "nexus.data_moves", priv)) {
                return true;
            }
        }
        var column = DSL.function("has_any_column_privilege", SQLDataType.BOOLEAN,
            DSL.val(SVC_ROLE), DSL.val("nexus.data_moves"), DSL.val("SELECT,INSERT,UPDATE,REFERENCES"));
        return Boolean.TRUE.equals(ctx.select(column).fetchOne(column));
    }

    private static void migrate(String url) {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        try (var ds = new com.zaxxer.hikari.HikariDataSource(cfg)) {
            SchemaMigrator.migrate(ds);
        }
    }

    /** The text of {@link #REVOKE_MARKER} to the end of its DO block, cut out: the changeset as it stood before the REVOKE. */
    static String withoutTheRevoke(String grants) {
        int from = grants.indexOf(REVOKE_MARKER);
        assertThat(from).as("the changeset carries the REVOKE block").isPositive();
        int opening = grants.indexOf("$$", from);   // the DO block's dollar quote, then its close
        int to = grants.indexOf("$$;\n", opening + 2) + "$$;\n".length();
        return grants.substring(0, from) + grants.substring(to + 1);
    }

    /** Writes old-master.xml and the old grants file under {@link #tmp}. */
    private void writeOldChangelog() throws Exception {
        String master = read("db/changelog/db.changelog-master.xml");
        assertThat(master).as("the master carries the registry include").contains(DATAMOVES_INCLUDE);
        master = master.replace(DATAMOVES_INCLUDE, "");
        // a changeset that registers a move writes into the registry, so the includes that register one are as unborn as
        // the registry itself in the old state; the new boot applies them. The set is read from the master (every include
        // whose file INSERTs INTO nexus.data_moves), so a new registering include cannot be forgotten here. The owner-read
        // policy follows the registering includes and reads the same unborn state, so it goes with them.
        List<String> stripped = new ArrayList<>(ShippedMoveRegistrations.byInclude().keySet());
        stripped.add("db/changelog/vectors-065-catalog-collections-owner-read.xml");
        // RDR-229 P3.1 (nexus-p2m1x.17): the stamp family builds on the registry and the policy above, so it is as unborn as they
        // are: vectors-068 (the collection_id columns and CHECKs the stamp moves fill and validate), vectors-069 (the dual-write
        // functions) and vectors-070 (the stamp moves and their owner-read policies). vectors-070 and catalog-052 register
        // moves, so the scan above already holds them; they are named again only to make the list say so, and added only when
        // the scan did not find them (a second entry would find its include already stripped). Sam's ruling (ask
        // p31-existing-schema-tests) names 068, 069 and 070; no walk failure has shown 068 or 069 to be needed here (they touch no
        // registry), they are stripped because the old state predates the stamp family as a whole.
        //
        // datamoves-002 (the finisher's verify_statement_timeout_ms column, an ALTER TABLE nexus.data_moves with no precondition) is
        // the one include after datamoves-001 that reads the registry and is not a registering include, so the scan cannot hold it:
        // left in, the old walk fails at datamoves-002-1 with 'relation "nexus.data_moves" does not exist' (chas run of 911747ea29).
        // It landed after this list was written and is independent of P3.1; the new boot applies it after datamoves-001.
        //
        // RDR-229 P3.8 (nexus-p2m1x.28): the collections family is as unborn as the stamp family it builds on. collections-002's
        // sequence-heal function reads catalog_collections.collection_id, which vectors-068 adds; left in, the old walk fails
        // there with 'column c.collection_id does not exist' (chas run of 32fc5c484b). collections-001 and -003 go with it: the
        // old state predates P3.8 as a whole, and the new boot applies all three.
        //
        // RDR-229 P3.5b (nexus-p2m1x.24): the reader redefinitions read the collection_id columns vectors-068 adds, so they are as
        // unborn as it is: left in, the old walk fails at vectors-077-1 (hybrid_search_1024) with 'column c.collection_id does not
        // exist'. A real database from before the registry walks vectors-068 before them; the new boot applies both files.
        for (String unborn : List.of("db/changelog/datamoves-002-verify-statement-timeout.xml",
                "db/changelog/vectors-068-collection-id-expand.xml",
                "db/changelog/vectors-069-collection-id-dual-write-functions.xml",
                "db/changelog/vectors-070-collection-id-stamp-moves.xml",
                "db/changelog/collections-001-tuple-unique-index.xml",
                "db/changelog/collections-002-collection-id-seq-heal-function.xml",
                "db/changelog/collections-003-tuple-lock-stripes.xml",
                "db/changelog/vectors-077-rdr229-p35b-readers-both.xml",
                "db/changelog/vectors-078-rdr229-p35b-readers-continue.xml")) {
            if (!stripped.contains(unborn)) {
                stripped.add(unborn);
            }
        }
        assertThat(stripped).as("non-vacuity: the registry's own follow-up and the P3.1 includes are in the old state's strip list")
            .contains("db/changelog/datamoves-002-verify-statement-timeout.xml",
                "db/changelog/vectors-068-collection-id-expand.xml",
                "db/changelog/vectors-069-collection-id-dual-write-functions.xml",
                "db/changelog/vectors-070-collection-id-stamp-moves.xml",
                "db/changelog/collections-002-collection-id-seq-heal-function.xml",
                "db/changelog/vectors-077-rdr229-p35b-readers-both.xml",
                "db/changelog/vectors-078-rdr229-p35b-readers-continue.xml",
                "db/changelog/catalog-052-residue-moves-supersede.xml");
        for (String registering : stripped) {
            String include = "<include file=\"" + registering + "\"/>";
            assertThat(master).as("the master includes " + registering).contains(include);
            master = master.replace(include, "");
        }
        String grantsInclude = "<include file=\"" + GRANTS_FILE + "\"/>";
        assertThat(master).as("the master includes the service grants file").contains(grantsInclude);
        master = master.replace(grantsInclude, "<include file=\"" + OLD_GRANTS_FILE + "\"/>");
        Files.writeString(tmp.resolve("old-master.xml"), master);

        String grants = read(GRANTS_FILE);
        String exclusion = "          -- nexus.data_moves (RDR-246, datamoves-001-registry.xml) is admin-connection only: "
            + "no nexus_svc grant.\n"
            + "          AND NOT (n.nspname = 'nexus' AND c.relname = 'data_moves')\n";
        assertThat(grants).as("the loop carries the exclusion").contains(exclusion);
        grants = grants.replace(exclusion, "");
        grants = withoutTheRevoke(grants);
        // every validCheckSum entry: the old text carried none (the comment above them is outside the checksum)
        int removed = 0;
        int idAt = grants.indexOf("id=\"" + GRANTS_ID + "\"");
        // only this changeset's: grants-004 below carries one of its own
        for (int from = grants.indexOf("<validCheckSum>9:", idAt);
             from >= 0 && from < grants.indexOf("</changeSet>", idAt); from = grants.indexOf("<validCheckSum>9:", from)) {
            int to = grants.indexOf("</validCheckSum>", from) + "</validCheckSum>".length();
            grants = grants.substring(0, from) + grants.substring(to);
            removed++;
        }
        assertThat(removed).as("the changeset lists two earlier checksums").isEqualTo(2);
        String root = "<databaseChangeLog\n";
        assertThat(grants).contains(root);
        grants = grants.replace(root, "<databaseChangeLog logicalFilePath=\"" + GRANTS_FILE + "\"\n");
        Path out = tmp.resolve(OLD_GRANTS_FILE);
        Files.createDirectories(out.getParent());
        Files.writeString(out, grants);
    }

    private static String read(String resource) throws Exception {
        try (InputStream in = GrantsNexusSvcUpgradeIntegrationTest.class.getClassLoader()
                .getResourceAsStream(resource)) {
            assertThat(in).as(resource).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private void update(String url, String changelog) throws Exception {
        try (Connection conn = DriverManager.getConnection(url, ADMIN_ROLE, ADMIN_PASS)) {
            new Liquibase(changelog,
                new CompositeResourceAccessor(new DirectoryResourceAccessor(tmp), new ClassLoaderResourceAccessor()),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn)))
                .update(new Contexts());
        }
    }

    private static List<String> md5sums(DSLContext ctx, String id) {
        return ctx.select(DSL.field(DSL.name("md5sum"), String.class))
            .from(DSL.table(DSL.name("databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(id))
            .fetch(0, String.class);
    }

    private static List<String> filenames(DSLContext ctx, String id) {
        return ctx.select(DSL.field(DSL.name("filename"), String.class))
            .from(DSL.table(DSL.name("databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(id))
            .fetch(0, String.class);
    }

    private static boolean hasTablePriv(DSLContext ctx, String relation, String priv) {
        var has = DSL.function("has_table_privilege", SQLDataType.BOOLEAN,
            DSL.val(SVC_ROLE), DSL.val(relation), DSL.val(priv));
        return Boolean.TRUE.equals(ctx.select(has).fetchOne(has));
    }
}

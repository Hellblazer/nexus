// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
            // (On a fresh walk the default ACL is installed by the last include, after data_moves, so the REVOKE
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

    /** Writes old-master.xml and the old grants file under {@link #tmp}. */
    private void writeOldChangelog() throws Exception {
        String master = read("db/changelog/db.changelog-master.xml");
        assertThat(master).as("the master carries the registry include").contains(DATAMOVES_INCLUDE);
        master = master.replace(DATAMOVES_INCLUDE, "");
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
        int from = grants.indexOf("<validCheckSum>9:", grants.indexOf("id=\"" + GRANTS_ID + "\""));
        int to = grants.indexOf("</validCheckSum>", from) + "</validCheckSum>".length();
        grants = grants.substring(0, from) + grants.substring(to);
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

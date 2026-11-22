// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.DmlUtilsTestFixtures;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24): the form A changesets of vectors-078 are skipped on a fresh walk by design (each waits, behind a
 * CONTINUE precondition, for every {@code rdr229-stamp-*} move to complete), and the Windows engine smoke allows them pending
 * (FRESH_BOOT_PENDING). This chains the real pieces so that allowance cannot hide a changeset that never lands (linda, from
 * colophon's collections-001-2 guard, b577a641cd): boot 1 is a real migration walk that registers the stamp moves through their
 * shipped changesets (the labels the preconditions read, never rows written here) and leaves all twelve unrecorded; the production
 * worker then completes every move ({@link BootedSchema#migrateAndBoot}, which asserts none is open); boot 2, the next real walk,
 * records each of the twelve exactly once; boot 3 records nothing new and leaves nothing pending.
 *
 * <p>The twelve ids come from {@code db/rdr229/p35b-continue-changesets.txt}, the list P3.7's rename and the restart check read.
 */
class Rdr229P35bContinueChangesetsApplyIntegrationTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String CONTINUE_LIST = "db/rdr229/p35b-continue-changesets.txt";
    private static final Table<?> JOURNAL = DSL.table(DSL.name("public", "databasechangelog"));
    private static final Field<String> ID = DSL.field(DSL.name("id"), String.class);

    @Test
    void aFreshInstall_skipsTheTwelve_theWorkerCompletesTheStamps_andTheNextBootAppliesEachOnce_soNothingStaysPending()
        throws Exception {
        List<String> twelve = continueList();
        assertThat(twelve).as("non-vacuity: the shipped list names the vectors-078 changesets").hasSize(12)
            .allMatch(i -> i.startsWith("vectors-078-"));
        PostgreSQLContainer<?> pg = BootedSchema.startContainer();
        try (Connection su = pg.createConnection("")) {
            // boot 1 (the walk) and the worker's drive to no open move; the walk alone runs before the drive, so nothing has
            // applied the twelve yet
            BootedSchema.migrateAndBoot(su);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(BootedSchema.openLabels(ctx)).as("the worker completed every move").isEmpty();
            for (String id : twelve) {
                assertThat(ctx.fetchCount(JOURNAL, ID.eq(id))).as("boot 1 leaves %s pending: a stamp was incomplete when it walked", id)
                    .isZero();
            }

            try (HikariDataSource admin = adminPool(su.getMetaData().getURL())) {
                var installer = new DmlUtilsInstaller(true);
                SchemaMigrator.migrate(admin, installer);  // boot 2: every stamp move is complete, so the gate passes
                for (String id : twelve) {
                    assertThat(ctx.fetchCount(JOURNAL, ID.eq(id)))
                        .as("boot 2 records %s exactly once: the deferred changeset does not stay pending", id).isEqualTo(1);
                }
                assertThat(pendingIds(admin)).as("after boot 2 no changeset of the master changelog is pending").isEmpty();

                int recorded = ctx.fetchCount(JOURNAL);
                SchemaMigrator.migrate(admin, installer);  // boot 3: nothing left to apply
                assertThat(ctx.fetchCount(JOURNAL)).as("boot 3 records nothing new").isEqualTo(recorded);
                assertThat(pendingIds(admin)).as("and nothing is pending").isEmpty();
            }
        } finally {
            pg.stop();
        }
    }

    private static HikariDataSource adminPool(String url) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(DmlUtilsTestFixtures.ADMIN);
        cfg.setPassword(DmlUtilsTestFixtures.ADMIN_PASSWORD);
        cfg.setMaximumPoolSize(1);
        return new HikariDataSource(cfg);
    }

    /** The master changelog's changesets that a walk would still run, runAlways ones left out (they are always "unrun"). */
    private static List<String> pendingIds(HikariDataSource admin) throws Exception {
        List<String> out = new ArrayList<>();
        try (Connection conn = admin.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                for (ChangeSet cs : liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression())) {
                    if (!cs.isAlwaysRun()) {
                        out.add(cs.getId());
                    }
                }
            }
        }
        return out;
    }

    private static List<String> continueList() throws Exception {
        List<String> out = new ArrayList<>();
        try (InputStream in = Rdr229P35bContinueChangesetsApplyIntegrationTest.class.getClassLoader().getResourceAsStream(CONTINUE_LIST)) {
            assertThat(in).as("%s is on the classpath", CONTINUE_LIST).isNotNull();
            var reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (!line.isBlank() && !line.startsWith("#")) {
                    out.add(line.strip());
                }
            }
        }
        return out;
    }
}

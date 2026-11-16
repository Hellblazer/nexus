// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Properties;

/**
 * nexus-f3yqd.2.11: no database. The vendored path is frozen: after the first apply, every library changeset's
 * {@code filename} in {@code liquibase.<library>_databasechangelog} carries the resource path it was applied from
 * ({@code db/vendor/<library>/changes/...}), and Liquibase matches an applied changeset by id, author AND filename. A
 * rename of the directory would read as all-new changesets on every install, and the first one would fail on an
 * object that already exists, so the prefix is pinned here as literals, not derived from the code that uses it.
 */
class DmlUtilsInstallerTest {

    @Test
    void theVendoredPathPrefix_isFrozen() {
        assertThat(DmlUtilsInstaller.LIBRARIES).extracting(DmlUtilsInstaller.Library::master)
            .containsExactly("db/vendor/ddl_utils/db.changelog-master.xml", "db/vendor/dml_utils/db.changelog-master.xml");
        assertThat(DmlUtilsInstaller.LIBRARIES).extracting(DmlUtilsInstaller.Library::historyTable)
            .containsExactly("ddl_utils_databasechangelog", "dml_utils_databasechangelog");
        assertThat(DmlUtilsInstaller.LIBRARIES).extracting(DmlUtilsInstaller.Library::lockTable)
            .containsExactly("ddl_utils_databasechangeloglock", "dml_utils_databasechangeloglock");
    }

    @Test
    void everyMaster_isOnTheClasspath_andIncludesItsChangesFileRelativeToItself() throws IOException {
        for (DmlUtilsInstaller.Library library : DmlUtilsInstaller.LIBRARIES) {
            String master = read(library.master());
            assertThat(master).as("%s include", library.master())
                .contains("<include file=\"changes/changes.xml\" relativeToChangelogFile=\"true\"/>");
            assertThat(read("db/vendor/" + library.name() + "/changes/changes.xml"))
                .as("%s changes file", library.name()).contains("<changeSet ");
        }
    }

    @Test
    void theApplyOrder_isTheOrderOfThePinsFile() throws IOException {
        Properties pins = new Properties();
        try (InputStream in = getClass().getClassLoader().getResourceAsStream("db/vendor/pins.properties")) {
            assertThat(in).as("db/vendor/pins.properties").isNotNull();
            pins.load(in);
        }
        List<String> pinned = pins.stringPropertyNames().stream().sorted().toList();
        assertThat(DmlUtilsInstaller.LIBRARIES).extracting(DmlUtilsInstaller.Library::name)
            .as("every vendored library is applied, and no other").containsExactlyInAnyOrderElementsOf(pinned)
            .as("ddl_utils before dml_utils").containsExactly("ddl_utils", "dml_utils");
    }

    @Test
    void aStatusThatIsNotApplied_hasAReasonForTheWorker() {
        var applied = new DmlUtilsInstaller.Status(DmlUtilsInstaller.State.APPLIED, null, "nexus_admin");
        var skipped = new DmlUtilsInstaller.Status(DmlUtilsInstaller.State.SKIPPED, "pg_background is not installed", null);
        assertThat(applied.blockedReason()).isEmpty();
        assertThat(skipped.blockedReason()).contains("pg_background is not installed");
        assertThat(new DmlUtilsInstaller(true).status().blockedReason()).isPresent();
    }

    /** The rule Main applies (nexus-f3yqd.2.11): libraries only when the admin login is not the service role. */
    @Test
    void forEngine_installsOnlyWithASeparateAdminRole() {
        assertThat(DmlUtilsInstaller.forEngine("nexus_admin", "nexus_svc").hasSeparateAdminRole())
            .as("NX_DB_ADMIN_USER differs from NX_DB_USER").isTrue();
        assertThat(DmlUtilsInstaller.forEngine("nexus_svc", "nexus_svc").hasSeparateAdminRole())
            .as("the admin connection is the service role (NX_DB_ADMIN_* unset resolves to NX_DB_*)").isFalse();
        assertThat(DmlUtilsInstaller.forEngine("a", "A").hasSeparateAdminRole())
            .as("role names are case sensitive").isTrue();
        assertThat(new DmlUtilsInstaller(true).hasSeparateAdminRole()).isTrue();
        assertThat(new DmlUtilsInstaller(false).hasSeparateAdminRole()).isFalse();
    }

    @Test
    void aReason_isOneBoundedLine() {
        assertThat(DmlUtilsInstaller.bounded("line one\n  line two\t" + "x".repeat(600))).doesNotContain("\n").hasSizeLessThan(520)
            .endsWith("...");
    }

    private String read(String resource) throws IOException {
        try (InputStream in = getClass().getClassLoader().getResourceAsStream(resource)) {
            assertThat(in).as(resource).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}

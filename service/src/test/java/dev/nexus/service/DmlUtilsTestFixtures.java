// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.sql.Connection;
import java.util.Map;

/**
 * nexus-f3yqd.2.11: the superuser-side states a data-move install scenario starts from, on a container of
 * {@link PgContainerHelper#newDataMoveContainer()}. Each is a test changelog (nexus-zrcj7: no SQL in Java, tests
 * included) run through {@link PgContainerHelper#runSuperuserTestChangelog}; none takes SQL text.
 */
final class DmlUtilsTestFixtures {

    private DmlUtilsTestFixtures() { /* static utility */ }

    /** The migration role the changelogs name (a fixed name: a placeholder would need RawSqlGateTest's lists). */
    static final String ADMIN = "nexus_admin_dmlutils";

    /**
     * The three library roles, their grants and the {@code liquibase} schema owned by {@code dml_utils_owner}, as
     * {@code nexus.db.pg_bootstrap.dml_utils} makes them, with {@link #ADMIN} as the engine's migration role, granted
     * {@code dml_utils_owner} with SET. After {@link PgContainerHelper#createDataMoveExtensions}. Idempotent, and it
     * restores what {@link #revokeDmlUtilsOwnerDatabaseCreate} and {@link #revokeSetRole} took.
     */
    static void bootstrapDmlUtils(Connection su) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-dml-utils-roles.xml",
            "databasechangelog_test_dml_utils_roles", Map.of());
    }

    /** {@link #ADMIN}'s membership in {@code dml_utils_owner} loses SET: {@code SET ROLE dml_utils_owner} is refused. */
    static void revokeSetRole(Connection su) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-dml-utils-revoke-set-role.xml",
            "databasechangelog_test_dml_utils_revoke_set", Map.of());
    }

    /** Drops the library schemas and the {@code liquibase} schema; roles, grants and the extension stay. */
    static void uninstallDmlUtils(Connection su) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-dml-utils-uninstall.xml",
            "databasechangelog_test_dml_utils_uninstall", Map.of());
    }

    /** {@code REVOKE CREATE ON DATABASE FROM dml_utils_owner}: the first library changeset then fails. */
    static void revokeDmlUtilsOwnerDatabaseCreate(Connection su) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su,
            "db/changelog-test/db.changelog-test-dml-utils-revoke-database-create.xml",
            "databasechangelog_test_dml_utils_revoke_create", Map.of());
    }

    /** {@code DROP EXTENSION pg_background}. */
    static void dropPgBackground(Connection su) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-drop-pg-background.xml",
            "databasechangelog_test_drop_pg_background", Map.of());
    }
}

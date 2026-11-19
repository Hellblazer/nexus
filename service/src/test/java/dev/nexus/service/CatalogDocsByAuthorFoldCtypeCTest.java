// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The author fold matrix on a cluster initdb'd as the local bundle is: initdb --no-locale -E UTF8, so datctype is C (the
 * bundle is built --without-icu; see src/nexus/db/pg_provision.py). The shared per-fork test cluster has an en_US.utf8
 * locale and cannot stand in for it. See {@link CatalogDocsByAuthorFoldBase}.
 */
class CatalogDocsByAuthorFoldCtypeCTest extends CatalogDocsByAuthorFoldBase {

    private static final String SVC_ROLE = "svc_rdr241_foldc_test";
    private static final String SVC_PASS = "svc_rdr241_foldc_test_pass";

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    @Override
    PostgreSQLContainer<?> startPg() {
        PostgreSQLContainer<?> c = PgContainerHelper.newContainer();
        c.withEnv("POSTGRES_INITDB_ARGS", "--no-locale -E UTF8");
        c.start();
        return c;
    }

    @Test
    void theClusterIsCtypeC_soTheMatrixIsNotRunningOnAUnicodeLocale() throws Exception {
        assertThat(databaseCtype()).isEqualTo("C");
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The by-scheme matrix on a cluster initdb'd as the local bundle is (--no-locale -E UTF8, datctype C). See
 * {@link CatalogDocsBySchemeBase}.
 */
class CatalogDocsBySchemeCtypeCTest extends CatalogDocsBySchemeBase {

    private static final String SVC_ROLE = "svc_rdr241_schemec_test";
    private static final String SVC_PASS = "svc_rdr241_schemec_test_pass";

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
    void theClusterIsCtypeC() throws Exception {
        assertThat(databaseCtype()).isEqualTo("C");
    }
}

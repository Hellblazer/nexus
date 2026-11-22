// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The author fold matrix on the shared test cluster, whose database has a Unicode locale (not C): the same answers as on
 * the C-ctype cluster (CatalogDocsByAuthorFoldCtypeCTest). See {@link CatalogDocsByAuthorFoldBase}.
 */
class CatalogDocsByAuthorFoldDefaultCtypeTest extends CatalogDocsByAuthorFoldBase {

    private static final String SVC_ROLE = "svc_rdr241_foldd_test";
    private static final String SVC_PASS = "svc_rdr241_foldd_test_pass";

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    /**
     * RDR-229 P3.1: a document names COLL by registry id, so COLL is registered for both tenants. It runs after the
     * base's startup (which burns each tenant's ghost sweep, and that sweep removes a collection nothing references).
     */
    @org.junit.jupiter.api.BeforeAll
    void registerRouteCollection() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, COLL);
            PgContainerHelper.insertCollection(dsl, TENANT_B, COLL);
        }
    }

    @Test
    void theClusterIsNotCtypeC_soTheTwoMatricesRunOnDifferentLocales() throws Exception {
        assertThat(databaseCtype()).isNotIn("C", "POSIX");
    }
}

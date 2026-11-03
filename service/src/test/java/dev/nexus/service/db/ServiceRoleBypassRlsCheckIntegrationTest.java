// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-b1adx: the check against real roles on a real, fully migrated server. The BYPASSRLS role is made through
 * {@link PgContainerHelper#createLoginRole} (a test changelog, no SQL text). Each case connects AS the role, because
 * the check reads {@code current_user}.
 *
 * <p>Positive controls inside the test: {@code nexus_svc} (NOSUPERUSER NOBYPASSRLS, the shape provisioning makes) and
 * the container's superuser both pass, so the refusal of the BYPASSRLS role is not a check that refuses every role;
 * and the facts read off the BYPASSRLS role are asserted, so a role that was not in fact BYPASSRLS cannot make the
 * refusal case pass by accident.
 */
class ServiceRoleBypassRlsCheckIntegrationTest {

    private static final String BYPASS_ROLE = "svc_bypassrls_b1adx";
    private static final String BYPASS_PASS = "svc_bypassrls_b1adx_pw";

    private static HikariDataSource connectAs(PostgreSQLContainer<?> pg, String user, String pass) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @Test
    void aBypassRlsServiceRoleIsRefused_theProvisionedRoleAndASuperuserPass() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.createLoginRole(su, BYPASS_ROLE, BYPASS_PASS, true);
            }

            try (var ds = connectAs(pg, BYPASS_ROLE, BYPASS_PASS)) {
                var facts = ServiceRoleBypassRlsCheck.read(ds);
                assertThat(facts).as("the role the test made is BYPASSRLS and not a superuser")
                    .isEqualTo(new ServiceRoleBypassRlsCheck.RoleFacts(BYPASS_ROLE, false, true));
                assertThatThrownBy(() -> ServiceRoleBypassRlsCheck.verifyAtStartup(ds))
                    .isInstanceOf(ServiceRoleBypassRlsCheck.BypassRlsException.class)
                    .hasMessageContaining(BYPASS_ROLE)
                    .hasMessageContaining("NOBYPASSRLS");
            }

            try (var ds = connectAs(pg, PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD)) {
                assertThat(ServiceRoleBypassRlsCheck.verifyAtStartup(ds))
                    .as("nexus_svc as provisioned: NOSUPERUSER NOBYPASSRLS")
                    .isEqualTo(new ServiceRoleBypassRlsCheck.RoleFacts(PgContainerHelper.SVC_USERNAME, false, false));
            }

            try (var ds = PgContainerHelper.superuserDataSource(pg)) {
                var facts = ServiceRoleBypassRlsCheck.verifyAtStartup(ds);
                assertThat(facts.superuser()).as("the container's role is a superuser").isTrue();
            }
        }
    }
}

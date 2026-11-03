// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-b1adx: the decision against any set of role attributes, with no database.
 * {@code ServiceRoleBypassRlsCheckIntegrationTest} reads real roles off a real server.
 */
class ServiceRoleBypassRlsCheckTest {

    private static ServiceRoleBypassRlsCheck.RoleFacts facts(String role, boolean superuser, boolean bypass) {
        return new ServiceRoleBypassRlsCheck.RoleFacts(role, superuser, bypass);
    }

    @Test
    void aBypassRlsNonSuperuserIsRefused_namingTheRoleAndTheFix() {
        String msg = ServiceRoleBypassRlsCheck.refusal(facts("svc_x", false, true)).orElseThrow();
        assertThat(msg).contains("svc_x").contains("BYPASSRLS")
            .contains("NOBYPASSRLS").contains("restart")
            .contains("vectors-039-1").contains("permission denied");
    }

    @Test
    void aRoleRlsBindsPasses_andSoDoesASuperuser() {
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("nexus_svc", false, false))).isEmpty();
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("postgres", true, true)))
            .as("a superuser needs no grant; single-role development setups boot as they did").isEmpty();
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("root", true, false))).isEmpty();
    }

    @Test
    void verifyThrowsOnARefusal_returnsTheFactsOtherwise() {
        assertThatThrownBy(() -> ServiceRoleBypassRlsCheck.verify(() -> facts("svc_x", false, true)))
            .isInstanceOf(ServiceRoleBypassRlsCheck.BypassRlsException.class)
            .hasMessageContaining("NOBYPASSRLS");
        assertThat(ServiceRoleBypassRlsCheck.verify(() -> facts("nexus_svc", false, false)).role())
            .isEqualTo("nexus_svc");
    }

    @Test
    void attributesThatCannotBeReadAreARefusal_notAPass() {
        assertThatThrownBy(() -> ServiceRoleBypassRlsCheck.verify(() -> { throw new SQLException("pool is down"); }))
            .isInstanceOf(ServiceRoleBypassRlsCheck.BypassRlsException.class)
            .hasMessageContaining("pool is down").hasMessageContaining("pg_roles");
    }
}

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
            .contains("vectors-039-1").contains("permission denied")
            // The message ships to operators who have no nexus checkout: a link, not a repo path (nexus-z7ddd).
            .contains("https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md#storage-service-postgres-prerequisites")
            .doesNotContain("See docs/");
    }

    @Test
    void aRoleRlsBindsPasses_andSoDoesASuperuser() {
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("nexus_svc", false, false))).isEmpty();
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("postgres", true, true)))
            .as("a superuser needs no grant; single-role development setups boot as they did").isEmpty();
        assertThat(ServiceRoleBypassRlsCheck.refusal(facts("root", true, false))).isEmpty();
    }

    @Test
    void aSuperuserPassesButWithAWarning_namingTheRoleAndThatIsolationIsNotEnforced() {
        String warn = ServiceRoleBypassRlsCheck.superuserWarning(facts("postgres", true, true)).orElseThrow();
        assertThat(warn).contains("postgres").contains("superuser").contains("tenant isolation is not enforced");
        assertThat(ServiceRoleBypassRlsCheck.superuserWarning(facts("root", true, false)))
            .as("RLS does not bind a superuser whether or not rolbypassrls is set").isPresent();
        // positive control: a role RLS binds, and a BYPASSRLS non-superuser (refused, not warned), draw no warning
        assertThat(ServiceRoleBypassRlsCheck.superuserWarning(facts("nexus_svc", false, false))).isEmpty();
        assertThat(ServiceRoleBypassRlsCheck.superuserWarning(facts("svc_x", false, true))).isEmpty();
    }

    /** The WARN lines {@link ServiceRoleBypassRlsCheck#verify} logs for {@code f}. */
    private static java.util.List<String> warnsFromVerify(ServiceRoleBypassRlsCheck.RoleFacts f) {
        var logger = (ch.qos.logback.classic.Logger)
            org.slf4j.LoggerFactory.getLogger(ServiceRoleBypassRlsCheck.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            ServiceRoleBypassRlsCheck.verify(() -> f);
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }
        return logs.list.stream()
            .filter(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN)
            .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
            .toList();
    }

    @Test
    void verifyLogsTheSuperuserWarning_andNothingForARoleRlsBinds() {
        // nexus-22out S-1: the text builder is tested above; this pins that verify actually emits it.
        assertThat(warnsFromVerify(facts("postgres", true, true)))
            .singleElement().asString()
            .contains("event=service_role_superuser_isolation_not_enforced").contains("role=postgres")
            .contains("tenant isolation is not enforced");
        assertThat(warnsFromVerify(facts("nexus_svc", false, false))).as("no warning for a role RLS binds").isEmpty();
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

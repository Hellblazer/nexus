// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-soqa8: pure-unit coverage of {@link FailFastPostgreSQLContainer}'s
 * retryable-vs-deterministic classifier, without booting any container (Docker not
 * required). Exercising the real squatter scenario needs a live foreign server on the
 * container's published port, which is exactly the nexus-lgdy1 mechanism and is not
 * reproduced here — this pins the classification logic that mechanism depends on.
 */
final class FailFastPostgreSQLContainerTest {

    @Test
    void invalidAuthorizationSpecificationIsDeterministic() {
        // "FATAL: role "postgres" does not exist" -- the exact nexus-lgdy1 signature.
        var e = new SQLException("FATAL: role \"postgres\" does not exist", "28000");

        assertThat(FailFastPostgreSQLContainer.deterministicFailureReason(e))
            .isNotNull()
            .contains("28000");
    }

    @Test
    void invalidPasswordIsDeterministic() {
        var e = new SQLException("FATAL: password authentication failed for user \"postgres\"", "28P01");

        assertThat(FailFastPostgreSQLContainer.deterministicFailureReason(e))
            .isNotNull()
            .contains("28P01");
    }

    @Test
    void connectionRefusedIsNotDeterministic() {
        // Container still starting up / not yet listening -- must keep retrying.
        var e = new SQLException("Connection to localhost:54321 refused", "08001");

        assertThat(FailFastPostgreSQLContainer.deterministicFailureReason(e)).isNull();
    }

    @Test
    void databaseStartingUpIsNotDeterministic() {
        // PostgreSQL 57P03 -- "the database system is starting up" -- transient by definition.
        var e = new SQLException("FATAL: the database system is starting up", "57P03");

        assertThat(FailFastPostgreSQLContainer.deterministicFailureReason(e)).isNull();
    }

    @Test
    void nullSqlStateIsNotDeterministic() {
        // e.g. a raw SSL-handshake / socket-level failure with no SQLSTATE assigned yet.
        var e = new SQLException("Connection reset");

        assertThat(FailFastPostgreSQLContainer.deterministicFailureReason(e)).isNull();
    }

    @Test
    void diagnosisMessageNamesTheLgdy1ClassAndInterpolatesThePort() {
        String message = FailFastPostgreSQLContainer.diagnosisMessage(
            "invalid_authorization_specification (28000, e.g. role does not exist)", 54321);

        assertThat(message)
            .contains("nexus-lgdy1")
            .contains("squatting the published port")
            .contains("lsof -iTCP:54321 -sTCP:LISTEN");
    }

    /**
     * WIRING (substantive-critic significant #1, T2 {@code nexus/nexus-soqa8-critique-
     * 2026-08-06} [21531]): pins that {@link PgContainerHelper#newContainer()} actually
     * constructs a {@link FailFastPostgreSQLContainer}, not just a bare
     * {@code PostgreSQLContainer} — the classifier tests above would all stay green through
     * a revert of that one-line construction swap. {@code GenericContainer}'s constructor
     * and the {@code with*} builder calls {@code newContainer()} chains are pure
     * object/config setup (confirmed against the testcontainers 1.21.4 sources: no Docker
     * client is touched before {@code start()}), so this assertion needs no running Docker
     * daemon.
     */
    @Test
    void pgContainerHelperNewContainerReturnsFailFastPostgreSQLContainer() {
        assertThat(PgContainerHelper.newContainer()).isInstanceOf(FailFastPostgreSQLContainer.class);
    }
}

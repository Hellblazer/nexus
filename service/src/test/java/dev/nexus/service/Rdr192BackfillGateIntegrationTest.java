// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.Rdr192BackfillGate.BackfillIncompleteException;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-192 (bead nexus-wbfpw.41) — the reaper's precondition.
 *
 * <p>{@code reapable(c)} treats a manifest-less chunk as garbage. A legacy
 * note (stored before nexus-b6enc, so it has a catalog document but never got
 * a manifest row) is manifest-less, so the reaper (nexus-2x9xa) must not run
 * on a tenant whose legacy-unmanifested backfill has not completed. The
 * client's upgrade-ladder rung {@code rdr192-manifest-backfill} records that
 * completion in {@code nexus.ladder_completions} only after a fresh census
 * reads zero; {@link Rdr192BackfillGate} is the read of that fact the reaper
 * calls. Real Postgres with RLS, through the service role.
 *
 * <p>Fails CLOSED: no record, another rung's record, another tenant's record,
 * and an unreadable ledger all refuse.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr192BackfillGateIntegrationTest {

    private static final String SVC_ROLE = "svc_rdr192_gate_test";
    private static final String SVC_PASS = "svc_rdr192_gate_test_pass";
    private static final String TENANT_A = "rdr192-gate-a";
    private static final String TENANT_B = "rdr192-gate-b";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    LadderRepository ladder;
    Rdr192BackfillGate gate;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        svcDs = pool(SVC_ROLE, SVC_PASS);
        ladder = new LadderRepository(new TenantScope(svcDs));
        gate = new Rdr192BackfillGate(ladder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private com.zaxxer.hikari.HikariDataSource pool(String user, String pass) {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @Test
    void rungNameIsTheLiteralTheClientRecords() {
        // Pinned from the Python side too (tests/upgrade/
        // test_rdr192_manifest_backfill_rung.py reads this source line).
        assertThat(Rdr192BackfillGate.RUNG_NAME).isEqualTo("rdr192-manifest-backfill");
    }

    @Test
    void refusesWhenNothingHasBeenRecorded() {
        String tenant = "rdr192-gate-empty";
        assertThat(gate.isComplete(tenant)).isFalse();
        assertThatThrownBy(() -> gate.requireComplete(tenant))
                .isInstanceOf(BackfillIncompleteException.class)
                .hasMessageContaining(Rdr192BackfillGate.RUNG_NAME)
                .hasMessageContaining(tenant);
    }

    @Test
    void permitsOnceTheRungIsRecorded() {
        ladder.record(TENANT_A, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        assertThat(gate.isComplete(TENANT_A)).isTrue();
        gate.requireComplete(TENANT_A); // does not throw
    }

    @Test
    void anotherRungsRecordDoesNotOpenTheGate() {
        String tenant = "rdr192-gate-other-rung";
        ladder.record(tenant, "some-other-rung", "7.99.0", "");
        assertThat(gate.isComplete(tenant)).isFalse();
    }

    @Test
    void completionIsPerTenant() {
        ladder.record(TENANT_A, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        assertThat(gate.isComplete(TENANT_A)).isTrue();
        assertThat(gate.isComplete(TENANT_B))
                .as("tenant B never recorded the rung; RLS and the tenant predicate keep A's fact out")
                .isFalse();
        assertThatThrownBy(() -> gate.requireComplete(TENANT_B))
                .isInstanceOf(BackfillIncompleteException.class);
    }

    @Test
    void anUnreadableLedgerFailsClosed() {
        var dead = pool(SVC_ROLE, SVC_PASS);
        var deadGate = new Rdr192BackfillGate(new LadderRepository(new TenantScope(dead)));
        // Control: the gate over a live pool with a recorded fact is open.
        ladder.record("rdr192-gate-dead", Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        assertThat(gate.isComplete("rdr192-gate-dead")).isTrue();

        dead.close();
        assertThat(deadGate.isComplete("rdr192-gate-dead")).isFalse();
        assertThatThrownBy(() -> deadGate.requireComplete("rdr192-gate-dead"))
                .isInstanceOf(BackfillIncompleteException.class)
                .hasMessageContaining("cannot read");
    }
}

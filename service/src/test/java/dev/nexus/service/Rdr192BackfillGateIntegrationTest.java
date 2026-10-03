// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.Rdr192BackfillGate.BackfillIncompleteException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.Map;

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
    ReaperRepository reaperStore;
    /** The production-shaped gate: the same empty-tenant test NexusService wires (nexus-wbfpw.73). */
    Rdr192BackfillGate exemptingGate;

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
        reaperStore = new ReaperRepository(new TenantScope(svcDs));
        exemptingGate = new Rdr192BackfillGate(ladder, ChunkReaper.emptyTenantProbe(reaperStore));
    }

    /** One chunk with no manifest row, seeded as the superuser. */
    private void seedChunk(String tenant, String collection, String seed) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection,
                List.of(dev.nexus.service.db.Chash.ofText(collection + "/" + seed).toHex()),
                List.of(seed + " text"), List.of(new float[384]), List.of(Map.<String, Object>of()));
        }
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

    // ── nexus-wbfpw.73: an empty tenant passes without a completion record ────

    private static final Duration BOUND = Duration.ofSeconds(25);

    @Test
    void anEmptyTenantWithNoRecordPasses_andNothingIsWritten() {
        String tenant = "rdr192-gate-hollow";
        assertThat(ladder.completions(tenant)).isEmpty();
        assertThat(reaperStore.holdsNothing(tenant, BOUND)).isTrue();

        exemptingGate.requireComplete(tenant); // does not throw

        assertThat(ladder.completions(tenant))
                .as("the gate reads; the client rung stays the one recorder of a completion")
                .isEmpty();
        assertThat(gate.isComplete(tenant)).as("isComplete still says what the ledger says").isFalse();
    }

    @Test
    void theGateWithNoEmptinessTestNeverExempts() {
        assertThatThrownBy(() -> gate.requireComplete("rdr192-gate-hollow-strict"))
                .isInstanceOf(BackfillIncompleteException.class);
    }

    @Test
    void aTenantHoldingAChunkWithNoRecordIsStillRefused() throws Exception {
        String tenant = "rdr192-gate-holds-chunk";
        seedChunk(tenant, "knowledge__" + tenant + "__minilm-l6-v2-384__v1", "a");
        assertThat(reaperStore.holdsNothing(tenant, BOUND)).isFalse();

        assertThatThrownBy(() -> exemptingGate.requireComplete(tenant))
                .isInstanceOf(BackfillIncompleteException.class)
                .hasMessageContaining(Rdr192BackfillGate.RUNG_NAME);
    }

    @Test
    void aRegisteredCollectionWithNoChunksDoesNotMakeATenantNonEmpty() throws Exception {
        // The client's listing is "every collection that physically holds chunks": a catalog row alone is not a chunk.
        String tenant = "rdr192-gate-registered-only";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant,
                    "knowledge__" + tenant + "__minilm-l6-v2-384__v1");
        }
        assertThat(reaperStore.holdsNothing(tenant, BOUND)).isTrue();
        exemptingGate.requireComplete(tenant);
    }

    @Test
    void aTenantHoldingOnlyQuarantineChunksIsNotEmpty() throws Exception {
        // It holds chunks, and the expiry a pass would run on them is an irreversible delete. The client rung's
        // empty branch does not apply to it either: its listing is non-empty, so it goes through the census.
        String tenant = "rdr192-gate-quarantine-only";
        seedChunk(tenant, "quarantine-knowledge__" + tenant + "__minilm-l6-v2-384__v1", "q");
        assertThat(reaperStore.holdsNothing(tenant, BOUND)).isFalse();

        assertThatThrownBy(() -> exemptingGate.requireComplete(tenant))
                .isInstanceOf(BackfillIncompleteException.class);
    }

    @Test
    void anotherTenantsChunksDoNotMakeATenantNonEmpty() throws Exception {
        seedChunk("rdr192-gate-neighbour", "knowledge__rdr192-gate-neighbour__minilm-l6-v2-384__v1", "n");
        assertThat(reaperStore.holdsNothing("rdr192-gate-neighbour", BOUND)).isFalse();
        assertThat(reaperStore.holdsNothing("rdr192-gate-alone", BOUND))
                .as("RLS scopes the read to the tenant it is asked about")
                .isTrue();
        exemptingGate.requireComplete("rdr192-gate-alone");
    }

    @Test
    void aRecordedTenantPassesWhetherOrNotItIsEmpty() throws Exception {
        String tenant = "rdr192-gate-recorded-full";
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        seedChunk(tenant, "knowledge__" + tenant + "__minilm-l6-v2-384__v1", "r");
        exemptingGate.requireComplete(tenant);
    }

    @Test
    void anUnreadableEmptinessTestFailsClosed() {
        var broken = new Rdr192BackfillGate(ladder, t -> {
            throw new IllegalStateException("simulated: permission denied for table chunks");
        });
        assertThatThrownBy(() -> broken.requireComplete("rdr192-gate-hollow-unreadable"))
                .isInstanceOf(BackfillIncompleteException.class)
                .hasMessageContaining("cannot tell whether the tenant is empty");
    }
}

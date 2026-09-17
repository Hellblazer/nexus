// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.5) — the per-template {@code
 * max_live_rows} refusal (Scale and Limits item 2, "a runaway writer").
 *
 * <p>Registers a test-only template, {@code probe-cap/<room>}, the same way
 * {@code TupleRepositoryTest}'s {@code probe/<id>} does: a second registry
 * source layered on the bundled resources via {@code
 * TemplateRegistry#loadAtBoot}'s {@code NX_TUPLE_TEMPLATE_DIR}-equivalent
 * directory argument, so production template files stay untouched. {@code
 * id_from: keys} lets each test choose whether a given {@code out} is a
 * genuinely new identity (a fresh {@code id} key value) or a refire of an
 * existing one (the same value) without needing a nonce.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleMaxLiveRowsTest {

    private static final String SVC_ROLE = "svc_tuple_maxrows_test";
    private static final String SVC_PASS = "svc_tuple_maxrows_test_pass";
    private static final long CAP = 2;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    TupleRepository repo;

    @BeforeAll
    void startAll(@TempDir Path extraTemplateDir) throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        Files.writeString(extraTemplateDir.resolve("probe-cap.yaml"), """
                name: probe-cap/<room>
                keys:
                  - id
                id_from: keys
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: 3600
                max_live_rows: %d
                """.formatted(CAP), StandardCharsets.UTF_8);

        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null,
                NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private String freshTenant(String label) {
        return "tuple-maxrows-" + label + "-" + UUID.randomUUID();
    }

    private byte[] outCap(String tenant, String room, String idKey, Long ttlSecondsOrNull) {
        return repo.out(tenant, "probe-cap/" + room, Map.of("id", idKey), Map.of(), null, null,
                ttlSecondsOrNull);
    }

    // ── the refusal itself ───────────────────────────────────────────────────

    @Test
    void outPastMaxLiveRows_refused_typedError_noRowWritten() {
        String tenant = freshTenant("refuse");
        String room = "room-refuse";
        outCap(tenant, room, "r1", null);
        outCap(tenant, room, "r2", null);

        assertThatExceptionOfType(MaxLiveRowsExceededException.class)
                .isThrownBy(() -> outCap(tenant, room, "r3", null))
                .satisfies(e -> {
                    assertThat(e.subspace()).isEqualTo("probe-cap/" + room);
                    assertThat(e.maxLiveRows()).isEqualTo(CAP);
                    assertThat(e.code()).isEqualTo("MaxLiveRowsExceeded");
                    assertThat(e.httpStatus()).isEqualTo(429);
                });

        var rows = repo.rdp(tenant, "probe-cap/" + room, null, 10, null);
        assertThat(rows).as("the refused out wrote nothing").hasSize(2);
    }

    // ── consumed and expired rows do not count ──────────────────────────────

    /**
     * RDR-211 Scale and Limits item 4 ("acked tasks"): an acked row keeps its
     * row (body cleared) until retention purges it, so it is NOT live by the
     * {@code consumed_at IS NULL} half of the predicate. Filling the cap, then
     * acking one, must free a slot for a genuinely new row.
     */
    @Test
    void ackedRowDoesNotCountAgainstTheCap_outSucceedsAfterAck() {
        String tenant = freshTenant("acked");
        String room = "room-acked";
        outCap(tenant, room, "a1", null);
        outCap(tenant, room, "a2", null);
        assertThatExceptionOfType(MaxLiveRowsExceededException.class)
                .isThrownBy(() -> outCap(tenant, room, "a3", null));

        var claimed = repo.inp(tenant, "probe-cap/" + room, Map.of("id", "a1"), "worker-1", 60);
        assertThat(claimed).isPresent();
        repo.ack(tenant, claimed.get().claimId(), "worker-1");

        // a1 is consumed (not live); only a2 is live. A new identity now fits under the cap.
        byte[] id = outCap(tenant, room, "a3", null);
        assertThat(id).isNotNull();
    }

    /**
     * RDR-211 Scale and Limits item 2's "live" predicate excludes an expired
     * row (bug: {@code consumed_at IS NULL AND expires_at > now()} — the same
     * one {@code TupleRepository#queryOnce}/{@code #computeCensus} already
     * use). A short-ttl row that ages past its own {@code expires_at} must
     * stop counting against the cap even though nothing consumed it.
     */
    @Test
    void expiredRowDoesNotCountAgainstTheCap_outSucceedsAfterExpiry() throws Exception {
        String tenant = freshTenant("expired");
        String room = "room-expired";
        outCap(tenant, room, "e1", 1L);   // a 1-second-lived row
        outCap(tenant, room, "e2", null); // a long-lived row, fills the cap with e1

        assertThatExceptionOfType(MaxLiveRowsExceededException.class)
                .isThrownBy(() -> outCap(tenant, room, "e3", null));

        Thread.sleep(1500); // e1 is now expired but not purged -- the row still exists

        byte[] id = outCap(tenant, room, "e3", null);
        assertThat(id).isNotNull();
    }

    // ── unbounded without the field ──────────────────────────────────────────

    /**
     * A template with no {@code max_live_rows} is unbounded, the behaviour
     * every template had before this field existed. Uses the bundled
     * {@code ledger/<session_id>} resource template directly -- it declares
     * no {@code max_live_rows} of its own, so this needs no extra template.
     */
    @Test
    void templateWithoutMaxLiveRows_unbounded() {
        String tenant = freshTenant("unbounded");
        String session = "session-unbounded";
        for (int i = 0; i < 20; i++) {
            repo.out(tenant, "ledger/" + session, Map.of("agent_id", "agent-" + i, "kind", "start"),
                    Map.of(), null, null, null);
        }
        var rows = repo.rdp(tenant, "ledger/" + session, null, 300, null);
        assertThat(rows).as("no cap -- every one of the 20 distinct identities landed").hasSize(20);
    }

    // ── the idempotent-refire bypass ─────────────────────────────────────────

    /**
     * RDR-211 leaves open whether an idempotent re-{@code out} of an EXISTING
     * identity counts against the cap; {@link
     * dev.nexus.service.tuples.TemplateSchema#maxLiveRows()}'s javadoc
     * decides it does not, because a refire adds no row ({@code
     * TupleRepository#writeOut}'s {@code onConflict} only refreshes {@code
     * expires_at}). Filling the cap exactly, then refiring one of the
     * existing identities, must succeed rather than refuse, and must not
     * grow the row count.
     */
    @Test
    void idempotentRefireOfAnExistingIdentity_bypassesTheCap() {
        String tenant = freshTenant("refire");
        String room = "room-refire";
        byte[] id1 = outCap(tenant, room, "f1", null);
        outCap(tenant, room, "f2", null);
        assertThatExceptionOfType(MaxLiveRowsExceededException.class)
                .isThrownBy(() -> outCap(tenant, room, "f3", null));

        // f1 refired: same keys, same identity -- a genuine refire, not a new row.
        byte[] id1Again = outCap(tenant, room, "f1", null);
        assertThat(id1Again).isEqualTo(id1);

        var rows = repo.rdp(tenant, "probe-cap/" + room, null, 10, null);
        assertThat(rows).as("still exactly the two original rows").hasSize(2);
    }
}

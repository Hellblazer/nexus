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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * F3 step 2 engine half (bead nexus-fetre; JDR-002, T2 nexus/f3-sender-identity-analysis-
 * 2026-10-21 section 3 Option B, non-strict). {@code out} stamps a server-written
 * {@code sender_session} on INSERT only; null when the caller had no minted session. A
 * refire or a retry never overwrites the first writer's stamp, and the claim paths
 * ({@code inp}/{@code in}) return it on the claimed row. {@code ackWithReply} is NOT
 * stamped here (nexus-g2ckl owns that): its reply row stays null, which this class pins
 * so the follow-up bead has a visible red to flip.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleSenderSessionTest {

    private static final String TENANT = "tuple-tenant-sender";
    private static final String SVC_ROLE = "svc_tuple_sender_test";
    private static final String SVC_PASS = "svc_tuple_sender_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TupleRepository repo;

    @BeforeAll
    void startAll() throws Exception {
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
        repo = new TupleRepository(new TenantScope(svcDs),
                TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
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

    private static String addr(String label) {
        return label + "-" + UUID.randomUUID().toString().substring(0, 10);
    }

    private TupleRepository.TupleRow onlyRow(String subspace, Map<String, String> pattern) {
        var rows = repo.rd(TENANT, subspace, pattern, 10, null, 0);
        assertThat(rows).as("exactly one row at %s", subspace).hasSize(1);
        return rows.get(0);
    }

    @Test
    void outWithASession_stampsIt_andRdReturnsIt() {
        String to = addr("stamped");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null, "session-A");
        assertThat(onlyRow("mailbox/" + to, Map.of("to", to)).senderSession()).isEqualTo("session-A");
    }

    @Test
    void outWithANullSession_storesNull() {
        String to = addr("unstamped");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null, null);
        assertThat(onlyRow("mailbox/" + to, Map.of("to", to)).senderSession()).isNull();
    }

    @Test
    void theSevenArgOut_storesNull() {
        String to = addr("legacy");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null);
        assertThat(onlyRow("mailbox/" + to, Map.of("to", to)).senderSession()).isNull();
    }

    @Test
    void aKeysTemplateRefireFromASecondSession_keepsTheFirstStamp() {
        String agent = addr("agent");
        Map<String, String> keys = Map.of("agent_id", agent, "kind", "start");
        String subspace = "ledger/" + addr("sess");
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, "session-A");
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, "session-B");
        assertThat(onlyRow(subspace, keys).senderSession()).isEqualTo("session-A");
    }

    @Test
    void aKeysTemplateRefireFromANullSession_doesNotClearTheStamp() {
        String agent = addr("agent");
        Map<String, String> keys = Map.of("agent_id", agent, "kind", "start");
        String subspace = "ledger/" + addr("sess");
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, "session-A");
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, null);
        assertThat(onlyRow(subspace, keys).senderSession()).isEqualTo("session-A");
    }

    @Test
    void aFirstNullWriterIsNotAdoptedByALaterSession_theRowKeepsNull() {
        // The first writer's stamp stands even when that stamp is "none": a refire
        // never sets the column, so a later minted session cannot claim the row.
        String agent = addr("agent");
        Map<String, String> keys = Map.of("agent_id", agent, "kind", "start");
        String subspace = "ledger/" + addr("sess");
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, null);
        repo.out(TENANT, subspace, keys, Map.of(), null, null, null, "session-B");
        assertThat(onlyRow(subspace, keys).senderSession()).isNull();
    }

    @Test
    void aLockTemplateRefireFromASecondSession_keepsTheFirstStamp() {
        String resource = addr("res");
        Map<String, String> keys = Map.of("resource", resource);
        String subspace = "lock/" + resource;
        repo.out(TENANT, subspace, keys, Map.of("from", "h1"), null, null, null, "session-A");
        repo.out(TENANT, subspace, keys, Map.of("from", "h1"), null, null, null, "session-B");
        assertThat(onlyRow(subspace, keys).senderSession()).isEqualTo("session-A");
    }

    @Test
    void aLockResetAfterExpiry_keepsTheFirstStamp_withTheBodyAndDimsItDescribes() throws Exception {
        String resource = addr("res");
        Map<String, String> keys = Map.of("resource", resource);
        String subspace = "lock/" + resource;
        repo.out(TENANT, subspace, keys, Map.of("from", "h1"), null, null, null, "session-A");
        try (Connection su = pg.createConnection("")) {
            var tuples = dev.nexus.service.jooq.nexus.Tables.TUPLES;
            assertThat(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(tuples)
                    .set(tuples.EXPIRES_AT, java.time.OffsetDateTime.now().minusSeconds(1))
                    .where(tuples.SUBSPACE.eq(subspace))
                    .execute()).as("the lock row is expired by hand").isEqualTo(1);
        }
        repo.out(TENANT, subspace, keys, Map.of("from", "h1"), null, null, null, "session-B");
        assertThat(onlyRow(subspace, keys).senderSession()).isEqualTo("session-A");
    }

    @Test
    void anIdenticalKeysNonceRetryFromASecondSession_succeedsAndKeepsTheFirstStamp() {
        String to = addr("retry");
        Map<String, String> keys = Map.of("to", to);
        byte[] first = repo.out(TENANT, "mailbox/" + to, keys, Map.of("from", "a"), "body", "n-1", null, "session-A");
        byte[] second = repo.out(TENANT, "mailbox/" + to, keys, Map.of("from", "a"), "body", "n-1", null, "session-B");
        assertThat(second).isEqualTo(first);
        assertThat(onlyRow("mailbox/" + to, keys).senderSession()).isEqualTo("session-A");
    }

    @Test
    void inpReturnsTheStampOnTheClaimedRow() {
        String to = addr("claim");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null, "session-A");
        Optional<TupleRepository.ClaimedTuple> claimed =
                repo.inp(TENANT, "mailbox/" + to, Map.of("to", to), "worker", 60);
        assertThat(claimed).isPresent();
        assertThat(claimed.get().tuple().senderSession()).isEqualTo("session-A");
    }

    @Test
    void inpOnAnUnstampedRowReturnsNull() {
        String to = addr("claim-null");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null, null);
        var claimed = repo.inp(TENANT, "mailbox/" + to, Map.of("to", to), "worker", 60);
        assertThat(claimed).isPresent();
        assertThat(claimed.get().tuple().senderSession()).isNull();
    }

    @Test
    void ackWithReply_replyRowIsNotStamped_thisBeadLeftItAlone() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        repo.out(TENANT, "mailbox/" + answerer, Map.of("to", answerer), Map.of("from", "asker"),
                "req", "n-" + UUID.randomUUID(), null, "session-A");
        String claimId = repo.inp(TENANT, "mailbox/" + answerer, Map.of("to", answerer), "w", 60)
                .orElseThrow().claimId();
        repo.ackWithReply(TENANT, claimId, "w", new TupleRepository.ReplySpec(
                "mailbox/" + asker, Map.of("to", asker), Map.of("from", "answerer"), "ans", null));
        assertThat(onlyRow("mailbox/" + asker, Map.of("to", asker)).senderSession())
                .as("nexus-g2ckl stamps replies; until then the column stays null").isNull();
    }
}

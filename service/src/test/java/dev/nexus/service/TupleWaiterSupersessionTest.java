// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-rxuiq: a parked announce-mode wait outlives the reader that issued
 * it, because the engine cannot see a client disconnect while a handler blocks.
 * A cancelled channel waiter's call, or a dead process's, stayed parked for up to
 * its timeout and stamped the next row as announced for nobody, so the reader's
 * successor never heard of it. The fix is a waiter token on the announce spec:
 * the newest token for a {@code (subspace, subscriber)} wins, and an older wait,
 * parked or new, returns {@code superseded} without querying or stamping.
 *
 * <p>Same hermetic Postgres harness as {@link TupleAnnounceTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleWaiterSupersessionTest {

    private static final String TENANT = "tuple-supersession-tenant";
    private static final String SVC_ROLE = "svc_tuple_supersession_test";
    private static final String SVC_PASS = "svc_tuple_supersession_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TupleRepository repo;
    ExecutorService pool;

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
        cfg.setMaximumPoolSize(20);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(new TenantScope(svcDs), registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 16);
        pool = Executors.newCachedThreadPool();
    }

    @AfterAll
    void stopAll() {
        pool.shutdownNow();
        svcDs.close();
    }

    private static TupleRepository.WaitSpec mailbox(String to, String waiter) {
        return new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null,
                new TupleRepository.WaitSpec.Announce(150, 5, null, waiter));
    }

    private static TupleRepository.WaitSpec board(String topic, String subscriber, String waiter) {
        return new TupleRepository.WaitSpec("board/" + topic, null, 10, null,
                new TupleRepository.WaitSpec.Announce(150, 5, subscriber, waiter));
    }

    private void mail(String to, String body) {
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                body, UUID.randomUUID().toString(), null);
    }

    private static String token(long timeNs) {
        return timeNs + "-" + UUID.randomUUID().toString().replace("-", "");
    }

    @Test
    void aParkedOlderWait_returnsSuperseded_andTheNewerWaitGetsTheRow() throws Exception {
        String to = "supersede-" + UUID.randomUUID();
        Future<List<TupleRepository.WaitResult>> older =
                pool.submit(() -> repo.waitAny(TENANT, List.of(mailbox(to, token(1_000))), 8));
        Thread.sleep(300); // the older wait is parked
        Future<List<TupleRepository.WaitResult>> newer =
                pool.submit(() -> repo.waitAny(TENANT, List.of(mailbox(to, token(2_000))), 8));

        List<TupleRepository.WaitResult> olderResult = older.get(3, TimeUnit.SECONDS);
        assertThat(olderResult).as("woken by the newer waiter, well inside its own 8 s park").hasSize(1);
        assertThat(olderResult.get(0).superseded()).isTrue();
        assertThat(olderResult.get(0).tuples()).isEmpty();

        mail(to, "after the handover");
        List<TupleRepository.WaitResult> newerResult = newer.get(5, TimeUnit.SECONDS);
        assertThat(newerResult).hasSize(1);
        assertThat(newerResult.get(0).superseded()).isFalse();
        assertThat(newerResult.get(0).tuples()).hasSize(1);
        assertThat(newerResult.get(0).tuples().get(0).announceCount())
                .as("announced exactly once, and to the live waiter").isEqualTo(1);
    }

    @Test
    void anOlderWaitArrivingLate_isRefusedWithoutStampingADueRow() {
        String to = "late-" + UUID.randomUUID();
        assertThat(repo.waitAny(TENANT, List.of(mailbox(to, token(2_000))), 0)).isEmpty();
        mail(to, "due now");

        List<TupleRepository.WaitResult> late = repo.waitAny(TENANT, List.of(mailbox(to, token(1_000))), 0);
        assertThat(late).hasSize(1);
        assertThat(late.get(0).superseded()).isTrue();
        assertThat(late.get(0).tuples()).isEmpty();

        List<TupleRepository.WaitResult> current = repo.waitAny(TENANT, List.of(mailbox(to, token(2_000).replaceFirst("-.*", "-") + "z")), 0);
        assertThat(current).as("a token equal in time but a larger id is newer, and the row is still unannounced")
                .hasSize(1);
        assertThat(current.get(0).tuples().get(0).announceCount()).isEqualTo(1);
    }

    @Test
    void theCurrentWaiterReissuingItsOwnToken_isNeverSuperseded() {
        String to = "same-" + UUID.randomUUID();
        String mine = token(5_000);
        assertThat(repo.waitAny(TENANT, List.of(mailbox(to, mine)), 0)).isEmpty();
        mail(to, "for me");
        List<TupleRepository.WaitResult> again = repo.waitAny(TENANT, List.of(mailbox(to, mine)), 0);
        assertThat(again).hasSize(1);
        assertThat(again.get(0).superseded()).isFalse();
    }

    @Test
    void boardSubscribers_doNotSupersedeEachOther() {
        String topic = "sup-" + UUID.randomUUID();
        repo.waitAny(TENANT, List.of(board(topic, "session-a", token(9_000))), 0);
        repo.out(TENANT, "board/" + topic, Map.of("topic", topic), Map.of("from", "x"),
                "post", UUID.randomUUID().toString(), null);
        List<TupleRepository.WaitResult> b = repo.waitAny(TENANT, List.of(board(topic, "session-b", token(1_000))), 0);
        assertThat(b).as("an older token for ANOTHER subscriber is not fenced by session-a's").hasSize(1);
        assertThat(b.get(0).superseded()).isFalse();
        assertThat(b.get(0).tuples()).hasSize(1);
    }

    @Test
    void aSpecWithoutAToken_keepsTodaysBehaviour() {
        String to = "untokened-" + UUID.randomUUID();
        repo.waitAny(TENANT, List.of(mailbox(to, token(9_000))), 0);
        mail(to, "x");
        List<TupleRepository.WaitResult> untokened = repo.waitAny(TENANT, List.of(mailbox(to, null)), 0);
        assertThat(untokened).hasSize(1);
        assertThat(untokened.get(0).superseded()).isFalse();
    }

    @Test
    void aPartlySupersededWait_returnsOnlyTheSupersededSpec_andStampsNothing() {
        // Waiter 1 watches a mailbox and a board; waiter 2, newer, takes over the
        // mailbox only. Waiter 1's next call must come back superseded for the
        // mailbox and must not stamp the board post for its subscriber either:
        // the client stops the whole waiter on any superseded spec, so a stamp here
        // would be an announcement nobody delivers.
        String to = "partial-" + UUID.randomUUID();
        String topic = "partial-" + UUID.randomUUID();
        String older = token(1_000);
        TupleRepository.WaitSpec olderMailbox = mailbox(to, older);
        TupleRepository.WaitSpec olderBoard = board(topic, "session-p", older);
        assertThat(repo.waitAny(TENANT, List.of(olderMailbox, olderBoard), 0)).isEmpty();
        assertThat(repo.waitAny(TENANT, List.of(mailbox(to, token(2_000))), 0)).isEmpty();
        repo.out(TENANT, "board/" + topic, Map.of("topic", topic), Map.of("from", "x"),
                "post", UUID.randomUUID().toString(), null);

        List<TupleRepository.WaitResult> partial = repo.waitAny(TENANT, List.of(olderMailbox, olderBoard), 0);
        assertThat(partial).hasSize(1);
        assertThat(partial.get(0).subspace()).isEqualTo("mailbox/" + to);
        assertThat(partial.get(0).superseded()).isTrue();

        List<TupleRepository.WaitResult> board = repo.waitAny(TENANT, List.of(board(topic, "session-p", older)), 0);
        assertThat(board).as("the board post was not stamped for session-p by the superseded call").hasSize(1);
        assertThat(board.get(0).tuples().get(0).announceCount()).isEqualTo(1);
    }

    @Test
    void malformedTokens_areRefused() {
        // The last one matches the pattern but overflows a long: accepted, it would
        // poison its key, since every later comparison against it would throw.
        for (String bad : List.of("", "abc", "12-", "-abc", "12-a_b", "x-1", "9999999999999999999-abc")) {
            assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(150, 5, null, bad))
                    .as(bad).isInstanceOf(SchemaViolationException.class);
        }
    }
}

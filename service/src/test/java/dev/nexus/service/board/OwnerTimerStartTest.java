// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.NexusService;
import dev.nexus.service.NexusServiceTestAccess;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: the deadline timer is started. {@code BoardOwnerWiring.start} binds the timer to the stamper and
 * schedules the restart rebuild; {@code OwnerStamper} arms nothing while its timer is null, so a start that is never
 * called leaves every other test green and the engine with no deadline. These tests see the scheduler: the first on a
 * wiring of its own, the others on a live {@code NexusService} whose sweep scheduler records what is registered, so
 * deleting the {@code boardOwner.start(...)} line of {@code NexusService} fails them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerTimerStartTest {

    private static final String BOOT = "boot-owner-timer-start";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Duration DEADLINE = Duration.ofMinutes(20);
    /** The timer wakes first at half the deadline, for next up's nudge (RDR-252 audit S2); the deadline follows it. */
    private static final Duration NUDGE = DEADLINE.dividedBy(2);

    StamperEnv se;
    NexusService service;
    final RecordingScheduler engineScheduler = new RecordingScheduler();
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
        try (Connection su = se.env.ids.pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), BOOT, TenantConstants.DEFAULT_TENANT,
                TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
        }
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = NexusServiceTestAccess.withSweepScheduler(0, BOOT, se.env.ids.ds, registry, engineScheduler);
        service.start();
    }

    @AfterAll
    void down() throws Exception {
        if (service != null) {
            service.stop();
        }
        se.close();
    }

    private String[] redOnY() {
        return redOnY(se.tenant());
    }

    private String[] redOnY(String t) {
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        return new String[] {t, l, a, c};
    }

    /**
     * A tenant whose open red's episode is long past, that the restart rebuild visits AFTER every other tenant: its id
     * sorts last ({@code t-zzzz} follows every {@code t-<hex>} the fixture mints), and the rebuild arms it at 0 ms. The
     * rebuild visits every tenant of the class's database, so a test that reads "the" last deadline instead of its own
     * tenant's reads this one. Seeding it makes that order the one every run sees, not the one JUnit's method order
     * happens to give.
     */
    private void seedStaleRedTenantVisitedLast() {
        Instant now = se.env.ids.clock.instant();
        se.env.ids.clock.set(Instant.parse("2026-11-16T00:00:00Z"));
        String[] stale = redOnY("t-zzzz" + UUID.randomUUID().toString().substring(0, 4));
        se.done(stale[0], StamperEnv.BASE, "Y", "failure");
        se.env.ids.clock.set(now);
    }

    /**
     * The deadlines the live engine has armed for {@code tenant} (one per open-red topic). The recorded one-shots carry
     * no tenant: each is the timer's private {@code run(key)}. So this asks the engine's own tenant-reset evictor, which
     * cancels exactly the tenant's armed deadlines, and returns the recorded ones whose futures that cancelled. It ends
     * the tenant's deadlines, so call it once, last, when the test is done with them.
     */
    private List<RecordingScheduler.Scheduled> deadlinesArmedFor(String tenant) {
        var evictor = NexusServiceTestAccess.tenantKeyedState(service).stream()
            .filter(s -> s.name().equals("AckDeadlineTimer")).findFirst().orElseThrow();
        var cancelledBefore = engineScheduler.deadlines().stream().filter(d -> engineScheduler.futureOf(d).isCancelled()).toList();
        int armed = evictor.evictTenant(tenant);
        var own = engineScheduler.deadlines().stream()
            .filter(d -> engineScheduler.futureOf(d).isCancelled() && !cancelledBefore.contains(d)).toList();
        assertThat(own).as("the tenant's %d armed topic(s), as the recorded one-shots the evictor cancelled", armed).hasSize(armed);
        return own;
    }

    private void seedToken(String raw, String tenant, String scope) throws Exception {
        try (Connection su = se.env.ids.pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), raw, tenant, "timer-" + scope, scope, null, null);
        }
    }

    private void redDone(String bearer, String sha) throws Exception {
        var body = Map.of("subspace", "board/ci/nexus-develop--done", "keys", Map.of("topic", "nexus-develop--done"),
            "dims", Map.of("from", "github", "kind", "sha", "sha", sha, "outcome", "failure"),
            "body", "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}", "nonce", UUID.randomUUID().toString());
        var r = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/tuples/out")
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(), HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
    }

    private static <T> T eventually(Supplier<T> read, java.util.function.Predicate<T> ok) throws Exception {
        long end = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(20);
        T v = read.get();
        while (!ok.test(v) && System.nanoTime() < end) {
            Thread.sleep(50);
            v = read.get();
        }
        return v;
    }

    @Test
    void startBindsTheTimerToTheStamperAndSchedulesARebuildThatConsultsTheTenants() {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");   // se.stamper has no timer: the owner row posts, nothing is armed
        Instant episode = Instant.parse((String) se.ownerRows(f[0], StamperEnv.BASE).getFirst().get("episode_at"));
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);
        var scheduler = new RecordingScheduler();
        AtomicInteger consulted = new AtomicInteger();

        wiring.start(scheduler, () -> {
            consulted.incrementAndGet();
            return List.of(f[0]);
        });

        assertThat(wiring.timer()).as("start creates the timer").isNotNull();
        RecordingScheduler.Scheduled rebuild = scheduler.rebuild();
        assertThat(rebuild).as("start schedules the rebuild, after its delay").isNotNull();
        assertThat(consulted).as("the tenants are read when the rebuild runs, not at start").hasValue(0);
        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("nothing armed before the rebuild runs").isNull();

        rebuild.task().run();

        assertThat(consulted).as("the rebuild consulted the tenants supplier").hasValue(1);
        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("the open red's deadline is armed")
            .isEqualTo(episode.plus(NUDGE));
        assertThat(scheduler.deadlines()).hasSize(1);

        // The timer is the stamper's: a red evaluated by the wiring's stamper after start arms it too.
        String[] g = redOnY();
        se.doneRowOnly(g[0], StamperEnv.BASE, "Y", "failure");
        wiring.stamper().afterOut(g[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");
        assertThat(wiring.timer().armedAt(g[0], StamperEnv.BASE)).as("a red written after start arms the timer").isNotNull();
    }

    @Test
    void aFailedFirstCensusIsRetriedUntilItSucceedsAndThenArmsTheDeadline() {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        Instant episode = Instant.parse((String) se.ownerRows(f[0], StamperEnv.BASE).getFirst().get("episode_at"));
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);
        var scheduler = new RecordingScheduler();
        AtomicInteger census = new AtomicInteger();

        wiring.start(scheduler, () -> {
            if (census.incrementAndGet() == 1) {
                throw new IllegalStateException("the tenant census failed: the database is not up yet");
            }
            return List.of(f[0]);
        });
        scheduler.secondsCalls().getFirst().task().run();

        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("the first census failed: nothing armed").isNull();
        assertThat(scheduler.secondsCalls()).as("the failed census scheduled another attempt").hasSize(2);
        assertThat(scheduler.secondsCalls().get(1).delay()).as("with a longer delay").isGreaterThan(BoardOwnerWiring.REBUILD_DELAY_SECONDS);

        scheduler.secondsCalls().get(1).task().run();

        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("the retry armed the open red's deadline")
            .isEqualTo(episode.plus(NUDGE));
        assertThat(scheduler.secondsCalls()).as("a full census ends the retries").hasSize(2);
    }

    @Test
    void aTenantTheCensusCannotListMakesTheCensusNotFull() {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);

        assertThat(wiring.rebuild(List.of(f[0]))).as("every tenant read: a full census").isTrue();
        assertThat(wiring.rebuild(List.of(f[0], "not a tenant id\u0000"))).as("one tenant failed: not a full census").isFalse();
    }

    private String[] twoRedTopics() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        assertThat(se.env.form(t, l, dev.nexus.service.http.GroupTestEnv.body("push-train", l,
            List.of("nexus-develop", "nexus-main"))).status()).isEqualTo(200);
        assertThat(se.env.join(t, a, "push-train", "member").status()).isEqualTo(200);
        assertThat(se.env.join(t, c, "push-train", "member").status()).isEqualTo(200);
        for (String base : List.of("nexus-develop", "nexus-main")) {
            se.push(t, base, "X", "G0", "sess-alpha");
            se.push(t, base, "Y", "X", "sess-carol");
            se.done(t, base, "Y", "failure");   // the fixture's stamper has no timer: an owner row, nothing armed
        }
        return new String[] {t};
    }

    @Test
    void aTopicWhoseRearmFailsIsRetriedAloneWhileTheOtherTopicStaysArmed() {
        String t = twoRedTopics()[0];
        var stamper = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock);
        var calls = new java.util.concurrent.ConcurrentHashMap<String, AtomicInteger>();
        var healed = new java.util.concurrent.atomic.AtomicBoolean();
        var wiring = new BoardOwnerWiring(se.tuples, stamper, (tenant, base) -> {
            calls.computeIfAbsent(base, k -> new AtomicInteger()).incrementAndGet();
            if (base.equals("nexus-main") && !healed.get()) {
                throw new IllegalStateException("the rearm of nexus-main failed");
            }
            stamper.rearm(tenant, base);
        }, se.env.ids.clock);
        var scheduler = new RecordingScheduler();
        AtomicInteger census = new AtomicInteger();

        wiring.start(scheduler, () -> {
            census.incrementAndGet();
            return List.of(t);
        });
        scheduler.secondsCalls().getFirst().task().run();

        assertThat(wiring.timer().armedAt(t, "nexus-develop")).as("the healthy topic is armed").isNotNull();
        assertThat(wiring.timer().armedAt(t, "nexus-main")).as("the failed topic is not").isNull();
        assertThat(scheduler.secondsCalls()).as("a topic failed: the census is not full and another attempt is scheduled").hasSize(2);

        healed.set(true);
        scheduler.secondsCalls().get(1).task().run();

        assertThat(wiring.timer().armedAt(t, "nexus-main")).as("the retry armed the failed topic").isNotNull();
        assertThat(calls.get("nexus-develop")).as("the healthy topic was not re-evaluated by the retry").hasValue(1);
        assertThat(calls.get("nexus-main")).hasValue(2);
        assertThat(census).as("the tenants were read once: the retry works from the failed pairs").hasValue(1);
        assertThat(scheduler.secondsCalls()).as("a full census ends the retries").hasSize(2);
    }

    @Test
    void aStuckCensusRaisesOneStableNamedErrorEventAfterTwelveAttemptsAndLogsTheStackOnlyOnTheFirst() {
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);
        var scheduler = new RecordingScheduler();
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(BoardOwnerWiring.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            wiring.start(scheduler, () -> {
                throw new IllegalStateException("down");
            });
            for (int i = 0; i < 13; i++) {
                scheduler.secondsCalls().get(i).task().run();
            }
        } finally {
            logger.detachAppender(logs);
        }

        var errors = logs.list.stream().filter(e -> e.getLevel() == ch.qos.logback.classic.Level.ERROR).toList();
        assertThat(errors).as("one error-level event, at the twelfth attempt").hasSize(1);
        assertThat(errors.getFirst().getFormattedMessage()).contains("event=board_ack_deadline_rebuild_stuck").contains("attempt=12");
        var failures = logs.list.stream().filter(e -> e.getFormattedMessage().contains("event=board_ack_deadline_rebuild_failed")).toList();
        assertThat(failures).hasSize(13);
        assertThat(failures.getFirst().getThrowableProxy()).as("the first failure carries the stack").isNotNull();
        assertThat(failures.stream().skip(1).map(e -> e.getThrowableProxy())).as("the later ones do not").containsOnlyNulls();
    }

    @Test
    void theRetryDelayDoublesAndStopsGrowingAtTheCap() {
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);
        var scheduler = new RecordingScheduler();

        wiring.start(scheduler, () -> {
            throw new IllegalStateException("down");
        });
        for (int i = 0; i < 12; i++) {
            scheduler.secondsCalls().get(i).task().run();
        }

        var delays = scheduler.secondsCalls().stream().map(RecordingScheduler.Scheduled::delay).toList();
        assertThat(delays).as("5 s first, then double, then the cap").startsWith(5L, 10L, 20L, 40L, 80L, 160L, 300L, 300L);
        assertThat(delays).as("never past the cap").allMatch(d -> d <= BoardOwnerWiring.REBUILD_MAX_DELAY_SECONDS);
        assertThat(delays).as("one attempt is always waiting").hasSize(13);
    }

    @Test
    void aRedDoneWriteOnALiveEngineLeavesADeadlineArmed() throws Exception {
        se.env.ids.clock.set(Instant.now());   // the engine reads the system clock: the seats' leases must be fresh by it
        String[] f = redOnY();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, f[0], TokenStore.SCOPE_BOARD_CI);
        int before = engineScheduler.deadlines().size();
        redDone(adapter, "Y");
        assertThat(eventually(() -> se.ownerRows(f[0], StamperEnv.BASE), r -> !r.isEmpty())).as("the owner row").hasSize(1);
        var armed = eventually(engineScheduler::deadlines, d -> d.size() > before);

        assertThat(armed).as("the live engine's stamper armed a deadline after the red --done write").hasSizeGreaterThan(before);
        assertThat(armed.getLast().millis()).as("the group's 20-minute deadline, counted from the episode")
            .isBetween(Duration.ofMinutes(9).toMillis(), NUDGE.toMillis());
    }

    @Test
    void theLiveEngineSchedulesTheRestartRebuildAndItRearmsAnOpenRed() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String[] f = redOnY();
        seedToken("adapter-" + UUID.randomUUID(), f[0], TokenStore.SCOPE_BOARD_CI);   // a token makes the tenant one the engine knows
        se.done(f[0], StamperEnv.BASE, "Y", "failure");   // posted by the fixture's stamper: an owner row and no armed deadline
        seedStaleRedTenantVisitedLast();
        RecordingScheduler.Scheduled rebuild = engineScheduler.rebuild();
        assertThat(rebuild).as("NexusService scheduled the rebuild on its sweep scheduler").isNotNull();

        rebuild.task().run();

        var own = deadlinesArmedFor(f[0]);
        assertThat(own).as("the rebuild read the engine's tenants and armed this tenant's open red, once").hasSize(1);
        assertThat(own.getFirst().millis()).as("this tenant's deadline, not the stale tenant the rebuild visits last")
            .isBetween(Duration.ofMinutes(9).toMillis(), NUDGE.toMillis());
    }

    @Test
    void theRestartRebuildVisitsATenantThatHoldsNoTokenRowButHasTupleRows() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String[] f = redOnY();   // no seedToken: the tenant is in tuple_tenants (its rows) and not in service_tokens
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        seedStaleRedTenantVisitedLast();
        RecordingScheduler.Scheduled rebuild = engineScheduler.rebuild();

        rebuild.task().run();

        var own = deadlinesArmedFor(f[0]);
        assertThat(own).as("the rebuild reached a tenant with owner rows and no token, and armed its red once").hasSize(1);
        assertThat(own.getFirst().millis()).as("this tenant's deadline, not the stale tenant the rebuild visits last")
            .isBetween(Duration.ofMinutes(9).toMillis(), NUDGE.toMillis());
    }

    @Test
    void theTenantResetEvictorOfTheLiveEngineCancelsADeadlineTheEngineArmed() throws Exception {
        se.env.ids.clock.set(Instant.now());
        String[] f = redOnY();
        String adapter = "adapter-" + UUID.randomUUID();
        seedToken(adapter, f[0], TokenStore.SCOPE_BOARD_CI);
        int before = engineScheduler.deadlines().size();
        redDone(adapter, "Y");
        var armed = eventually(engineScheduler::deadlines, d -> d.size() > before);
        assertThat(armed).hasSizeGreaterThan(before);
        var task = armed.getLast();
        assertThat(engineScheduler.futureOf(task).isCancelled()).as("armed, not cancelled").isFalse();
        var evictor = NexusServiceTestAccess.tenantKeyedState(service).stream()
            .filter(s -> s.name().equals("AckDeadlineTimer")).findFirst().orElse(null);
        assertThat(evictor).as("the engine registers the AckDeadlineTimer evictor for the tenant reset").isNotNull();

        assertThat(evictor.evictTenant(f[0])).as("the one armed topic of the tenant").isEqualTo(1);

        assertThat(engineScheduler.futureOf(task).isCancelled()).as("the reset cancelled the tenant's deadline").isTrue();
        assertThat(evictor.evictTenant(f[0])).as("nothing left").isZero();
    }
}

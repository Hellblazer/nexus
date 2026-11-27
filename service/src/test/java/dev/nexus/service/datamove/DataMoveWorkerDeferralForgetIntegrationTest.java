// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.db.PciBuilders;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CopyOnWriteArraySet;
import java.util.concurrent.atomic.AtomicReference;

/**
 * RDR-229 P3.7 row 23 (nexus-p2m1x.27, F-E): a move that leaves a run of deferrals other than through {@code finish()} (a failure
 * the worker records, a supersede) tells the finisher to forget it, so the next wait reports its own age and not the old start
 * (the 6 h overdue warning is what the operator reads). The finisher here is a {@link BuildWait} behind the contract
 * {@link VerifyingFinisher} keeps: {@code finish()} defers or ends the run, {@code forget()} ends it. The worker is real, on the
 * pg_background image.
 */
class DataMoveWorkerDeferralForgetIntegrationTest extends DataMoveIntegrationBase {

    private static final Instant T0 = Instant.parse("2026-11-24T00:00:00Z");
    private static final PciBuilders.Building ONE = new PciBuilders.Building(1, List.of(4101));
    private static final Duration PAST_RETRY = Duration.ofMillis(60);

    /** Defers the labels in {@link #deferring}; any other label's finish ends the run, as a completion would. */
    private static final class DeferringFinisher implements MoveFinisher {
        final BuildWait wait = new BuildWait();
        final Set<String> deferring = new CopyOnWriteArraySet<>();
        final List<String> forgotten = new CopyOnWriteArrayList<>();

        void at(Instant now) {
            wait.clock = Clock.fixed(now, ZoneOffset.UTC);
        }

        @Override
        public Result finish(Context context) {
            String label = context.move().label();
            if (deferring.contains(label)) {
                return wait.defer(label, ONE, BuildWait.Path.BEFORE_LOCKS);
            }
            wait.clear(label);   // finish() ends the run for anything but a deferral, as VerifyingFinisher.finish does
            return Result.completed();
        }

        @Override
        public void forget(String label) {
            forgotten.add(label);
            wait.clear(label);
        }
    }

    private static void pastTheRetry() throws InterruptedException {
        Thread.sleep(PAST_RETRY.toMillis());   // the worker's blocked retry is 10 ms
    }

    private String reasonOfDeferral(DataMoveWorker w, String label) {
        String reason = w.states().state(label).orElseThrow().reason();
        assertThat(reason).as("a deferral").startsWith(BuildWait.REASON_PREFIX);
        return reason;
    }

    @Test
    void aFailureOutsideFinishWhileDeferring_endsTheRun_soTheNextWaitReportsItsOwnAge() throws Exception {
        register("d-fail", "tenant_plain", 50, 0);
        var finisher = new DeferringFinisher();
        finisher.deferring.add("d-fail");
        finisher.at(T0);
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, finisher);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOfDeferral(w, "d-fail")).contains("deferred_minutes=0");
        assertThat(finisher.wait.age("d-fail")).as("the run began").isNotNull();

        // a pass that fails before it reaches finish(): the admin connection cannot be opened
        w.beforeSessionOpenHook = () -> {
            throw new IllegalStateException("no admin connection");
        };
        pastTheRetry();
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(w.states().state("d-fail").orElseThrow().reason()).contains("the admin connection could not be opened");
        assertThat(finisher.forgotten).as("the worker told the finisher").contains("d-fail");
        assertThat(finisher.wait.age("d-fail")).as("the failure ended the run of deferrals").isNull();

        // the operator's reset, seven hours on: the wait that follows is a new one
        w.beforeSessionOpenHook = null;
        finisher.at(T0.plus(Duration.ofHours(7)));
        pastTheRetry();
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOfDeferral(w, "d-fail")).as("its own age, not the old start's seven hours")
            .contains("deferred_minutes=0").doesNotContain("overdue");
    }

    @Test
    void aSupersedeWhileDeferring_endsTheRun_soAWaitOfTheSameLabelReportsItsOwnAge() throws Exception {
        register("d-old", "tenant_plain", 50, 0);
        var finisher = new DeferringFinisher();
        finisher.deferring.add("d-old");
        finisher.at(T0);
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, finisher);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOfDeferral(w, "d-old")).contains("deferred_minutes=0");
        assertThat(finisher.wait.age("d-old")).isNotNull();

        DataMoveTestFixtures.register(ctx(), "d-new", "tenant_plain", 50, 0, "d-old", "stamp-template.sql");
        pastTheRetry();
        w.runCycle();

        assertThat(w.states().state("d-old")).as("replaced, so no longer reported").isEmpty();
        assertThat(finisher.forgotten).as("the worker told the finisher").contains("d-old");
        assertThat(finisher.wait.age("d-old")).as("the supersede ended the run of deferrals").isNull();
        finisher.at(T0.plus(Duration.ofHours(7)));
        assertThat(finisher.wait.defer("d-old", ONE, BuildWait.Path.BEFORE_LOCKS).reason())
            .as("a later wait of the label: its own age").contains("deferred_minutes=0").doesNotContain("overdue");
    }

    @Test
    void twoDeferralsSeparatedByACompletion_theSecondShowsItsOwnAge() throws Exception {
        register("d-twice", "tenant_plain", 50, 0);
        var finisher = new DeferringFinisher();
        finisher.deferring.add("d-twice");
        finisher.at(T0);
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, finisher);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOfDeferral(w, "d-twice")).contains("deferred_minutes=0");

        finisher.deferring.clear();   // the next finish completes
        pastTheRetry();
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(finisher.wait.age("d-twice")).as("the completion ended the run").isNull();

        // the move is open again as far as the registry says (the stand-in completed nothing): a new wait, seven hours on
        finisher.deferring.add("d-twice");
        finisher.at(T0.plus(Duration.ofHours(7)));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOfDeferral(w, "d-twice")).contains("deferred_minutes=0").doesNotContain("overdue");
    }

    // ── the page a counted failure latched survives a not-now finish (nexus-p2m1x.27 row 23) ──────────────

    private static final FailureHandler.PageInfo PAGED =
        new FailureHandler.PageInfo(true, true, "on_complete", 900, true);

    /** A finisher whose next answer the test sets; a failure it reports goes to a handler that pages. */
    private DataMoveWorker pagedWorker(AtomicReference<MoveFinisher> next) throws Exception {
        FailureHandler paging = failure -> FailureHandler.Disposition.blocked("retry 3: on_complete lock timeout; needs an operator",
            "55P03", 3, PAGED);
        return worker(installed(), null, paging, c -> next.get().finish(c));
    }

    private static MoveFinisher onCompleteTimeout() {
        return c -> MoveFinisher.Result.report(new FailureHandler.Failure(c.move(), FailureHandler.Stage.ON_COMPLETE, null,
            "dm_test", "tenant_plain", null, 0, new SQLException("canceling statement due to lock timeout", "55P03"),
            "on_complete of " + c.move().label() + " did not complete: sqlstate=55P03", c.admin(), false));
    }

    /** The {@code event=data_move_needs_operator} lines the root logger saw while {@code body} ran, one string each. */
    private static List<String> pagesLogged(Runnable body) {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
            return logs.list.stream().map(ILoggingEvent::getFormattedMessage)
                .filter(m -> m.contains("event=data_move_needs_operator")).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void aDeferralAfterAPage_keepsThePage_theWorkersBlockedHandlingReportsNoneOfItsOwn() throws Exception {
        register("d-page", "tenant_plain", 50, 0);
        var wait = new BuildWait();
        var next = new AtomicReference<MoveFinisher>(onCompleteTimeout());
        var w = pagedWorker(next);

        List<String> first = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));
        assertThat(w.states().state("d-page").orElseThrow().needsOperator()).as("non-vacuity: the failure paged").isTrue();
        assertThat(first).as("non-vacuity: and logged its one page").hasSize(1);

        next.set(c -> wait.defer(c.move().label(), ONE, BuildWait.Path.BEFORE_LOCKS));
        pastTheRetry();
        List<String> second = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));

        var state = w.states().state("d-page").orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(state.reason()).as("a deferral").startsWith(BuildWait.REASON_PREFIX);
        assertThat(state.needsOperator()).as("the deferral cleared no page").isTrue();
        assertThat(state.step()).isEqualTo("on_complete");
        assertThat(state.boundMs()).isEqualTo(900);
        assertThat(state.terminatesIndexBuilds()).isTrue();
        assertThat(second).as("and logged no second page: the failure's is the only one (a kept page is entered=false)").isEmpty();
    }

    @Test
    void anyBlockedFinishAfterAPage_keepsIt_notOnlyADeferral() throws Exception {
        register("d-blocked", "tenant_plain", 50, 0);
        var next = new AtomicReference<MoveFinisher>(onCompleteTimeout());
        var w = pagedWorker(next);
        assertThat(w.runCycle()).isEqualTo(1);

        next.set(c -> MoveFinisher.Result.blocked("some other not-now"));
        pastTheRetry();
        assertThat(w.runCycle()).isEqualTo(1);

        var state = w.states().state("d-blocked").orElseThrow();
        assertThat(state.reason()).isEqualTo("some other not-now");
        assertThat(state.needsOperator()).isTrue();
        assertThat(state.step()).isEqualTo("on_complete");
    }

    @Test
    void aDeferralOfAMoveThatWasNeverPaged_carriesNoPage() throws Exception {
        register("d-plain", "tenant_plain", 50, 0);
        var wait = new BuildWait();
        var w = pagedWorker(new AtomicReference<>(c -> wait.defer(c.move().label(), ONE, BuildWait.Path.BEFORE_LOCKS)));

        assertThat(w.runCycle()).isEqualTo(1);

        var state = w.states().state("d-plain").orElseThrow();
        assertThat(state.reason()).startsWith(BuildWait.REASON_PREFIX);
        assertThat(state.needsOperator()).as("nothing latched, so nothing kept").isFalse();
        assertThat(state.step()).isNull();
    }

    // ── the other BLOCKED sources that carried no page keep the latched one (nexus-p2m1x.27, 11-24 14:27 follow-up 2) ──────

    /** A move whose CALL fails on every pass (a template without its chunking clause), so a pass reaches the failure handler. */
    private void registerFailingCall(String label) {
        DataMoveTestFixtures.register(ctx(), label, "leafed", 50, 0, null, "no-clause-template.sql");
    }

    /** Pages on its first failure, throws on every later one: the handler that fails after the page latched. */
    private static final class PagesThenThrows implements FailureHandler {
        private final java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();

        @Override
        public Disposition onFailure(Failure failure) {
            if (calls.incrementAndGet() == 1) {
                return Disposition.blocked("retry 3: needs an operator", "55P03", 3, PAGED);
            }
            throw new IllegalStateException("the handler's own connection is gone");
        }
    }

    @Test
    void aFailureHandlerThatThrowsAfterAPage_keepsThePage_andLogsNoSecondOne() throws Exception {
        registerFailingCall("d-throws");
        var w = worker(installed(), null, new PagesThenThrows(), MoveFinisher.RUNS_ONLY);

        List<String> first = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));
        assertThat(w.states().state("d-throws").orElseThrow().needsOperator()).as("non-vacuity: the first failure paged").isTrue();
        assertThat(first).as("non-vacuity: and logged its one page").hasSize(1);

        pastTheRetry();
        List<String> second = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));

        var state = w.states().state("d-throws").orElseThrow();
        assertThat(state.reason()).as("non-vacuity: the handler failed on this pass").contains("the failure handler failed");
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(state.needsOperator()).as("a handler that could not decide does not clear the page a counted failure latched").isTrue();
        assertThat(state.step()).isEqualTo("on_complete");
        assertThat(second).as("and the kept page is not announced again").isEmpty();
    }

    @Test
    void aStoppedPassAfterAPage_keepsThePage_andLogsNoSecondOne() throws Exception {
        registerFailingCall("d-stop");
        FailureHandler paging = failure -> FailureHandler.Disposition.blocked("retry 3: needs an operator", "55P03", 3, PAGED);
        var w = worker(installed(), null, paging, MoveFinisher.RUNS_ONLY);
        // the first pass fails before any call connection is opened (the admin connection cannot be opened), and the handler pages it
        w.beforeSessionOpenHook = () -> {
            throw new IllegalStateException("no admin connection");
        };

        List<String> first = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));
        assertThat(w.states().state("d-stop").orElseThrow().needsOperator()).as("non-vacuity: the failure paged").isTrue();
        assertThat(first).as("non-vacuity: and logged its one page").hasSize(1);

        // the engine goes down as the next pass opens its call connection: the pass ends at once, with no failure for the handler
        w.beforeSessionOpenHook = null;
        w.afterConnectionOpenedHook = w::stop;
        pastTheRetry();
        List<String> second = pagesLogged(() -> assertThat(w.runCycle()).isEqualTo(1));

        var state = w.states().state("d-stop").orElseThrow();
        assertThat(state.reason()).as("non-vacuity: the pass ended because the worker was stopped").contains("stopped");
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(state.needsOperator()).as("a stop is not the operator's reset: the page stays").isTrue();
        assertThat(state.step()).isEqualTo("on_complete");
        assertThat(second).as("and the kept page is not announced again").isEmpty();
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.PciBuilders;
import org.junit.jupiter.api.Test;

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

    @Test
    void aDeferralAfterAPage_keepsThePage_theWorkersBlockedHandlingReportsNoneOfItsOwn() throws Exception {
        register("d-page", "tenant_plain", 50, 0);
        var wait = new BuildWait();
        var next = new AtomicReference<MoveFinisher>(onCompleteTimeout());
        var w = pagedWorker(next);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(w.states().state("d-page").orElseThrow().needsOperator()).as("non-vacuity: the failure paged").isTrue();

        next.set(c -> wait.defer(c.move().label(), ONE, BuildWait.Path.BEFORE_LOCKS));
        pastTheRetry();
        assertThat(w.runCycle()).isEqualTo(1);

        var state = w.states().state("d-page").orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(state.reason()).as("a deferral").startsWith(BuildWait.REASON_PREFIX);
        assertThat(state.needsOperator()).as("the deferral cleared no page").isTrue();
        assertThat(state.step()).isEqualTo("on_complete");
        assertThat(state.boundMs()).isEqualTo(900);
        assertThat(state.terminatesIndexBuilds()).isTrue();
        assertThat(w.states().announce("d-page")).as("and announced nothing new: the failure's page is the only one").isFalse();
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
}

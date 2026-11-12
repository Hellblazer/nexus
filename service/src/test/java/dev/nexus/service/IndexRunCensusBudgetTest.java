// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.IndexRunCensusReads;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.LongSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-f3yqd: the time bounds of {@code GET /v1/catalog/index-run/census}: a bucket never arms a spent budget (PostgreSQL
 * reads {@code statement_timeout = 0} as no limit at all), a timeout on a LATER statement of a bucket is that bucket's
 * {@code timed_out} and never a partial answer or a zero count, and one call budget is shared by the three buckets.
 *
 * <p>The monotonic clock the bounds read is injectable ({@code IndexRunCensusReads(repo, clock, nanos)}), so a spent budget
 * is deterministic. The reads of it, in order, are: the call deadline (1); each bucket's start (stale 2, fence 4, failed 8
 * when fence took its three statements) and, after it, one read per statement armed (stale 3; fence 5, 6, 7).
 */
class IndexRunCensusBudgetTest extends IndexRunCensusTestBase {

    private static final long MS = TimeUnit.MILLISECONDS.toNanos(1);

    /** A clock that reads 0 for its first {@code reads} reads and {@code afterNanos} from then on. */
    private static LongSupplier jumpsAfter(int reads, long afterNanos) {
        var count = new AtomicInteger();
        return () -> count.incrementAndGet() <= reads ? 0L : afterNanos;
    }

    // ── the budget itself ───────────────────────────────────────────────────

    @Test
    void aSpentBudgetIsNeverArmed_itThrowsTheCancelledError() {
        // start 0, bound 10 ms, clock at exactly 10 ms: nothing left. A 0 here would be a statement_timeout of 0 = no limit.
        var spent = new IndexRunCensusReads.Budget(0, Long.MAX_VALUE, 10, () -> 10 * MS);
        assertThatThrownBy(spent::leftMs).hasRootCauseInstanceOf(java.sql.SQLException.class)
            .satisfies(e -> assertThat(((java.sql.SQLException) e.getCause()).getSQLState()).isEqualTo(CatalogRepository.QUERY_CANCELED));
        assertThatThrownBy(new IndexRunCensusReads.Budget(0, Long.MAX_VALUE, 10, () -> 99 * MS)::leftMs)
            .isInstanceOf(org.jooq.exception.DataAccessException.class);
        // under a millisecond left reads as spent too (toMillis truncates), never as 0
        assertThatThrownBy(new IndexRunCensusReads.Budget(0, Long.MAX_VALUE, 10, () -> 10 * MS - 1)::leftMs)
            .isInstanceOf(org.jooq.exception.DataAccessException.class);
        // control: with time left it answers it
        assertThat(new IndexRunCensusReads.Budget(0, Long.MAX_VALUE, 10, () -> 4 * MS).leftMs()).isEqualTo(6);
    }

    @Test
    void aBucketBudgetIsTheSmallerOfItsBoundAndWhatIsLeftOfTheCall() {
        // the bucket starts at 20 s of a 25 s call: its 10 s bound is cut to the 5 s left
        long start = 20_000 * MS;
        assertThat(new IndexRunCensusReads.Budget(start, 25_000 * MS, 10_000, () -> start).leftMs()).isEqualTo(5_000);
        // and a call with plenty left leaves the bucket's own bound
        assertThat(new IndexRunCensusReads.Budget(start, 90_000 * MS, 10_000, () -> start).leftMs()).isEqualTo(10_000);
    }

    // ── one call budget for the three buckets ───────────────────────────────

    @Test
    void aSpentCallBudgetReportsEveryBucketTimedOutWithoutReadingAny() throws Exception {
        insert(TENANT, Doc.of("9.20.1").state("indexing").started(ago(8 * 3600)));
        System.setProperty(IndexRunCensusReads.CALL_BUDGET_PROPERTY, "0");
        try {
            var body = census("");
            for (String b : List.of("stale", "fence", "failed_hidden")) assertThat(bucket(body, b)).as(b).isEqualTo(Map.of("timed_out", true));
        } finally {
            System.clearProperty(IndexRunCensusReads.CALL_BUDGET_PROPERTY);
        }
        assertThat(bucket(census(""), "stale")).containsEntry("count", 1);
    }

    @Test
    void bucketsThatStartPastTheCallBudgetAreTimedOut_theBucketBeforeThemStillAnswers() throws Exception {
        insert(TENANT, Doc.of("9.21.1").state("indexing").started(ago(8 * 3600)));
        // stale takes reads 2 and 3 at time 0; fence starts at 30 s, past the 25 s call budget
        var census = new IndexRunCensusReads(repo, Clock.systemUTC(), jumpsAfter(3, 30_000 * MS)).read(TENANT, 10);
        assertThat(census.stale().timedOut()).isFalse();
        assertThat(census.stale().value().count()).isEqualTo(1);
        assertThat(census.fence().timedOut()).isTrue();
        assertThat(census.fence().value()).isNull();
        assertThat(census.failedHidden().timedOut()).isTrue();
    }

    // ── a later statement of a bucket ───────────────────────────────────────

    /** The fence's THIRD statement (the post-anchor read) runs out of budget: the whole bucket is timed_out, not a count of 0. */
    @Test
    void aTimeoutOnTheFencesLaterStatementIsTheBucketsTimedOut_neverAPartialFence() throws Exception {
        insert(TENANT, Doc.of("9.22.1").state("complete").indexed(at("2026-08-20T10:00:00Z")).run("rA"));
        insert(TENANT, Doc.of("9.22.2").state("complete").indexed(at("2026-08-20T10:00:05Z")).run("rA"));
        insert(TENANT, Doc.of("9.22.3").indexed(at("2026-09-01T00:00:00Z")));       // post-anchor: the third statement runs
        // control: the same data answers in full when the clock never moves
        var whole = new IndexRunCensusReads(repo, Clock.systemUTC(), jumpsAfter(Integer.MAX_VALUE, 0)).read(TENANT, 10);
        assertThat(whole.fence().timedOut()).isFalse();
        assertThat(whole.fence().value().postAnchor().count()).isEqualTo(1);
        // reads 1..6 at 0 (call, stale x2, fence start, fence statements 1 and 2); read 7 (the third statement) is 100 s on
        var census = new IndexRunCensusReads(repo, Clock.systemUTC(), jumpsAfter(6, 100_000 * MS)).read(TENANT, 10);
        assertThat(census.stale().timedOut()).isFalse();
        assertThat(census.fence().timedOut()).isTrue();
        assertThat(census.fence().value()).isNull();
    }

    /**
     * failed_hidden's chunk read (its LAST statement) is cancelled by PostgreSQL: another session holds ACCESS EXCLUSIVE on
     * the chunk table, which only that statement reads. The bucket is timed_out, never a count of 0, and the other two answer.
     */
    @Test
    void aTimeoutOnTheChunkReadIsTheBucketsTimedOut_neverAZeroCount() throws Exception {
        collection(TENANT, C1);
        insert(TENANT, Doc.of("9.23.1").state("failed").collection(C1).chunks(1).meta("{\"content_hash\":\"hq\"}"));
        chunk(TENANT, C1, "{\"content_hash\":\"hq\"}");
        insert(TENANT, Doc.of("9.23.2").state("indexing").started(ago(8 * 3600)));
        assertThat(bucket(census(""), "failed_hidden")).containsEntry("count", 1);    // control: it is there to count
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var body = census("");
            assertThat(bucket(body, "failed_hidden")).isEqualTo(Map.of("timed_out", true));
            assertThat(bucket(body, "stale")).containsEntry("count", 1);
            assertThat(bucket(body, "fence")).containsEntry("checked", 2);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
    }
}

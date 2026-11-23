// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename readiness the engine hands the one {@link dev.nexus.service.db.RenameGuard}
 * ({@link CompletionBootWork#renameReadiness()}): total, cheap and fail closed (linda 2026-11-23). It answers from the last sample, never reads the
 * database itself, never throws, and says {@code readiness_unreadable} where it cannot know; the last NONE stands across a failed read (its facts are
 * monotone); and it waits for P3.5c's id readers ({@code IdReaders.present()}). Hermetic: a scripted reader, no database. The decision whether a rename
 * consults it at all is the guard's, from the latch row it reads in the rename's own transaction.
 */
class CompletionBootWorkReadinessTest {

    private static CompletionCheck.Result result(CompletionCheck.RenameState rename) {
        return new CompletionCheck.Result(true, true, true, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, false, true, false, false, rename);
    }

    /** A reader that answers whatever {@code next} holds: a result, or a failure when it holds null. */
    private static final class Scripted implements CompletionBootWork.Reader {
        final AtomicReference<CompletionCheck.Result> next = new AtomicReference<>();
        final AtomicInteger reads = new AtomicInteger();

        @Override
        public CompletionCheck.Result read() {
            reads.incrementAndGet();
            var r = next.get();
            if (r == null) {
                throw new IllegalStateException("the database cannot answer");
            }
            return r;
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    private static CompletionBootWork work(Scripted reader) {
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, Clock.systemUTC(), Runnable::run);
    }

    /**
     * folio's round-3 critique: with no sample (the check cannot read) the readiness answers readiness_unreadable, never NONE; a read that throws
     * publishes nothing and leaves the answer as it was; and it costs no read of its own (it answers from the last sample).
     */
    @Test
    void theReadinessFailsClosedWhenTheCheckCannotBeRead_andNeverYieldsNoneFromAnError() {
        var reader = new Scripted();
        var work = work(reader);

        assertThat(work.publish()).as("an unreadable check publishes nothing").isEqualTo(-1);

        assertThat(work.latest()).isNull();
        assertThat(work.renameReadiness().missing()).as("no sample is not ready, and never NONE from an error")
            .isEqualTo(RenameReadiness.Missing.READINESS_UNREADABLE);
        int before = reader.reads.get();
        for (int i = 0; i < 5; i++) {
            work.renameReadiness().missing();
        }
        assertThat(reader.reads.get()).as("the readiness reads no database of its own: it answers from the last sample").isEqualTo(before);
    }

    /**
     * linda's census of 19d19081dc, item 1: the readiness waits for P3.5c's readers. Everything else complete and the id readers absent
     * ({@code IdReaders.present()} is false on this base) is {@code p35c_readers_absent}: in the readiness and in the status field. The test-only seam makes
     * them present and the same state reads NONE.
     */
    @Test
    void aReadyInstallWhoseReadersAreNotTheIdReadersIsP35cReadersAbsent_untilTheyArePresent() {
        var reader = new Scripted();
        reader.next.set(result(new CompletionCheck.RenameState(true, 1, List.of())));
        var work = work(reader);
        work.publish();

        assertThat(dev.nexus.service.vectors.IdReaders.present()).as("non-vacuity: this base's readers still select by name").isFalse();
        assertThat(work.renameReadiness().missing()).as("complete, but the id readers are absent: the default is IdReaders.present()")
            .isEqualTo(RenameReadiness.Missing.P35C_READERS_ABSENT);
        assertThat(work.statusJson()).as("the status field says so").contains("\"rename_missing\":\"p35c_readers_absent\"");

        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);

        assertThat(work.renameReadiness().missing()).as("with the readers present the same state is ready").isEqualTo(RenameReadiness.Missing.NONE);
        assertThat(work.statusJson()).contains("\"rename_missing\":\"none\"");
    }

    /** The facts behind NONE are monotone: a read that fails after a NONE does not take it back, and one that read something missing does. */
    @Test
    void theLastNoneStandsAcrossAFailedRead_butNotAcrossAReadThatSaysSomethingIsMissing() {
        var reader = new Scripted();
        var work = work(reader);
        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);
        reader.next.set(result(new CompletionCheck.RenameState(true, 1, List.of())));
        work.publish();
        assertThat(work.renameReadiness().missing()).as("non-vacuity: ready").isEqualTo(RenameReadiness.Missing.NONE);

        reader.next.set(result(CompletionCheck.RenameState.UNREADABLE));   // a sample whose own readiness read timed out
        work.publish();
        assertThat(work.renameReadiness().missing()).as("an unreadable sample after a NONE keeps answering NONE").isEqualTo(RenameReadiness.Missing.NONE);

        reader.next.set(result(new CompletionCheck.RenameState(false, 1, List.of())));   // a sample that read the drop move as incomplete
        work.publish();
        assertThat(work.renameReadiness().missing()).as("a read that says something is missing is believed")
            .isEqualTo(RenameReadiness.Missing.NAME_KEY_DROP_MOVE);
    }

    /**
     * linda's Q3 ruling on folio's r3 critique (F3), the first half: BEFORE any sample read NONE, a read error is never NONE. An unreadable sample (its own
     * readiness read failed or timed out) and an error while the readiness is computed (the catch arm) both answer readiness_unreadable.
     */
    @Test
    void beforeTheFirstReadySample_aReadErrorIsReadinessUnreadable_neverNone() {
        var reader = new Scripted();
        var work = work(reader);
        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);
        reader.next.set(result(CompletionCheck.RenameState.UNREADABLE));
        work.publish();
        assertThat(work.latest()).as("non-vacuity: a sample was published").isNotNull();
        assertThat(work.renameReadiness().missing()).as("an unreadable sample before any NONE").isEqualTo(RenameReadiness.Missing.READINESS_UNREADABLE);

        CompletionBootWorkTestSupport.idReadersPresent(work, () -> {
            throw new IllegalStateException("the readiness cannot be computed");
        });
        assertThat(work.renameReadiness().missing()).as("an error while the readiness is computed, before any NONE")
            .isEqualTo(RenameReadiness.Missing.READINESS_UNREADABLE);
    }

    /**
     * linda's Q3 ruling, the second half: AFTER a sample read NONE, a read error keeps NONE (the facts behind it are monotone), whether the error is an
     * unreadable sample or an error while the readiness is computed (the catch arm).
     */
    @Test
    void afterTheFirstReadySample_aReadErrorKeepsNone() {
        var reader = new Scripted();
        var work = work(reader);
        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);
        reader.next.set(result(new CompletionCheck.RenameState(true, 1, List.of())));
        work.publish();
        assertThat(work.renameReadiness().missing()).as("non-vacuity: ready").isEqualTo(RenameReadiness.Missing.NONE);

        reader.next.set(result(CompletionCheck.RenameState.UNREADABLE));
        work.publish();
        assertThat(work.renameReadiness().missing()).as("an unreadable sample after a NONE").isEqualTo(RenameReadiness.Missing.NONE);

        CompletionBootWorkTestSupport.idReadersPresent(work, () -> {
            throw new IllegalStateException("the readiness cannot be computed");
        });
        assertThat(work.renameReadiness().missing()).as("an error while the readiness is computed, after a NONE").isEqualTo(RenameReadiness.Missing.NONE);
    }
}

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

    /** {@link #result} with the latch as the sample reports it. */
    private static CompletionCheck.Result resultLatched(boolean latched, CompletionCheck.RenameState rename) {
        return new CompletionCheck.Result(true, true, true, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, false, latched, false, false, rename);
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
     * ({@code IdReaders.present()} false) is {@code p35c_readers_absent}: in the readiness and in the status field. P3.5c is in this base, so the default
     * is present and the same state reads NONE; the test-only seam makes the readers absent again.
     */
    @Test
    void aReadyInstallReadsNone_whenTheIdReadersArePresent_andP35cReadersAbsentWhenTheyAreNot() {
        var reader = new Scripted();
        reader.next.set(result(new CompletionCheck.RenameState(true, 1, List.of())));
        var work = work(reader);
        work.publish();

        assertThat(dev.nexus.service.vectors.IdReaders.present()).as("non-vacuity: this base carries P3.5c's id readers").isTrue();
        assertThat(work.renameReadiness().missing()).as("complete, and the default is IdReaders.present(): ready").isEqualTo(RenameReadiness.Missing.NONE);
        assertThat(work.statusJson()).as("the status field says so").contains("\"rename_missing\":\"none\"");

        CompletionBootWorkTestSupport.idReadersPresent(work, () -> false);

        assertThat(work.renameReadiness().missing()).as("with the readers absent the same state waits for them")
            .isEqualTo(RenameReadiness.Missing.P35C_READERS_ABSENT);
        assertThat(work.statusJson()).contains("\"rename_missing\":\"p35c_readers_absent\"");
    }

    /**
     * The rename reads one signal: the service's gate 3 signal (the flag the service's Gate3 reads) is set before the sample is published, and the readiness
     * answers NONE only while it is set, so a rename that takes the one-row path never meets helpers that still take the label forms. A sample that reads
     * NONE while the signal is not set (the signal lags: a hand-built result, a reordering) waits as for an unreadable readiness, whatever sample or failed
     * read follows, until a sample sets the signal.
     */
    @Test
    void theReadinessIsNoneOnlyWhileTheServicesGate3SignalIsSet() {
        var reader = new Scripted();
        var work = work(reader);
        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);
        var complete = new CompletionCheck.RenameState(true, 1, List.of());
        reader.next.set(resultLatched(false, complete));   // reads NONE, and says the latch has not been seen
        work.publish();

        assertThat(work.latest().readinessMissing()).as("non-vacuity: the sample itself reads NONE").isEqualTo(RenameReadiness.Missing.NONE);
        assertThat(work.gate3Latched()).as("non-vacuity: the service's signal is not set").isFalse();
        assertThat(work.renameReadiness().missing()).as("none is not answered while the signal is not set (no new wire value: unreadable)")
            .isEqualTo(RenameReadiness.Missing.READINESS_UNREADABLE);

        reader.next.set(null);   // a failed read after a NONE keeps NONE only once the signal is set
        work.publish();
        assertThat(work.renameReadiness().missing()).as("the monotone NONE does not outrun the signal either")
            .isEqualTo(RenameReadiness.Missing.READINESS_UNREADABLE);

        reader.next.set(resultLatched(true, complete));
        work.publish();
        assertThat(work.gate3Latched()).as("the sample set the signal").isTrue();
        assertThat(work.renameReadiness().missing()).as("with the signal set the same readiness is NONE").isEqualTo(RenameReadiness.Missing.NONE);
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

    /**
     * folio's r3 confirmation review S6: the status field {@code rename_missing} (what {@code nx doctor} shows) says what the rename path says. After a
     * sample read the readiness as NONE, an unreadable sample still reports {@code none}, because renames keep running on the monotone NONE; before any
     * NONE, an unreadable sample reports {@code readiness_unreadable}; a sample that reads something missing is believed.
     */
    @Test
    void theStatusFieldReportsTheReadinessTheRenamePathReads_monotoneAcrossAnUnreadableSample() {
        var reader = new Scripted();
        var work = work(reader);
        CompletionBootWorkTestSupport.idReadersPresent(work, () -> true);

        reader.next.set(result(CompletionCheck.RenameState.UNREADABLE));
        work.publish();
        assertThat(work.statusJson()).as("an unreadable sample before any NONE: the rename path refuses, and so does the field")
            .contains("\"rename_missing\":\"readiness_unreadable\"");

        reader.next.set(result(new CompletionCheck.RenameState(true, 1, List.of())));
        work.publish();
        assertThat(work.statusJson()).as("non-vacuity: ready").contains("\"rename_missing\":\"none\"");

        reader.next.set(result(CompletionCheck.RenameState.UNREADABLE));
        work.publish();
        assertThat(work.renameReadiness().missing()).as("the rename path still runs").isEqualTo(RenameReadiness.Missing.NONE);
        assertThat(work.statusJson()).as("so the status field says none, not refused").contains("\"rename_missing\":\"none\"");

        reader.next.set(result(new CompletionCheck.RenameState(false, 1, List.of())));
        work.publish();
        assertThat(work.statusJson()).as("a sample that reads something missing is believed, in the field as in the rename path")
            .contains("\"rename_missing\":\"name_key_drop_move\"");
    }
}

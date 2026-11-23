// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.datamove.CompletionBootWork;
import dev.nexus.service.datamove.Gate3Latch;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciReconciler.PassReport;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Clock;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): gate 3 over the id indexes: the name family retires where the id index is valid, gate 3's
 * PCI clause reads the database, the completion check sees it, the latch, the empty and killed-REINDEX cases and the coverage
 * read as a row-secured role. The fixtures are {@link PciIdIndexSupport}'s.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIdGate3IntegrationTest extends PciIdIndexSupport {

    // -- linda 2026-11-22: the name family retires at gate 3; gate 3's PCI clause reads the database --------------

    @Test
    void atGate3_theNameIndexesGoWhereTheIdIndexIsValid_andGate3StillReadsCovered() throws Exception {
        String tenant = newTenant("retire");
        long a = collection(tenant, name("ra"), 300);
        long b = collection(tenant, name("rb"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(validNameIndexed(tenant)).as("before gate 3 the name indexes stay").containsExactlyInAnyOrder(name("ra"), name("rb"));
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(a, b);
        assertThat(uncovered(r, tenant)).isEmpty();

        gate3 = true;
        PassReport pass = r.reconcileOnce();

        assertThat(pass.drops()).as("each name index, one DROP INDEX CONCURRENTLY after another").isEqualTo(2);
        assertThat(pass.builds()).as("and no name index is built back").isZero();
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(a, b);
        assertThat(uncovered(r, tenant)).as("dropping the name family does not flip gate 3").isEmpty();
        assertThat(coverage(r).orElseThrow().planned()).as("both collections are still required").hasSize(2);
        PassReport again = r.reconcileOnce();
        assertThat(again.drops()).isZero();
        assertThat(again.builds()).as("a further pass builds no name index").isZero();
    }

    @Test
    void aCollectionWhoseIdBuildWasKilled_keepsItsNameIndexAfterGate3_untilItsIdIndexIsValid() throws Exception {
        String tenant = newTenant("keep");
        long id = collection(tenant, name("keep"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        gate3 = true;
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> hasInvalidId(tenant), "the concurrent id build's invalid index");
            terminateBuilder();
            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(hasInvalidId(tenant)).isTrue();

        PassReport next = r.reconcileOnce();                // drops the invalid index and builds it again

        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(validNameIndexed(tenant)).as("the id index was not valid when this pass planned").containsExactly(name("keep"));
        assertThat(next.drops()).isEqualTo(1);

        PassReport last = r.reconcileOnce();                // now the id index is valid: the name index is replaced
        assertThat(last.drops()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactly(id);
    }

    @Test
    void aStandbyEnginesGate3Clause_agreesWithTheLockHolders() throws Exception {
        String tenant = newTenant("standby");
        collection(tenant, name("sb"), 300);
        PciSettings s = settings(16);
        PciReconciler holder = reconciler(s);
        holder.reconcileOnce();
        gate2 = true;
        holder.reconcileOnce();
        PciReconciler standby = reconciler(s);              // never ran a pass: it has no plan of its own

        assertThat(coverage(standby)).as("same database, same answer").isEqualTo(coverage(holder));
        assertThat(uncovered(standby, tenant)).isEmpty();
        // and where the holder's builds are incomplete, so are the standby's
        long other = collection(tenant, name("sb2"), 300);
        holder.reconcileOnce();                             // builds sb2's name index and its id index (gate 3 is off: no retirement)
        PciCatalog.Leaf current = leaf(tenant);
        PciCatalog.Index victim = current.idIndexes().stream().filter(i -> i.collectionId() == other).findFirst().orElseThrow();
        try (var pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s).open()) {
            assertThat(pass.drop(current, victim)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);   // the production drop path
        }
        assertThat(coverage(standby)).isEqualTo(coverage(holder));
        assertThat(uncovered(standby, tenant)).hasSize(1);
    }

    @Test
    void withPciOff_theClauseIsNotApplicable() throws Exception {
        String tenant = newTenant("off");
        collection(tenant, name("off"), 300);
        var off = new PciSettings(false, B, 600, 16, null, false);

        var coverage = coverage(reconciler(off));

        assertThat(coverage).isPresent();
        assertThat(coverage.get().notApplicable()).isTrue();
        assertThat(coverage.get().uncovered()).isEmpty();
    }

    @Test
    void withPciOnAndNoIndexAtAll_aQualifyingCollectionFailsTheClause_andAStoreOfSmallOnesPasses() throws Exception {
        String tenant = newTenant("empty");
        collection(tenant, name("small"), 50);
        PciReconciler r = reconciler(settings(16));

        var nothing = coverage(r).orElseThrow();
        assertThat(nothing.nothingToIndex()).as("no collection reaches B: nothing to build").isTrue();
        assertThat(nothing.uncovered()).isEmpty();

        collection(tenant, name("big"), 300);
        var qualifying = coverage(reconciler(settings(16))).orElseThrow();     // a fresh instance: no cached answer
        assertThat(qualifying.nothingToIndex()).isFalse();
        assertThat(qualifying.planned()).as("a store that should have an index and has none").isNotEmpty();
        assertThat(qualifying.uncovered()).isNotEmpty();
    }

    // -- S5: end to end through CompletionCheck.evaluate with the reconciler's real source ------------------------

    @Test
    void completionCheckSeesGate3True_withTheReconcilersRealSourceAndTheExtras_whenEverythingHolds() throws Exception {
        String tenant = newTenant("e2e");
        collection(tenant, name("e2e"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        CompletionCheck.ReadSet gates = new CompletionCheck.ReadSet(List.of(), List.of());

        try (Connection admin = adminDs.getConnection()) {
            admin.setReadOnly(true);
            DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
            var holds = CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> true));
            assertThat(holds.gate2()).as("non-vacuity: gate 2 holds").isTrue();
            assertThat(holds.gate3()).as("every id index valid, the extras hold: %s", holds.gate3Why()).isTrue();
            assertThat(holds.gate3PciNotApplicable()).isFalse();

            var extraFalse = CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> false));
            assertThat(extraFalse.gate3()).as("the id unique indexes are still required").isFalse();

            // The name family retires: gate 3 stays true after.
            gate3 = true;
            r.reconcileOnce();
            assertThat(validNameIndexed(tenant)).isEmpty();
            assertThat(CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> true)).gate3())
                .as("dropping the name index does not flip it").isTrue();
        }
    }

    // -- linda 2026-11-22: the retire's structural guard, and the gate 3 latch ---------------------------------

    @Test
    void aLatchedGate3WithTheReadersAbsent_dropsNoNameIndex_andWithThemPresentDropsThem() throws Exception {
        String tenant = newTenant("guard");
        long id = collection(tenant, name("guard"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        gate3 = true;                   // the latched gate 3
        readers = false;                // IdReaders.PRESENT is false or the class is absent

        PassReport without = r.reconcileOnce();

        assertThat(without.drops()).as("no name index is dropped without the readers").isZero();
        assertThat(without.builds()).as("and the name family is not retired, so nothing builds it differently").isZero();
        assertThat(validNameIndexed(tenant)).containsExactly(name("guard"));
        assertThat(validIdIndexed(tenant)).containsExactly(id);

        readers = true;
        PassReport with = r.reconcileOnce();

        assertThat(with.drops()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactly(id);
    }

    /**
     * A reporter that reads the coverage on every sample, latched or not (the throttle's period is zero), so a test sees the live state; the
     * production reporter repeats the read at most every {@code LATCHED_COVERAGE_PERIOD} once latched (see the throttle test).
     */
    CompletionBootWork reporter(PciReconciler r) {
        return reporter(r, CompletionCheck.Gate3LatchWriter.IMMEDIATE);
    }

    static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    boolean latchRow(String gate) throws Exception {
        return asSuperuser(su -> su.fetchExists(su.selectOne().from(LATCH).where(DSL.field(DSL.name("gate"), String.class).eq(gate))));
    }

    @Test
    void gate3Latches_andStaysLatchedWhileTheLiveGate3GoesFalseOnAnInvalidIdIndex_andANewReporterReadsItAgain() throws Exception {
        String tenant = newTenant("latch");
        collection(tenant, name("latchA"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());                 // a clean slate: another test may have latched
        var reporter = reporter(r);
        try {
            var held = reporter.refresh();
            assertThat(held.gate3()).as("every id index valid, the extras hold: %s", held.gate3Why()).isTrue();
            assertThat(held.gate3Latched()).as("the check latched it").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();
            assertThat(latchRow(CompletionCheck.GATE3)).as("the row is in the database").isTrue();

            // A second collection whose id build is killed: its id index is INVALID, so the live gate 3 goes false.
            gate2 = false;
            collection(tenant, name("latchB"), 300);
            r.reconcileOnce();                                            // B's name index
            gate2 = true;
            try (HeldWrite heldWrite = new HeldWrite(tenant)) {
                CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
                await(() -> hasInvalidId(tenant), "B's concurrent id build's invalid index");
                terminateBuilder();
                heldWrite.close();
                pass.get(60, TimeUnit.SECONDS);
            }
            var after = reporter.refresh();
            assertThat(after.gate3()).as("the live gate 3 is false").isFalse();
            assertThat(after.idIndexInvalid()).as("and says why, on its own").isTrue();
            assertThat(after.gate3Latched()).as("the latched value stays true").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();
            assertThat(reporter.statusJson()).contains("\"gate3_latched\":true").contains("\"id_index_invalid\":true").contains("\"gate3\":false");

            // A new engine over the same database reads the latch.
            var restarted = reporter(r);
            try {
                restarted.refresh();
                assertThat(restarted.gate3Latched()).as("a new reporter over the same database reads latched").isTrue();
            } finally {
                restarted.close();
            }

            // A latch row from before a restore: gone from the database, so a new reporter reads not latched. The old one is sticky.
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
            var restored = reporter(r);
            try {
                var read = restored.refresh();
                assertThat(read.gate3()).as("non-vacuity: gate 3 is still false, so nothing writes the row back").isFalse();
                assertThat(read.gate3Latched()).isFalse();
                assertThat(restored.gate3Latched()).as("a restore that lost the row reads not latched").isFalse();
                assertThat(reporter.gate3Latched()).as("an engine that already saw the row keeps it for its life").isTrue();
            } finally {
                restored.close();
            }
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    CompletionBootWork reporter(PciReconciler r, CompletionCheck.Gate3LatchWriter seam) {
        var admin = new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), admin, new CompletionCheck.ReadSet(List.of(), List.of()),
            r.gate3Source(), List.of(c -> true), seam, Gate3Latch.readAtBoot(admin), java.time.Duration.ZERO);
    }

    @Test
    void theLatchIsWrittenOnItsOwnSession_notTheReadOnlyOne_andThroughASeamThatCanFailOrWait() throws Exception {
        String tenant = newTenant("seam");
        collection(tenant, name("seam"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        try {
            // The check's own session is read only at the server: a write on it is refused (25006), so the row is never written there.
            try (Connection admin = adminDs.getConnection()) {
                DSLContext readOnly = DSL.using(admin, SQLDialect.POSTGRES);
                // as the check's own session is opened (CompletionBootWork.DbReader): default_transaction_read_only = on. The connection is
                // pooled, so the setting is put back whatever happens.
                readOnly.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("default_transaction_read_only"), DSL.val("on"),
                    DSL.inline(false))).fetch();
                try {
                    assertThat(readOnly.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("transaction_read_only")))
                        .fetchOne(0, String.class)).as("non-vacuity: this is a read-only session").isEqualTo("on");
                    org.assertj.core.api.Assertions.assertThatThrownBy(() -> CompletionCheck.insertLatch(readOnly, CompletionCheck.GATE3, "v", false))
                        .as("the latch INSERT cannot run on the read-only session").hasMessageContaining("read-only");
                } finally {
                    readOnly.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("default_transaction_read_only"), DSL.val("off"),
                        DSL.inline(false))).fetch();
                }
            }
            assertThat(latchRow(CompletionCheck.GATE3)).isFalse();

            // A writer that throws: gate 3 holds, nothing is latched (not the row, not the flag), and the failure does not escape the check.
            var failing = reporter(r, writeRow -> { throw new IllegalStateException("the rename lock was refused"); });
            try {
                var read = failing.refresh();
                assertThat(read.gate3()).as("non-vacuity: gate 3 holds: %s", read.gate3Why()).isTrue();
                assertThat(read.gate3Latched()).isFalse();
                assertThat(failing.gate3Latched()).isFalse();
                assertThat(latchRow(CompletionCheck.GATE3)).as("the row is absent").isFalse();
            } finally {
                failing.close();
            }

            // A writer that never runs the row write latches nothing either.
            var skipping = reporter(r, writeRow -> { });
            try {
                assertThat(skipping.refresh().gate3Latched()).isFalse();
                assertThat(latchRow(CompletionCheck.GATE3)).isFalse();
            } finally {
                skipping.close();
            }

            // A writer that waits (P3.7: for in-flight renames, under its lock) still latches once it returns, after the row is written.
            java.util.concurrent.atomic.AtomicBoolean rowWritten = new java.util.concurrent.atomic.AtomicBoolean();
            java.util.concurrent.atomic.AtomicReference<Boolean> flagWhenRowWritten = new java.util.concurrent.atomic.AtomicReference<>();
            var holder = new CompletionBootWork[1];
            holder[0] = reporter(r, writeRow -> {
                try {
                    Thread.sleep(300);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                writeRow.run();
                rowWritten.set(true);
                flagWhenRowWritten.set(holder[0].gate3Latched());   // the in-process flag is not set before the writer returns
            });
            try {
                long start = System.nanoTime();
                var read = holder[0].refresh();
                assertThat(System.nanoTime() - start).as("the read waited for the writer").isGreaterThanOrEqualTo(TimeUnit.MILLISECONDS.toNanos(250));
                assertThat(rowWritten).isTrue();
                assertThat(flagWhenRowWritten.get()).as("the flag is set only after the writer returns").isFalse();
                assertThat(read.gate3Latched()).isTrue();
                assertThat(holder[0].gate3Latched()).isTrue();
                assertThat(latchRow(CompletionCheck.GATE3)).isTrue();
            } finally {
                holder[0].close();
            }
        } finally {
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- N1: a name-indexed collection that counts zero still gets an id index, so gate 3 can hold -------------

    @Test
    void aLiveCollectionEmptiedOfItsRows_keepsItsNameIndex_getsAnIdIndexToo_andGate3Holds() throws Exception {
        String tenant = newTenant("empty-rows");
        long id = collection(tenant, name("empty-rows"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                    // the name index, at 300 rows
        assertThat(validNameIndexed(tenant)).containsExactly(name("empty-rows"));
        deleteRows(tenant, name("empty-rows"), 300);          // every row gone: the zero-count guard keeps the name index
        gate2 = true;

        r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).as("kept: a live collection that counts zero is a counting failure, never a drop")
            .containsExactly(name("empty-rows"));
        assertThat(validIdIndexed(tenant)).as("and it gets its id index, an empty partial index").containsExactly(id);
        assertThat(uncovered(r, tenant)).isEmpty();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        var reporter = reporter(r);
        try {
            var read = reporter.refresh();
            assertThat(read.gate3()).as("gate 3 reaches true: %s", read.gate3Why()).isTrue();
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- N2: a killed REINDEX of an id index leaves an INVALID leftover that the next pass drops --------------------

    boolean hasIdLeftover(String tenant) {
        return leaf(tenant).indexes().stream().anyMatch(i -> i.leftover() && i.leftoverOfId() != null);
    }

    @Test
    void aKilledReindexOfAnIdIndex_leavesAnInvalidLeftover_gate3StaysLatched_andTheNextPassDropsIt() throws Exception {
        String tenant = newTenant("idreindex");
        long id = collection(tenant, name("idreindex"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        var reporter = reporter(r);
        try {
            assertThat(reporter.refresh().gate3Latched()).as("non-vacuity: gate 3 latched").isTrue();
            // Retire the name family (latched + readers present), so the REINDEX candidate is the id index.
            gate3 = true;
            r.reconcileOnce();
            assertThat(validNameIndexed(tenant)).isEmpty();
            assertThat(validIdIndexed(tenant)).containsExactly(id);
            deleteRows(tenant, name("idreindex"), 80);           // deletes since build: the id index is a REINDEX candidate
            PciReconciler restarted = reconciler(reindexSettings());

            try (HeldWrite held = new HeldWrite(tenant)) {
                CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(restarted::reconcileOnce, async);
                await(() -> hasIdLeftover(tenant), "the REINDEX's INVALID _ccnew copy");
                terminateBuilder();
                held.close();
                pass.get(60, TimeUnit.SECONDS);
            }

            assertThat(hasIdLeftover(tenant)).as("the killed REINDEX left an INVALID id-predicate leftover").isTrue();
            assertThat(validIdIndexed(tenant)).as("the original stays valid").containsExactly(id);
            var after = reporter.refresh();
            assertThat(after.idIndexInvalid()).as("id_index_invalid sees the leftover").isTrue();
            assertThat(after.gate3Latched()).as("the latch outlives it").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();

            PassReport next = restarted.reconcileOnce();

            assertThat(hasIdLeftover(tenant)).as("the next pass dropped the leftover").isFalse();
            assertThat(next.drops()).isGreaterThanOrEqualTo(1);
            assertThat(validIdIndexed(tenant)).containsExactly(id);
            assertThat(reporter.refresh().idIndexInvalid()).isFalse();
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- M1 and M2: the coverage read as a row-secured role, and on an install with no leaf ---------------------------

    @Test
    void theCoverageReadAsNexusSvc_needsItsTenantBind_toSeeTheRegistry() throws Exception {
        String tenant = newTenant("svcread");
        collection(tenant, name("svcread"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();

        try (Connection svc = svcDs.getConnection()) {
            // nexus_svc is subject to the registry's row-level security: without the coverage read's own nexus.tenant bind it sees no registry row,
            // a leaf with a VALID name index and no visible row is unknown, and the read answers nothing.
            var coverage = r.gate3Source().read(DSL.using(svc, SQLDialect.POSTGRES));
            assertThat(coverage).as("non-vacuity: the read answers as the row-secured role").isPresent();
            assertThat(coverage.get().planned()).as("it saw the registry row").hasSize(1);
            assertThat(coverage.get().uncovered()).isEmpty();
            assertThat(DSL.using(svc, SQLDialect.POSTGRES).select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
                DSL.inline(true))).fetchOne(0, String.class)).as("the bind is cleared after the read").isIn(null, "");
        }
    }

    @Test
    void aPciOnInstallWithNoLeafAtAll_hasNothingRequired_butIsNotVerifiedEmpty_soTheRegistryDecides() throws Exception {
        var empty = new PciGate3Coverage(() -> new PciCatalog.Snapshot(List.of()), settings(16), Clock.systemUTC());
        String tenant = newTenant("noleaf");
        collection(tenant, name("noleaf"), 10);                // a live registry row exists, whatever the (empty) snapshot says
        try (Connection admin = adminDs.getConnection()) {
            DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
            var coverage = empty.read(ctx);
            assertThat(coverage).as("not unknown: no tenant leaf means nothing required").isPresent();
            assertThat(coverage.get().planned()).isEmpty();
            assertThat(coverage.get().nothingToIndex()).as("but not 'verified empty': nothing was probed").isFalse();
            assertThat(coverage.get().uncovered()).isEmpty();

            var read = CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()), empty, List.of());
            assertThat(read.gate2()).as("non-vacuity: gate 2 holds").isTrue();
            assertThat(read.gate3()).as("an empty plan over a populated registry does not pass").isFalse();
            assertThat(read.gate3Why()).contains("no collection requires an id-predicate index");
        }
    }

    // -- linda 2026-11-23: the required set, the latch read at construction, the PCI-off latch, the throttle ------------

    @Test
    void aCollectionThatCrossesTheThresholdWithNoIndex_holdsGate3ShutUntilItsIdIndexIsValid() throws Exception {
        String tenant = newTenant("cross");
        collection(tenant, name("cross-a"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(uncovered(r, tenant)).as("the indexed collection is covered").isEmpty();
        collection(tenant, name("cross-b"), 50);                // below B: nothing requires it
        assertThat(uncovered(r, tenant)).as("a collection below the threshold with no index is not required").isEmpty();

        long idB = collection(tenant, name("cross-b"), 300);    // 350 rows now: it has crossed B, and has no index of either kind

        var holding = coverage(r).orElseThrow();
        assertThat(holding.uncovered()).as("required before its index exists: gate 3 cannot latch ahead of it")
            .containsExactly(PciCatalog.idKey(M384, tenant, idB));
        assertThat(holding.nothingToIndex()).isFalse();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        var reporter = reporter(r);
        try {
            var read = reporter.refresh();
            assertThat(read.gate3()).as("gate 3 is false: %s", read.gate3Why()).isFalse();
            assertThat(read.gate3Latched()).isFalse();
            assertThat(latchRow(CompletionCheck.GATE3)).isFalse();

            r.reconcileOnce();                                  // the reconciler builds it

            assertThat(validIdIndexed(tenant)).contains(idB);
            assertThat(uncovered(r, tenant)).isEmpty();
            var after = reporter.refresh();
            assertThat(after.gate3()).as("and gate 3 holds once its id index is VALID: %s", after.gate3Why()).isTrue();
            assertThat(after.gate3Latched()).isTrue();
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    @Test
    void aFreshReporterOnADatabaseThatHoldsTheLatchRow_readsLatchedBeforeAnySample() throws Exception {
        String tenant = newTenant("boot");
        collection(tenant, name("boot"), 10);
        PciReconciler r = reconciler(settings(16));
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        asSuperuser(su -> { CompletionCheck.insertLatch(su, CompletionCheck.GATE3, "engine-0", true); return null; });
        try {
            var reporter = reporter(r);
            try {
                assertThat(reporter.latest()).as("non-vacuity: no sample has run").isNull();
                assertThat(reporter.gate3Latched()).as("known at construction, from the row").isTrue();
                assertThat(reporter.gate3LatchedPciOff()).isTrue();
            } finally {
                reporter.close();
            }
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
            var restored = reporter(r);
            try {
                assertThat(restored.gate3Latched()).as("no row: not latched, and that is an answer").isFalse();
                assertThat(restored.gate3LatchedPciOff()).isFalse();
            } finally {
                restored.close();
            }
        } finally {
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    @Test
    void aLatchThatSaysPciOff_makesTheReconcilerBuildTheIdIndexesWithoutWaitingForGate2() throws Exception {
        String tenant = newTenant("pcioff");
        long id = collection(tenant, name("pcioff"), 300);
        PciReconciler r = reconciler(settings(16));
        gate2 = false;
        pciOffLatch = false;
        r.reconcileOnce();
        r.reconcileOnce();
        assertThat(validNameIndexed(tenant)).containsExactly(name("pcioff"));
        assertThat(validIdIndexed(tenant)).as("gate 2 not held and no pci_off latch: no id build").isEmpty();

        pciOffLatch = true;                                     // the latch row says gate 3 latched while PCI was off
        r.reconcileOnce();

        assertThat(validIdIndexed(tenant)).as("PCI is on now: the id index is built first, as if gate 2 held").containsExactly(id);
        assertThat(validNameIndexed(tenant)).as("the name index stays: the retire still needs the latched gate 3 and the readers").containsExactly(name("pcioff"));
    }

    @Test
    void onceLatched_theCoverageReadIsThrottled_whileBeforeTheLatchEverySampleReads() throws Exception {
        var reads = new java.util.concurrent.atomic.AtomicInteger();
        CompletionCheck.Gate3Source covered = c -> {
            reads.incrementAndGet();
            return Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("k"), Set.of("k"), Set.of()));
        };
        CompletionCheck.Gate3Source waiting = c -> {
            reads.incrementAndGet();
            return Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("k"), Set.of(), Set.of()));
        };
        var admin = new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
        var gates = new CompletionCheck.ReadSet(List.of(), List.of());
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        try {
            var latched = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), admin, gates, covered, List.of(c -> true),
                CompletionCheck.Gate3LatchWriter.IMMEDIATE, Gate3Latch.of(true, false));
            try {
                latched.refresh();
                latched.refresh();
                latched.refresh();
                assertThat(reads.get()).as("latched at boot: one coverage read in the period, the rest reuse it").isEqualTo(1);
            } finally {
                latched.close();
            }
            reads.set(0);
            asSuperuser(su -> su.deleteFrom(LATCH).execute());      // the first reporter's gate 3 latched the row; the second starts not latched
            var open = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), admin, gates, waiting, List.of(c -> true),
                CompletionCheck.Gate3LatchWriter.IMMEDIATE, Gate3Latch.of(false, false));
            try {
                open.refresh();
                open.refresh();
                open.refresh();
                assertThat(reads.get()).as("not latched: every sample reads").isEqualTo(3);
            } finally {
                open.close();
            }
        } finally {
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }
}

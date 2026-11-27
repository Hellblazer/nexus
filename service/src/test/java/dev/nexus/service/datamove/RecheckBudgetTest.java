// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** nexus-p2m1x.21: what the re-check's shared budget charges, with no database and no clock. */
class RecheckBudgetTest {

    private static final long MS = 1_000_000L;

    @Test
    void aRunIsChargedItsExecuteTimeLessTheSmallestRoundTripSeen() {
        var budget = RecheckBudget.of(1_400);
        budget.observeRoundTrip(40 * MS);
        budget.observeRoundTrip(30 * MS);
        budget.observeRoundTrip(35 * MS);   // not the smallest: the floor stays 30 ms

        budget.chargeRun(50 * MS);

        assertThat(budget.floorNanos()).isEqualTo(30 * MS);
        assertThat(budget.spentNanos()).as("50 ms of execute less one 30 ms round trip").isEqualTo(20 * MS);
        assertThat(budget.leftMs()).isEqualTo(1_380);
    }

    @Test
    void withNoRoundTripSeen_nothingIsSubtracted() {
        var budget = RecheckBudget.of(1_400);

        budget.chargeRun(50 * MS);
        budget.chargeCatalogRead(70 * MS, 4);

        assertThat(budget.floorNanos()).isZero();
        assertThat(budget.spentNanos()).isEqualTo(120 * MS);
    }

    @Test
    void aRunFasterThanTheFloorIsChargedNothing_neverCredit() {
        var budget = RecheckBudget.of(1_400);
        budget.observeRoundTrip(30 * MS);

        budget.chargeRun(10 * MS);
        budget.chargeRun(0);
        budget.chargeCatalogRead(100 * MS, 4);   // 100 ms for four statements of a 30 ms round trip is under 4 x 30

        assertThat(budget.spentNanos()).isZero();
        assertThat(budget.leftMs()).isEqualTo(1_400);
    }

    @Test
    void theCatalogReadIsChargedLessOneRoundTripPerStatement() {
        var budget = RecheckBudget.of(1_400);
        budget.observeRoundTrip(30 * MS);

        budget.chargeCatalogRead(200 * MS, 4);

        assertThat(budget.spentNanos()).as("200 ms for four statements, less 4 x 30 ms").isEqualTo(80 * MS);
    }

    @Test
    void manyRunsAtTheFloor_spendNothing_whateverTheirNumber() {
        var budget = RecheckBudget.of(1_400);
        budget.observeRoundTrip(30 * MS);

        for (int i = 0; i < 10_000; i++) {
            budget.chargeRun(30 * MS);
        }

        assertThat(budget.spentNanos()).as("what the network took is not the database's time").isZero();
    }

    @Test
    void theBudgetIsSpentByDatabaseTime_andTheSettingIsWhatIsLeftAtLeastOneMillisecond() {
        var budget = RecheckBudget.of(1_000);
        budget.observeRoundTrip(1 * MS);

        budget.chargeRun(401 * MS);
        budget.chargeRun(401 * MS);
        assertThat(budget.leftMs()).isEqualTo(200);
        assertThat(budget.statementTimeoutSetting()).isEqualTo("200");

        budget.chargeRun(301 * MS);
        assertThat(budget.leftMs()).as("spent: negative").isNegative();
        assertThat(budget.statementTimeoutSetting()).as("never 0, which is no bound").isEqualTo("1");
    }

    @Test
    void anUnboundedBudgetIsNeverSpent_andSetsNoStatementTimeout() {
        var budget = RecheckBudget.unbounded();
        budget.observeRoundTrip(1 * MS);

        budget.chargeRun(Long.MAX_VALUE / 4);
        budget.chargeCatalogRead(Long.MAX_VALUE / 4, 4);

        assertThat(budget.leftMs()).isEqualTo(Long.MAX_VALUE);
        assertThat(budget.statementTimeoutSetting()).isEqualTo("0");
    }

    @Test
    void aVerificationDeclaresItReadsTheCatalogOnly_byTheMarkerAtTheStartOfItsText() {
        assertThat(move(DataMove.TABLE_WIDE_ONLY_MARKER + "\nSELECT 1 WHERE $1 IS NULL").tableWideOnly()).isTrue();
        assertThat(move("  \n" + DataMove.TABLE_WIDE_ONLY_MARKER + " SELECT 1").tableWideOnly()).as("white space first").isTrue();
        assertThat(move("SELECT 1 WHERE $1 IS NULL").tableWideOnly()).isFalse();
        assertThat(move("SELECT 1 " + DataMove.TABLE_WIDE_ONLY_MARKER).tableWideOnly()).as("only at the start").isFalse();
        assertThat(move("/* table-wide-only */ SELECT 1").tableWideOnly()).as("the exact text").isFalse();
    }

    private static DataMove move(String verification) {
        return new DataMove("m", "t", "s", "t", 1, 0, 1_400, verification, null, null, null, null, 0, null, null, 60_000);
    }
}

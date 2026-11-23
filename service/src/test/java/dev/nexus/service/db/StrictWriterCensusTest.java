// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.tools.jdbc.MockConnection;
import org.jooq.tools.jdbc.MockResult;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The engine-suite-wide census of the registry check (RDR-229 P3.5a, linda 6(a)) and its negative control: a write of a table filed by
 * collection in a transaction that read no registry row fails in {@code strict} and is kept in {@code record}; one that read a row, one
 * tagged exempt, one that wrote the registry itself, and a write of a table that is not filed by collection pass. The listener sees the
 * SQL a jOOQ query renders, so a mock connection is enough: no server, no substrate.
 */
class StrictWriterCensusTest {

    @AfterEach
    void off() {
        StrictWriterCensus.setModeForTests("off");
    }

    private static DSLContext txn() {
        DSLContext ctx = DSL.using(new MockConnection(c -> new MockResult[] {new MockResult(1)}), SQLDialect.POSTGRES);
        StrictWriterCensus.install(ctx);
        return ctx;
    }

    private static void writeChunk(DSLContext ctx) {
        ctx.update(CHUNKS).set(CHUNKS.COLLECTION, "x").where(CHUNKS.TENANT_ID.eq("t")).execute();
    }

    @Test
    void aWriteOfACollectionScopedTableWithNoRegistryRead_failsInStrictAndIsKeptInRecord() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext ctx = txn();
        assertThatThrownBy(() -> writeChunk(ctx)).isInstanceOf(RuntimeException.class)
            .hasStackTraceContaining("registry check missing").hasStackTraceContaining("chunks");
        assertThat(StrictWriterCensus.violations()).as("the finding is kept").hasSize(1);

        StrictWriterCensus.setModeForTests("record");
        DSLContext rec = txn();
        writeChunk(rec);
        assertThat(StrictWriterCensus.violations()).singleElement().asString().startsWith("chunks @ ");
    }

    @Test
    void aWriteAfterTheReadOfItsCollection_andAWriteInATaggedExemptTransaction_pass() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext read = txn();
        WriteRegistryMemo.put(read, "t", "x", row(7, "x"));   // the statement binds the label "x"
        writeChunk(read);

        DSLContext exempt = txn();
        WriteRegistryMemo.exempt(exempt, "a background writer");
        writeChunk(exempt);

        assertThat(StrictWriterCensus.violations()).isEmpty();
        assertThat(StrictWriterCensus.examined()).as("non-vacuity: the census looked at the two writes").isEqualTo(2);
    }

    @Test
    void aTransactionThatWroteTheRegistryRow_isNoLongerExemptForTheWritesAfterIt() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext registryWriter = txn();
        WriteRegistryMemo.forget(registryWriter);   // dropped the memo after its own UPDATE of the row: it holds nothing now
        assertThatThrownBy(() -> writeChunk(registryWriter)).hasStackTraceContaining("registry check missing");
    }

    @Test
    void aWriteThatBindsAnotherCollectionThanTheOneHeld_isRecordedButDoesNotThrow() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext ctx = txn();
        WriteRegistryMemo.put(ctx, "t", "held", row(9, "held"));
        writeChunk(ctx);   // binds "x" and "t"; the row held is "held"
        assertThat(StrictWriterCensus.violations()).singleElement().asString().contains("holds another collection");
    }

    @Test
    void aWriteOfATableNotFiledByCollection_isNotExamined() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext ctx = txn();
        ctx.update(MEMORY).set(MEMORY.CONTENT, "c").where(MEMORY.TENANT_ID.eq("t")).execute();
        assertThat(StrictWriterCensus.examined()).isZero();
        assertThat(StrictWriterCensus.violations()).isEmpty();
    }

    @Test
    void offByDefault_installsNothing() {
        StrictWriterCensus.setModeForTests("off");
        DSLContext ctx = txn();
        writeChunk(ctx);
        assertThat(StrictWriterCensus.examined()).isZero();
        assertThat(StrictWriterCensus.on()).isFalse();
    }

    private static CollectionRow row(long id, String label) {
        return new CollectionRow("code", "o", "voyage-code-3", 1024, "live", "", id, label);
    }

    @Test
    void anAdvisoryLockAfterARegistryRead_failsWhileTheCensusIsOn_andAReEntryDoesNot() {
        DSLContext ctx = txn();
        WriteRegistryMemo.assertNoneHeld(ctx, "a lock");   // census off by default: nothing held, nothing thrown
        boolean was = ExpectedCollectionIds.censusOn();
        ExpectedCollectionIds.setCensus(true);
        try {
            WriteRegistryMemo.assertNoneHeld(ctx, "the index-run lock of d");              // taken first: fine
            WriteRegistryMemo.put(ctx, "t", "x", row(7, "x"));
            WriteRegistryMemo.assertNoneHeld(ctx, "the index-run lock of d");              // taken again after the read: a re-entry
            assertThatThrownBy(() -> WriteRegistryMemo.assertNoneHeld(ctx, "the index-run lock of e"))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("advisory locks come first");
        } finally {
            ExpectedCollectionIds.setCensus(was);
        }
    }

    @Test
    void aSweepGateAfterARegistryRead_isRefused_evenForTheLabelARenameMovedTheWriteOnto_butAReEntryPasses() {
        DSLContext ctx = txn();
        boolean was = ExpectedCollectionIds.censusOn();
        ExpectedCollectionIds.setCensus(true);
        try {
            WriteRegistryMemo.assertGateFirst(ctx, "t", "old");                            // the gate of the boundary label, first
            WriteRegistryMemo.put(ctx, "t", "old", row(7, "new"));                         // the row read under it: now called "new"
            WriteRegistryMemo.put(ctx, "t", "new", row(7, "new"));
            WriteRegistryMemo.assertGateFirst(ctx, "t", "old");                            // re-entry: waits for nothing
            assertThatThrownBy(() -> WriteRegistryMemo.assertGateFirst(ctx, "t", "new"))   // the rename target: the write runs again with it first
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("advisory locks come first");
            assertThatThrownBy(() -> WriteRegistryMemo.assertGateFirst(ctx, "t", "other"))
                .isInstanceOf(IllegalStateException.class);
            DSLContext next = txn();                                                       // the next attempt, a new transaction: new label first
            WriteRegistryMemo.assertGateFirst(next, "t", "new");
            WriteRegistryMemo.put(next, "t", "old", row(7, "new"));
            WriteRegistryMemo.put(next, "t", "new", row(7, "new"));
            WriteRegistryMemo.assertGateFirst(next, "t", "new");
        } finally {
            ExpectedCollectionIds.setCensus(was);
        }
    }

    @Test
    void renameFollow_runsTheAttemptAgainWithTheRowsCurrentLabel_andGivesUpAfterTooManyRenames() {
        java.util.List<String> gates = new java.util.ArrayList<>();
        String result = RenameFollow.run("a", gate -> {
            gates.add(gate);
            if (gates.size() == 1) RenameFollow.check(gate, row(1, "b"));   // renamed to b while the attempt waited
            if (gates.size() == 2) RenameFollow.check(gate, row(1, "c"));   // and again
            return gate;
        });
        assertThat(result).isEqualTo("c");
        assertThat(gates).containsExactly("a", "b", "c");
        assertThatThrownBy(() -> RenameFollow.run("a", gate -> {
            RenameFollow.check(gate, row(1, gate + "x"));
            return gate;
        })).isInstanceOf(RenameBusyException.class);
    }
}

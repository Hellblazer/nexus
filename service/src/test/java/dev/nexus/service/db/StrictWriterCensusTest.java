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
    void aWriteAfterARegistryRead_aTaggedExemptTransaction_andARegistryWriter_pass() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext read = txn();
        WriteRegistryMemo.put(read, "t", "x", new CollectionRow("code", "o", "voyage-code-3", 1024, "live"));
        writeChunk(read);

        DSLContext exempt = txn();
        WriteRegistryMemo.exempt(exempt, "a background writer");
        writeChunk(exempt);

        DSLContext registryWriter = txn();
        WriteRegistryMemo.forget(registryWriter);
        writeChunk(registryWriter);

        assertThat(StrictWriterCensus.violations()).isEmpty();
        assertThat(StrictWriterCensus.examined()).as("non-vacuity: the census looked at the three writes").isEqualTo(3);
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
    void aSweepGateAfterARegistryRead_isRefused_unlessItIsTheGateOfTheLabelARenameMovedTheWriteOnto() {
        DSLContext ctx = txn();
        boolean was = ExpectedCollectionIds.censusOn();
        ExpectedCollectionIds.setCensus(true);
        try {
            WriteRegistryMemo.assertGateFirst(ctx, "t", "old");                            // the gate of the boundary label, first
            WriteRegistryMemo.put(ctx, "t", "old", row(7, "new"));                         // the row read under it: now called "new"
            WriteRegistryMemo.put(ctx, "t", "new", row(7, "new"));
            WriteRegistryMemo.assertGateFirst(ctx, "t", "old");                            // re-entry
            WriteRegistryMemo.assertGateFirst(ctx, "t", "new");                            // the rename target: the same row, same id
            WriteRegistryMemo.put(ctx, "t", "other", row(9, "other"));
            assertThatThrownBy(() -> WriteRegistryMemo.assertGateFirst(ctx, "t", "other"))  // a different collection's gate after the read
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("advisory locks come first");
            assertThatThrownBy(() -> WriteRegistryMemo.assertGateFirst(ctx, "t", "unread")) // a label no row of this transaction has
                .isInstanceOf(IllegalStateException.class);
        } finally {
            ExpectedCollectionIds.setCensus(was);
        }
    }
}

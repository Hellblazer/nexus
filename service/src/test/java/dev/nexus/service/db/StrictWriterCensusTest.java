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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
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

    /** The statement binds the tenant and a tumbler, which are Strings too: they name no collection, so a held row of another is no finding. */
    @Test
    void aStatementThatBindsNoCollectionColumn_isNotJudgedAgainstTheHeldRows() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext ctx = txn();
        WriteRegistryMemo.put(ctx, "t", "held", row(9, "held"));
        ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.2.3")).execute();
        assertThat(StrictWriterCensus.examined()).as("non-vacuity: the census looked at the write").isEqualTo(1);
        assertThat(StrictWriterCensus.violations()).as("a statement keyed by a tumbler names no collection").isEmpty();
    }

    /** The same table, bound to a collection label or id column: judged, and recorded when the row held is another's. */
    @Test
    void aStatementThatBindsACollectionLabelOrIdColumn_isStillJudgedAgainstTheHeldRows() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext byLabel = txn();
        WriteRegistryMemo.put(byLabel, "t", "held", row(9, "held"));
        byLabel.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq("elsewhere")).execute();
        DSLContext byId = txn();
        WriteRegistryMemo.put(byId, "t", "held", row(9, "held"));
        byId.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.eq(99L)).execute();
        DSLContext held = txn();
        WriteRegistryMemo.put(held, "t", "held", row(9, "held"));
        held.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq("held")).execute();
        assertThat(StrictWriterCensus.violations()).as("the label and the id of another collection are found, the held one is not").hasSize(2)
            .allSatisfy(v -> assertThat(v).contains("holds another collection"));
    }

    /** The rule itself, on what jOOQ renders: a bound collection column (compared, assigned or inserted) counts, a tenant or a tumbler does not. */
    @Test
    void bindsACollection_readsTheRenderedStatement() {
        DSLContext dsl = DSL.using(SQLDialect.POSTGRES);
        assertThat(StrictWriterCensus.bindsACollection(dsl.update(CHUNKS).set(CHUNKS.COLLECTION, "x").where(CHUNKS.TENANT_ID.eq("t")).getSQL()))
            .as("an assigned collection column").isTrue();
        assertThat(StrictWriterCensus.bindsACollection(dsl.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.in(1L, 2L)).getSQL()))
            .as("an id column in a list").isTrue();
        assertThat(StrictWriterCensus.bindsACollection(dsl.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION).values("t", "x").getSQL()))
            .as("an insert that lists a collection column").isTrue();
        assertThat(StrictWriterCensus.bindsACollection(dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 1)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.2")).getSQL()))
            .as("the tenant and a tumbler name no collection").isFalse();
    }

    /**
     * G3 (the row 14 census fix): a collection column bound to the empty string names no collection. {@code CollectionIds.idOf(Field, Field)}
     * renders its empty-name guard as {@code <name column> = ?} with {@code ''} bound, and the ledger writes' keep-the-stored-id coalesce
     * carries it, so such a statement was judged against the held rows and recorded as another collection's.
     */
    @Test
    void aCollectionColumnBoundToTheEmptyString_namesNoCollection_andIsNotJudgedAgainstTheHeldRows() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext compared = txn();
        WriteRegistryMemo.put(compared, "t", "held", row(9, "held"));
        compared.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq("")).execute();
        // the keep-the-stored-id coalesce of a re-embed job row's own-id stamp, as the repository writes it
        DSLContext ledger = txn();
        WriteRegistryMemo.put(ledger, "t", "held", row(9, "held"));
        ledger.update(REEMBED_JOBS)
            .set(REEMBED_JOBS.SOURCE_COLLECTION_ID, DSL.coalesce(REEMBED_JOBS.SOURCE_COLLECTION_ID,
                CollectionIds.idOf(REEMBED_JOBS.TENANT_ID, REEMBED_JOBS.SOURCE_COLLECTION)))
            .where(REEMBED_JOBS.TENANT_ID.eq("t")).and(REEMBED_JOBS.JOB_ID.eq(java.util.UUID.randomUUID())).execute();
        assertThat(StrictWriterCensus.examined()).as("non-vacuity: the census looked at both writes").isEqualTo(2);
        assertThat(StrictWriterCensus.violations()).as("an empty-string bind names no collection: nothing to judge").isEmpty();
    }

    /** The empty string is dropped only where it is the whole bind of the column: a real label or id beside it is still judged. */
    @Test
    void aCollectionColumnBoundToTheEmptyStringBesideARealCollection_isStillJudged() {
        StrictWriterCensus.setModeForTests("strict");
        DSLContext list = txn();
        WriteRegistryMemo.put(list, "t", "held", row(9, "held"));
        list.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.in("", "elsewhere")).execute();
        DSLContext both = txn();
        WriteRegistryMemo.put(both, "t", "held", row(9, "held"));
        both.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(""))
            .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.eq(99L)).execute();
        DSLContext held = txn();
        WriteRegistryMemo.put(held, "t", "held", row(9, "held"));
        held.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.in("", "held")).execute();
        assertThat(StrictWriterCensus.violations()).as("the list with another collection and the id of another are found; the held one is not")
            .hasSize(2).allSatisfy(v -> assertThat(v).contains("holds another collection"));
    }

    /** The rule on what jOOQ renders and binds. */
    @Test
    void bindsACollection_ofARenderedStatementWithItsBinds_skipsAColumnBoundToTheEmptyString() {
        DSLContext dsl = DSL.using(SQLDialect.POSTGRES);
        var empty = dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 1)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(""));
        assertThat(StrictWriterCensus.bindsACollection(empty.getSQL())).as("the text alone cannot tell: it binds a collection column").isTrue();
        assertThat(StrictWriterCensus.bindsACollection(empty.getSQL(), empty.getBindValues())).as("with its binds: ''").isFalse();
        var named = dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 1)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("t")).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq("x"));
        assertThat(StrictWriterCensus.bindsACollection(named.getSQL(), named.getBindValues())).as("a label").isTrue();
        var set = dsl.update(CHUNKS).set(CHUNKS.COLLECTION, "x").where(CHUNKS.TENANT_ID.eq("t"));
        assertThat(StrictWriterCensus.bindsACollection(set.getSQL(), set.getBindValues())).as("an assigned label").isTrue();
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

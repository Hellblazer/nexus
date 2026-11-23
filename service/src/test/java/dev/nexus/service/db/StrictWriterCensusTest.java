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

    @Test
    void anAdvisoryLockAfterARegistryRead_failsWhileTheCensusIsOn() {
        DSLContext ctx = txn();
        WriteRegistryMemo.assertNoneHeld(ctx, "a lock");   // census off by default: nothing held, nothing thrown
        WriteRegistryMemo.put(ctx, "t", "x", new CollectionRow("code", "o", "voyage-code-3", 1024, "live"));
        boolean was = ExpectedCollectionIds.censusOn();
        ExpectedCollectionIds.setCensus(true);
        try {
            assertThatThrownBy(() -> WriteRegistryMemo.assertNoneHeld(ctx, "the sweep gate of x"))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("advisory locks come first");
            WriteRegistryMemo.forget(ctx);
            WriteRegistryMemo.assertNoneHeld(ctx, "the sweep gate of x");   // nothing held now
        } finally {
            ExpectedCollectionIds.setCensus(was);
        }
    }
}

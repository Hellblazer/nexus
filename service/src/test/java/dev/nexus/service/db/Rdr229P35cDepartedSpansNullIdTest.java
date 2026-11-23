// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.datamove.Gate3;
import org.junit.jupiter.api.Test;

import java.util.List;

/**
 * RDR-229 P3.5c (linda 2026-11-23, census D2): from gate 3 a departed span with no collection_id is not resolved by its stored name. It is
 * counted unresolved (and logged at WARN), and nothing is cleared. The by-name arm stays before the latch.
 */
class Rdr229P35cDepartedSpansNullIdTest extends AtomicWriteTestBase {

    private static final Gate3 LATCHED = () -> true;

    private static DepartedWriterSpans.Gone span(String collection, Long id) {
        return new DepartedWriterSpans.Gone(collection, Chash.ofText("span " + collection).toBytes(), 1, 2, 3, 4, id);
    }

    @Test
    void aSpanWithNoCollectionIdIsCountedUnresolved_notResolvedByItsName_fromGate3() throws Exception {
        String name = "code__p35cnull" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(name);   // a registry row holds the label: the old code resolved the span to it
        long before = DepartedWriterSpans.unresolvedSpans();

        int cleared = tenantScope.withTenant(TENANT, ctx -> DepartedWriterSpans.clear(ctx, TENANT, List.of(span(name, null)), LATCHED));

        assertThat(cleared).as("nothing is cleared for a span with no id").isZero();
        assertThat(DepartedWriterSpans.unresolvedSpans()).as("counted, never kept silently: the label is not resolved").isEqualTo(before + 1);

        long beforeName = DepartedWriterSpans.unresolvedSpans();
        tenantScope.withTenant(TENANT, ctx -> DepartedWriterSpans.clear(ctx, TENANT, List.of(span(name, null)), Gate3.never()));
        assertThat(DepartedWriterSpans.unresolvedSpans()).as("before the latch the name is the key: not unresolved").isEqualTo(beforeName);
    }
}

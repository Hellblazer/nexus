// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.LegacyChunkSurvey;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.HexFormat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the legacy-width chunk survey reads a collection by its id once gate 3 has latched. The registry
 * row of the collection is renamed in place (FK triggers off: the state P3.7's one-row rename leaves, children keep the old name and
 * the id); before the latch the owner lookup and the report find the chunk under the old name, after it under the registry's
 * current label, and a chunk is reported under that label.
 */
class Rdr229P35cLegacySurveyByIdTest extends LegacyChashFixture {

    private static final Gate3 LATCHED = () -> true;

    private String renameRegistryRow(String tenant, String old, String neu) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        }
        return neu;
    }

    @Test
    void legacyChunkSurvey_readsTheIdFromGate3() throws Exception {
        byte[] held = HexFormat.of().parseHex("ef".repeat(16));
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, held);
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, held);
        });
        String neu = renameRegistryRow(TENANT_A, COLLECTION, "knowledge__legacy-renamed__" + MODEL + "__v1");
        var scope = superuserScope();

        // before the latch: the name path
        var byName = LegacyChunkSurvey.forTenant(scope, TENANT_A, 1000, 20, 5000, 60_000L);
        assertThat(byName.chunks()).hasSize(1);
        assertThat(byName.chunks().get(0).collection()).as("the chunk's own name").isEqualTo(COLLECTION);
        assertThat(byName.chunks().get(0).owners().owners()).extracting(LegacyChunkSurvey.Owner::docId).containsExactly("1.1");
        assertThat(byName.manifestCounts().byReason()).containsEntry("no_text", 1L);

        // gate 3 latched: the registry's current label, found and owned by the id
        var byId = LegacyChunkSurvey.forTenant(scope, TENANT_A, 1000, 20, 5000, 60_000L, LATCHED);
        assertThat(byId.chunkCounts().byReason()).containsEntry("no_text", 1L);
        assertThat(byId.chunks()).hasSize(1);
        assertThat(byId.chunks().get(0).collection()).as("the registry's current label of the chunk's id").isEqualTo(neu);
        assertThat(byId.chunks().get(0).owners().owners()).as("the manifest names the chunk by the same id")
            .extracting(LegacyChunkSurvey.Owner::docId).containsExactly("1.1");
        assertThat(byId.manifestCounts().byReason()).as("the manifest row meets its chunk on the id").containsEntry("no_text", 1L);
        assertThat(byId.manifestDocs()).containsKey("no_text");

        // the owner lookup resolves the label it is given: the old name names nothing after the latch, the new one names the chunk
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, neu, MODEL, held, 5, c -> true, LATCHED).owners()).hasSize(1);
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, COLLECTION, MODEL, held, 5, c -> true, LATCHED).owners()).isEmpty();
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, COLLECTION, MODEL, held, 5, c -> true).owners()).hasSize(1);
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, "", MODEL, held, 5, c -> true, LATCHED).owners())
                .as("a '' label is no collection").isEmpty();
        }
    }
}

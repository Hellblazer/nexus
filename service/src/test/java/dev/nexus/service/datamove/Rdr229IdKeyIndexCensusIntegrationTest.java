// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.vectors.Rdr229SwapMove;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22, linda's ruling 2026-11-22): every id key has a supporting index ({@link IdKeyIndexCensus}). Run after
 * P3.3c's swaps, so the census sees all three families: P3.3a's twins to the registry (vectors-072, indexed by IdUniqueIndexPass: its plain supporting indexes, or an arbiter whose leading columns cover the key), P3.3c's
 * three child keys into the chunks id key and its composite from chunks to the registry.
 *
 * <p>Why a key into chunks passes on (tenant_id, chash) or (tenant_id, doc_id) and not (tenant_id, &lt;id column&gt;): chash is
 * near-unique within a tenant (doc_id on topic_assignments holds the chash), so the child-side lookup a chunk delete or update makes
 * through that index reads a handful of rows; bob's EXPLAINs in Rdr229SwapMovesAfterTheSwapIntegrationTest show each of the three
 * lookups using its index (idx_catalog_chunks_chash, idx_chunk_orphaned_at_tenant_chash, idx_ta_tenant_doc_id).
 */
class Rdr229IdKeyIndexCensusIntegrationTest extends Rdr229SwapMovesFixtures {

    /** P3.3c's three child keys into the chunks id key (Rdr229SwapMove's CHILDREN) and the composite. */
    static final List<String> P33C_KEYS = List.of("fk_catalog_chunks_chunk_id", "chunk_orphaned_at_chunk_id_fk",
        "topic_assignments_chunk_id_fk", Rdr229SwapMove.COMPOSITE_CONSTRAINT);

    @Test
    void everyIdKeyHasAValidIndexLedByTwoOfItsColumnsOneOfThemTenantId() throws Exception {
        seed("census");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the chunks swap completed, so P3.3c's keys exist").isNotNull();
        // the supporting indexes are runtime builds (IdUniqueIndexPass; linda 2026-11-22: out of the boot walk), arbiters included
        new IdUniqueIndexPass(adminConnection, new dev.nexus.service.BootWorkRegistry(java.time.Clock.systemUTC()), () -> true).runOnce();

        List<String> expected = new ArrayList<>(P33C_KEYS);
        for (FkTwins.Row row : FkTwins.load()) {
            if (ctx().fetchOne("SELECT to_regclass(?)::text", "nexus." + row.table()).get(0) != null) {
                expected.add(row.twin());
            }
        }
        var keys = IdKeyIndexCensus.keys(ctx());
        assertThat(keys).extracting(IdKeyIndexCensus.Key::constraint)
            .as("non-vacuity and precision: the census finds exactly the id keys P3.3a and P3.3c add")
            .containsExactlyInAnyOrderElementsOf(expected);
        assertThat(expected).as("at least P3.3a's six always-present twins and P3.3c's four keys").hasSizeGreaterThanOrEqualTo(10);
        assertThat(IdKeyIndexCensus.unsupported(ctx())).as("every id key has a supporting index").isEmpty();
    }

    /**
     * The planted failure: a key whose only index leads with a column outside the key is unsupported, and so is one whose index is
     * (tenant_id, id) but partial; then an index on (id column, tenant_id), in either order, supports it.
     */
    @Test
    void aKeyWhoseIndexesDoNotLeadWithTwoOfItsColumnsIsUnsupported() {
        String t = "nexus.p34_census_plant";
        try {
            ctx().execute("CREATE TABLE " + t + " (tenant_id text NOT NULL, collection_id bigint, made_at timestamptz,"
                + " CONSTRAINT p34_census_plant_collection_id_fk FOREIGN KEY (tenant_id, collection_id)"
                + " REFERENCES nexus.catalog_collections (tenant_id, collection_id))");
            ctx().execute("CREATE INDEX p34_census_plant_made_at ON " + t + " (made_at, tenant_id, collection_id)");
            ctx().execute("CREATE INDEX p34_census_plant_partial ON " + t + " (tenant_id, collection_id) WHERE made_at IS NOT NULL");
            ctx().execute("CREATE INDEX p34_census_plant_tenant ON " + t + " (tenant_id)");
            assertThat(IdKeyIndexCensus.unsupported(ctx())).extracting(IdKeyIndexCensus.Key::constraint)
                .as("led by a column outside the key, partial, or one column only: none supports the key")
                .contains("p34_census_plant_collection_id_fk");

            ctx().execute("CREATE INDEX p34_census_plant_id ON " + t + " (collection_id, tenant_id)");
            assertThat(IdKeyIndexCensus.keys(ctx())).filteredOn(k -> k.constraint().equals("p34_census_plant_collection_id_fk"))
                .singleElement().extracting(IdKeyIndexCensus.Key::supported).as("(id column, tenant_id) supports it").isEqualTo(true);
        } finally {
            ctx().execute("DROP TABLE IF EXISTS " + t);
        }
    }
}

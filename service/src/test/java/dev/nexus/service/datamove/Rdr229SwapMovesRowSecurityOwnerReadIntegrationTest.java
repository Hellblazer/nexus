// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229ChunksFk;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

/**
 * The row-security cases on the schema as the changelog builds it: the driving tables (nexus.chunks and the three children) carry a permissive
 * owner-read SELECT policy for the migration role (RDR-246), so the finisher's session, which names no tenant, reads every tenant's rows of them
 * and the planted orphans are visible to it. Each foreign-key gate must refuse its orphan. See {@link Rdr229SwapMovesRowSecurityBase}.
 */
class Rdr229SwapMovesRowSecurityOwnerReadIntegrationTest extends Rdr229SwapMovesRowSecurityBase {

    /**
     * A chunk whose (tenant_id, collection_id, embedding_model) has no registry row, under a tenant the session does not name. The swap no
     * longer adds the composite (nexus-p2m1x.21): it completes, and the orphan chunk's LEAF validation move runs {@code VALIDATE CONSTRAINT} of
     * the leaf's key as the finisher's session (NOSUPERUSER, NOBYPASSRLS owner, FORCE RLS). It must refuse with 23503, a permanent failure of
     * that move, before any claim, and the composite must never appear (the adoption move fails loud naming the failed leaf move).
     */
    @Test
    void aChunkWithNoRegistryRowIsRefusedByItsLeafValidationAsTheFinisherSessionRunsIt() throws Exception {
        seed("rls-chunk");
        assertTheSessionIsTheProductionShape();
        String tenant = "p33c-rls-orphan-chunk";
        tenantsUsed.add(tenant);
        plantOrphanChunk(tenant);
        assertThat(visibleToTheFinisher("chunks", tenant)).as("the owner-read policy shows the finisher the orphan chunk").isPositive();
        registerSwaps();

        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(LeafIndexPass.swapLabel("chunks"))).as("the swap adds no composite, so over an orphan chunk it completes").isNotNull();
        var w = productionWorker(new VerifyingFinisher());
        for (int i = 0; i < 4; i++) {
            w.runCycle();
        }

        var refused = leafMoves().stream().filter(m -> m.get("failed_at") != null).toList();
        assertThat(refused).as("the orphan chunk's leaf validation was refused").isNotEmpty();
        for (var move : refused) {
            assertThat(move.get("completed_at")).as(move.get("label") + " did not complete over an orphan chunk").isNull();
            assertThat((String) move.get("failure")).as("by the key's own validation, as the finisher's role runs it").contains("sqlstate=23503");
            String leaf = ((String) move.get("label")).substring(Rdr229ChunksFk.LEAF_LABEL_PREFIX.length());
            assertThat(leafFks().stream().filter(f -> f.leaf().equals(leaf)).toList()).as(leaf + ": its key is still NOT VALID").isNotEmpty()
                .allSatisfy(f -> assertThat(f.validated()).isFalse());
        }
        assertThat(compositeOnId()).as("the composite never appears over an orphan chunk").isEmpty();
        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("completed_at")).as("the adoption move did not complete").isNull();
        assertThat((String) adopt.get("failure")).as("it fails loud, naming the failed leaf move").contains(Rdr229ChunksFk.LEAF_LABEL_PREFIX);
    }

    /**
     * For each child table, a row whose (tenant_id, collection_id, chash, embedding_model) has no chunk: its name key finds its chunk, its id
     * is another registered collection's. It is planted before the swap (the id key does not exist yet, so nothing refuses it), under the
     * child's own tenant. The swap completes and registers the validation moves; each move's VALIDATE CONSTRAINT must refuse with 23503, and the
     * key stays NOT VALID.
     */
    @Test
    void aChildRowWithNoChunkUnderItsIdKeyIsRefusedByItsValidationMove() throws Exception {
        var t = seed("rls-child");
        assertTheSessionIsTheProductionShape();
        for (String child : CHILD_ID_COLUMN.keySet()) {
            plantChildOrphan(child, t.of(child));
            assertThat(visibleToTheFinisher(child, t.of(child))).as("the owner-read policy shows the finisher " + child).isGreaterThanOrEqualTo(2);
        }
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(LeafIndexPass.swapLabel("chunks"))).as("precondition: the swap completed (no orphan chunk here)").isNotNull();

        runValidationMoves();

        for (String child : CHILD_ID_COLUMN.keySet()) {
            String label = Rdr229SwapMove.validationLabel(child);
            var move = moves(LABEL_F.eq(label)).get(0);
            assertThat(move.get("completed_at")).as(label + " did not complete over an orphan row").isNull();
            assertThat(move.get("failed_at")).as(label + ": refused").isNotNull();
            assertThat((String) move.get("failure")).as(label + ": by the key's own validation").contains("sqlstate=23503");
            assertThat(validated(child)).as(child + ": the id key is still NOT VALID").isFalse();
        }
    }
}

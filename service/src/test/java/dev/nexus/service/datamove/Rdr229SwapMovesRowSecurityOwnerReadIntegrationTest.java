// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.LeafIndexPass;
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
     * A chunk whose (tenant_id, collection_id, embedding_model) has no registry row, under a tenant the session does not name. The swap's
     * composite is added and validated in its on_complete (the referencing table is partitioned, so it is not NOT VALID): it must refuse with
     * 23503, a permanent failure, and the transaction rolls back whole.
     */
    @Test
    void aChunkWithNoRegistryRowIsRefusedByTheSwapsCompositeAsTheFinisherSessionRunsIt() throws Exception {
        seed("rls-chunk");
        assertTheSessionIsTheProductionShape();
        String tenant = "p33c-rls-orphan-chunk";
        tenantsUsed.add(tenant);
        plantOrphanChunk(tenant);
        assertThat(visibleToTheFinisher("chunks", tenant)).as("the owner-read policy shows the finisher the orphan chunk").isPositive();
        registerSwaps();

        runSwaps(new VerifyingFinisher());

        var chunks = moves(LABEL_F.eq(LeafIndexPass.swapLabel("chunks"))).get(0);
        assertThat(chunks.get("completed_at")).as("the swap did not complete over an orphan chunk").isNull();
        assertThat(chunks.get("failed_at")).as("a permanent refusal").isNotNull();
        assertThat((String) chunks.get("failure")).as("by the composite's own check").contains("sqlstate=23503");
        assertThat(compositeOnId()).as("rolled back: no composite").isEmpty();
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("rolled back: no id twin on " + child).isEmpty();
        }
        assertThat(registeredBySwap()).as("and no validation move").isEmpty();
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

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
 * The row-security cases with the owner-read policies of the driving tables dropped, so only {@code tenant_isolation} is left and the finisher's
 * session, which names no tenant, sees none of the planted rows. A move over such a table never reaches its foreign-key step: the worker's
 * preflight refuses a driving table the admin session does not read table-wide ({@code admin_cannot_see_all_tenants}, AdminDdlSession), so the
 * move is blocked, transient, and nothing is added or validated. The gate is not vacuous: an orphan row hidden by row security cannot be passed
 * over, because the move does not run. See {@link Rdr229SwapMovesRowSecurityBase}.
 *
 * <p>Order matters: the children's policies go first (the chunks swap still runs and completes), then chunks'.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229SwapMovesTenantIsolationOnlyIntegrationTest extends Rdr229SwapMovesRowSecurityBase {

    private static final String BLOCKED = "does not read every tenant's rows";

    private void dropOwnerRead(List<String> tables) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            for (String t : tables) {
                PgContainerHelper.dropPolicyIfExists(c, DSL.table(DSL.name("nexus", t)), t + "_owner_read");
            }
        }
    }

    private void dropChunksOwnerRead() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            var d = DSL.using(c, SQLDialect.POSTGRES);
            var relations = new ArrayList<String>(List.of("chunks"));
            IdKeyIndexProbe.modelPartitions(d, IdKeyIndexProbe.CHUNKS).forEach(r -> relations.add(bare(r)));
            IdKeyIndexProbe.leaves(d, IdKeyIndexProbe.CHUNKS).forEach(r -> relations.add(bare(r)));
            for (String r : relations) {
                PgContainerHelper.dropPolicyIfExists(c, DSL.table(DSL.name("nexus", r)), "chunks_gate_probe_owner_read");
            }
        }
    }

    /** A relation as {@code regclass} prints it, without the schema and quotes. */
    private static String bare(String regclass) {
        String n = regclass.startsWith("nexus.") ? regclass.substring("nexus.".length()) : regclass;
        return n.startsWith("\"") && n.endsWith("\"") ? n.substring(1, n.length() - 1).replace("\"\"", "\"") : n;
    }

    @Test
    @Order(1)
    void childValidationMovesOverRowsHiddenFromTheSessionAreBlockedAtPreflightNeverPassed() throws Exception {
        var t = seed("tio-child");
        assertTheSessionIsTheProductionShape();
        dropOwnerRead(List.copyOf(CHILD_ID_COLUMN.keySet()));
        for (String child : CHILD_ID_COLUMN.keySet()) {
            plantChildOrphan(child, t.of(child));
            assertThat(visibleToTheFinisher(child, t.of(child))).as("non-vacuity: only tenant_isolation is left, so " + child + " is hidden from the session").isZero();
        }
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(LeafIndexPass.swapLabel("chunks"))).as("precondition: the chunks swap still runs (chunks keeps its policy) and completes").isNotNull();

        runValidationMoves();

        for (String child : CHILD_ID_COLUMN.keySet()) {
            String label = Rdr229SwapMove.validationLabel(child);
            var move = moves(LABEL_F.eq(label)).get(0);
            assertThat(move.get("completed_at")).as(label + " did not complete over rows it cannot see").isNull();
            assertThat(move.get("failed_at")).as(label + " is blocked, transient, not failed").isNull();
            assertThat((String) move.get("last_transient")).as(label + ": blocked at preflight").contains(BLOCKED);
            assertThat(validated(child)).as(child + ": the id key was never validated").isFalse();
        }
    }

    @Test
    @Order(2)
    void theChunksSwapOverRowsHiddenFromTheSessionIsBlockedAtPreflightNeverPassed() throws Exception {
        seed("tio-chunk");
        assertTheSessionIsTheProductionShape();
        dropChunksOwnerRead();
        String tenant = "p33c-tio-orphan-chunk";
        tenantsUsed.add(tenant);
        plantOrphanChunk(tenant);
        assertThat(visibleToTheFinisher("chunks", tenant)).as("non-vacuity: only tenant_isolation is left, so the orphan chunk is hidden").isZero();
        registerSwaps();

        runSwaps(new VerifyingFinisher());

        var chunks = moves(LABEL_F.eq(LeafIndexPass.swapLabel("chunks"))).get(0);
        assertThat(chunks.get("completed_at")).as("the swap did not complete over rows it cannot see").isNull();
        assertThat(chunks.get("failed_at")).as("blocked, transient, not failed").isNull();
        assertThat((String) chunks.get("last_transient")).as("blocked at preflight").contains(BLOCKED);
        assertThat(compositeOnId()).as("no composite").isEmpty();
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("no id twin on " + child).isEmpty();
        }
    }
}

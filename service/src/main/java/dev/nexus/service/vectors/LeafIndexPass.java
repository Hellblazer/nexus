// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import org.jooq.DSLContext;

import java.util.List;
import java.util.Objects;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): the per-leaf unique-index pass that registers the swap moves. THIS IS A STUB: only the
 * names the failing tests compile against, and a {@link #runOnce} that does nothing. The implementation lands with the
 * bead's green commit.
 *
 * <p>What the pass is to do (the bead's design; RDR-229 item 6, "Index builds follow the PCI reconciler's pattern"):
 * <ul>
 *   <li>Start only when gate 1 of the boot-time completion check holds ({@link Gate1}): every stamp move and every P3.3a
 *       move is complete. On an install whose moves are blocked (no pg_background) it never holds, so the pass builds no
 *       index and registers no swap (residual S-5, nexus-p2m1x.43).</li>
 *   <li>Run whether or not {@code NX_SEARCH_PCI} is on, on a schedule of its own, outside the per-leaf cap and not on the
 *       PCI reconciler's lock holder (residual S-3, nexus-p2m1x.41). It takes no {@code PciSettings}.</li>
 *   <li>For {@code nexus.chunks} (key {@code (tenant_id, collection_id, chash, embedding_model)}) and
 *       {@code nexus.taxonomy_centroids} (key {@code (tenant_id, collection_id, topic_id, embedding_model)}), build the
 *       unique index by the two-level route of S-3: {@code ALTER TABLE ONLY} the parent {@code ADD CONSTRAINT UNIQUE};
 *       the same on each model partition; per leaf {@code CREATE UNIQUE INDEX CONCURRENTLY}, {@code ADD CONSTRAINT ...
 *       UNIQUE USING INDEX}, {@code ALTER INDEX <model key> ATTACH PARTITION <leaf key>}; per model partition
 *       {@code ALTER INDEX <parent key> ATTACH PARTITION <model key>}. A leaf's INVALID leftover is dropped and rebuilt.</li>
 *   <li>Once every leaf of a table has a valid index and no {@code nexus.data_moves} row carries that table's swap label
 *       ({@link #swapLabel}), insert the swap move's row on the admin connection, with the changeset_id
 *       {@link #CHANGESET_PREFIX}{@code <engine version>} (S-3 point 3, rule 2: an engine pass with no on_complete text).</li>
 *   <li>Report in {@code boot_work} under {@link #BOOT_WORK_ID}, an entry of its own.</li>
 * </ul>
 */
public final class LeafIndexPass {

    /** The pass's own {@code boot_work} entry; not the reconciler's {@code pci-reconcile}. */
    public static final String BOOT_WORK_ID = "leaf-index-pass";

    /** The registering changeset_id of a swap move is this prefix and the engine version (S-3 point 3, rule 2). */
    public static final String CHANGESET_PREFIX = "engine:p33b-unique-index:";

    /** Gate 1 of the completion check, read on the admin connection; production wires {@code CompletionCheck.evaluate(ctx).gate1()}. */
    @FunctionalInterface
    public interface Gate1 {
        boolean holds(DSLContext admin);
    }

    /**
     * What one pass did. {@code gateHeld} is false when gate 1 did not hold (and then the rest is zero or empty);
     * {@code registered} lists the swap labels this pass inserted.
     */
    public record Result(boolean gateHeld, int built, int dropped, List<String> registered) { }

    private final AdminConnection admin;
    private final BootWorkRegistry registry;
    private final Gate1 gate1;
    private final String engineVersion;

    public LeafIndexPass(AdminConnection admin, BootWorkRegistry registry, Gate1 gate1, String engineVersion) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.registry = Objects.requireNonNull(registry, "registry");
        this.gate1 = Objects.requireNonNull(gate1, "gate1");
        this.engineVersion = Objects.requireNonNull(engineVersion, "engineVersion");
    }

    /** The label of {@code table}'s swap move ({@code chunks} or {@code taxonomy_centroids}): P3.3c's move, registered by this pass. */
    public static String swapLabel(String table) {
        return "rdr229-swap-" + table;
    }

    /** The name of a leaf's id unique index, so a leftover INVALID one is found by name and dropped. A leaf name is at most 35 characters. */
    public static String leafIndexName(String leaf) {
        return leaf + "_cid_uq";
    }

    /** One pass, now: the scheduler's tick. STUB: does nothing. */
    public Result runOnce() {
        return new Result(false, 0, 0, List.of());
    }
}

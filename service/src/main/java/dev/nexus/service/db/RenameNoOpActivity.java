// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23, linda's ruling): in the one-row regime (the gate 3 latch row exists and the rename readiness is
 * complete) a registry rename has already moved the label, so the per-store renames (chash, taxonomy, telemetry, aspects, highlights)
 * change nothing and answer success with zero rows changed; rewriting their name columns would break the keys the one-row regime
 * depends on. Each such call is counted here, JVM-wide since boot (the shape of {@link RequestLedgerActivity}; the service has no
 * metrics registry), and logged at INFO with both labels.
 */
public final class RenameNoOpActivity {

    private static final Logger log = LoggerFactory.getLogger(RenameNoOpActivity.class);
    private static final AtomicLong NOOPS = new AtomicLong();

    private RenameNoOpActivity() {
    }

    /** One per-store rename answered as a no-op. {@code store} names the primitive, e.g. {@code chash}. */
    public static void record(String store, String oldLabel, String newLabel) {
        NOOPS.incrementAndGet();
        log.info("event=rename_noop_one_row store={} old={} new={} msg=\"one-row regime: the registry rename moved the label\"",
            store, oldLabel, newLabel);
    }

    /** @return the no-op renames since boot, all stores. */
    public static long total() {
        return NOOPS.get();
    }
}

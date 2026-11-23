// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The log hook for waits on a registry row's lock (RDR-229 P3.5a, linda's Q3 ruling): a verb's wait for its {@code FOR UPDATE}
 * ({@link CollectionTombstones#lockRows} calls it) and, when the label-writer side lands, a writer's wait behind a verb's
 * {@code FOR UPDATE}. Any wait over {@link #SLOW_MS} is one INFO event with the wait time, so a convoy of writers behind a long
 * rename, or a verb starved by writers, is visible.
 */
public final class RegistryLockWait {

    private static final Logger log = LoggerFactory.getLogger(RegistryLockWait.class);

    /** A wait longer than this is logged. */
    public static final long SLOW_MS = 1000;

    private RegistryLockWait() {
    }

    /**
     * @param role   {@code verb} or {@code writer}
     * @param names  the collection label(s) the lock covers
     * @param waitMs how long the lock took to get, or to give up
     * @param outcome {@code locked} or {@code timed_out}
     */
    public static void logIfSlow(String role, String tenant, String names, long waitMs, String outcome) {
        if (waitMs > SLOW_MS) {
            log.info("event=registry_row_lock_wait role={} outcome={} wait_ms={} tenant={} collections={}", role, outcome, waitMs, tenant, names);
        }
    }
}

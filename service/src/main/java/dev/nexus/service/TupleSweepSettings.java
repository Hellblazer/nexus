// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

/**
 * The tuple sweep's three settings (RDR-205 Phase 1 Step 5, bead nexus-em75s.5), each an env override of a default in
 * {@link NexusService}: pure resolution, so a test passes the env value. Moved out of {@code NexusService} (file-size
 * ceiling) unchanged.
 */
final class TupleSweepSettings {

    private TupleSweepSettings() {}

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_BATCH_SIZE_ENV}. */
    static int batchSize() {
        return batchSize(System.getenv(NexusService.TUPLE_SWEEP_BATCH_SIZE_ENV));
    }

    static int batchSize(String envValue) {
        return parsePositiveInt(envValue, NexusService.DEFAULT_TUPLE_SWEEP_BATCH_SIZE, NexusService.TUPLE_SWEEP_BATCH_SIZE_ENV);
    }

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV}. */
    static int maxBatchesPerTenant() {
        return maxBatchesPerTenant(System.getenv(NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV));
    }

    static int maxBatchesPerTenant(String envValue) {
        return parsePositiveInt(envValue, NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT,
                NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV);
    }

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS_ENV}. */
    static long wallClockBudgetSeconds() {
        return wallClockBudgetSeconds(System.getenv(NexusService.TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS_ENV));
    }

    static long wallClockBudgetSeconds(String envValue) {
        return parsePositiveLong(envValue, NexusService.DEFAULT_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS,
                NexusService.TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS_ENV);
    }

    private static int parsePositiveInt(String envValue, int defaultValue, String envName) {
        if (envValue == null || envValue.isBlank()) {
            return defaultValue;
        }
        int v;
        try {
            v = Integer.parseInt(envValue.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(envName + " must be a positive integer (got '" + envValue + "')");
        }
        if (v <= 0) {
            throw new IllegalArgumentException(envName + " must be positive (got " + v + ")");
        }
        return v;
    }

    private static long parsePositiveLong(String envValue, long defaultValue, String envName) {
        if (envValue == null || envValue.isBlank()) {
            return defaultValue;
        }
        long v;
        try {
            v = Long.parseLong(envValue.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(envName + " must be a positive integer (got '" + envValue + "')");
        }
        if (v <= 0) {
            throw new IllegalArgumentException(envName + " must be positive (got " + v + ")");
        }
        return v;
    }
}

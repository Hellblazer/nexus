// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

/**
 * The tuple sweep's environment bounds (RDR-205 Phase 1 Step 5, bead nexus-em75s.5): pure resolution of
 * {@link NexusService}'s {@code NX_TUPLE_SWEEP_*} variables to positive values, a blank or absent value meaning the default.
 * Moved out of {@link NexusService}, unchanged, so that class stays under its file-size ceiling (RDR-229 P3.5c, nexus-p2m1x.25).
 */
final class TupleSweepEnv {

    private TupleSweepEnv() { /* static */ }

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_BATCH_SIZE_ENV}. */
    static int resolveTupleSweepBatchSize() {
        return resolveTupleSweepBatchSize(System.getenv(NexusService.TUPLE_SWEEP_BATCH_SIZE_ENV));
    }

    static int resolveTupleSweepBatchSize(String envValue) {
        return parsePositiveInt(envValue, NexusService.DEFAULT_TUPLE_SWEEP_BATCH_SIZE, NexusService.TUPLE_SWEEP_BATCH_SIZE_ENV);
    }

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV}. */
    static int resolveTupleSweepMaxBatchesPerTenant() {
        return resolveTupleSweepMaxBatchesPerTenant(System.getenv(NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV));
    }

    static int resolveTupleSweepMaxBatchesPerTenant(String envValue) {
        return parsePositiveInt(envValue, NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT,
                NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV);
    }

    /** Testable, pure resolution — {@value NexusService#TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS_ENV}. */
    static long resolveTupleSweepWallClockBudgetSeconds() {
        return resolveTupleSweepWallClockBudgetSeconds(System.getenv(NexusService.TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS_ENV));
    }

    static long resolveTupleSweepWallClockBudgetSeconds(String envValue) {
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

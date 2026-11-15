// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4lnn1.1.10: the canary's exact leg (the recall oracle, index scans off) ran 25.6 s cold on a production copy of
 * code__1-87 (P2.4 fork, 2026-11-15) against the serving search bound of 30 s, and the cloud's canary failed with
 * stage=statement after a boot and after a REINDEX. The HNSW leg is the serving statement and keeps the serving bound;
 * the exact leg gets the run's own limit.
 */
class PciCanaryPolicyTimeoutTest {

    @Test
    void theHnswLegKeepsTheServingSearchBound() {
        assertThat(PciCanaryPolicy.statementTimeoutMs(false).getAsInt())
            .isEqualTo(PgSession.startupSearchStatementTimeoutMs());
    }

    @Test
    void theExactLegGetsTheRunLimit() {
        assertThat(PciCanaryPolicy.statementTimeoutMs(true).getAsInt())
            .isEqualTo((int) PciCanaryPolicy.RUN_LIMIT.toMillis())
            .isGreaterThan(PgSession.startupSearchStatementTimeoutMs());
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.db.CatalogRepository;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0uuit/sybbh crit-fix critique 2026-08-19 (code-review-expert, round 2):
 * {@link VectorHandler#clampSampleLimit(int)} — the line that clamps
 * {@code handleGcQuarantineOrphans}'s caller-supplied {@code sample_limit} —
 * had zero direct coverage. {@code CatalogGcAuditProducersTest
 * #quarantineOrphans_oversizedSampleLimitRequest_clampsAndFlagsTruncation}
 * calls {@code PgVectorRepository#quarantineOrphans} directly, bypassing this
 * handler entirely, and only proves the SQL-side clamp (catalog-033-2) — a
 * revert of {@code clampSampleLimit}'s own body would not be caught by any
 * prior test, since the SQL function redundantly re-clamps at the identical
 * ceiling regardless of what this method does.
 *
 * <p>A true HTTP-round-trip test cannot distinguish "clamped by
 * {@code clampSampleLimit}" from "clamped by SQL" either — both cap at the
 * SAME {@link CatalogRepository#GC_AUDIT_MAX_CHASHES}, so any oversized
 * request observed via the {@code /gc/quarantine-orphans} response or the
 * resulting {@code gc_audit} row would look identical whether or not this
 * method's body is intact. This is therefore the "smallest unit that
 * includes the clamp line" per the remediation instruction: a pure,
 * dependency-free unit test of the extracted static method itself.
 */
class VectorHandlerSampleLimitClampTest {

    @Test
    void oversizedRequest_clampsToTheMaxChashesCeiling() {
        assertThat(VectorHandler.clampSampleLimit(999_999))
            .isEqualTo(CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }

    @Test
    void requestWithinBound_isPassedThroughUnchanged() {
        assertThat(VectorHandler.clampSampleLimit(20)).isEqualTo(20);
    }

    @Test
    void requestExactlyAtTheCeiling_isUnchanged() {
        assertThat(VectorHandler.clampSampleLimit(CatalogRepository.GC_AUDIT_MAX_CHASHES))
            .isEqualTo(CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }

    @Test
    void requestOneOverTheCeiling_clampsDownByExactlyOne() {
        assertThat(VectorHandler.clampSampleLimit(CatalogRepository.GC_AUDIT_MAX_CHASHES + 1))
            .isEqualTo(CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }

    @Test
    void negativeRequest_isNotFlooredByThisMethodAlone() {
        // Documents the UPPER-BOUND-ONLY contract in clampSampleLimit's own
        // javadoc: unlike the SQL side's LEAST(GREATEST(...,0),5000), this
        // method does not floor a negative value to 0. Safe end-to-end only
        // because nexus.gc_quarantine_orphans (catalog-033-2) floors it
        // independently before the value reaches LIMIT.
        assertThat(VectorHandler.clampSampleLimit(-1)).isEqualTo(-1);
    }
}

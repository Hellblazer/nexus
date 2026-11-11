// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminDdlSession;
import org.junit.jupiter.api.Test;

/** nexus-f3yqd: the failure outcomes of the chash VALIDATE job (no database). */
class ValidateConstraintsJobsTest {

    @Test
    void aSessionEndedUnderTheJob_isA503ConnectionLost_aRefusalKeepsItsOwnStatus_andAnythingElseIsA500() {
        var ended = ValidateConstraintsJobs.failure(null, new AdminDdlSession.Refused(500, "ddl_failed", "x", "57P01"));
        assertThat(ended.httpStatus()).isEqualTo(503);
        assertThat(ended.body().get("reason")).isEqualTo("connection_lost");
        var lost = ValidateConstraintsJobs.failure(null, new AdminDdlSession.Refused(500, "ddl_failed", "x", "08006"));
        assertThat(lost.body().get("reason")).isEqualTo("connection_lost");

        var busy = ValidateConstraintsJobs.failure(null, new AdminDdlSession.Refused(409, "constraint_run_in_progress", "held"));
        assertThat(busy.httpStatus()).isEqualTo(409);
        assertThat(busy.body().get("reason")).isEqualTo("constraint_run_in_progress");

        var other = ValidateConstraintsJobs.failure(null, new IllegalStateException("secret tenant data"));
        assertThat(other.httpStatus()).isEqualTo(500);
        assertThat(other.body().get("error").toString()).doesNotContain("secret");
    }

    /**
     * A tenant reset scrubs the tenant out of a finished job's refused steps (its count and the example chashes), and leaves
     * another tenant's. Kills: an evictor that evicts nothing of the step, so a reset tenant's chash outlives it in the status.
     */
    @Test
    void forgettingATenant_scrubsItsCountAndExamplesFromRefusedSteps_andLeavesOthers() {
        var jobs = ValidateConstraintsJobs.registry(System::currentTimeMillis);
        var progress = new ValidateConstraintsJobs.Progress(false);
        progress.stepped(new dev.nexus.service.db.ChashConstraintValidator.Step("nexus.catalog_document_chunks",
            "catalog_document_chunks_chash_octet_check", dev.nexus.service.db.ChashConstraintValidator.Outcome.REFUSED, "bad_rows",
            3L, false, java.util.List.of("aa"), "unavailable", java.util.Map.of("t-a", 2L, "t-b", 1L)));
        assertThat(progress.forgetTenant("t-a")).isEqualTo(1);
        assertThat(progress.forgetTenant("t-a")).as("idempotent").isZero();
        @SuppressWarnings("unchecked")
        var refused = (java.util.List<java.util.Map<String, Object>>) progress.view().get("refused");
        assertThat(refused.get(0).get("example_chashes")).isEqualTo(java.util.List.of());
        assertThat(refused.get(0).get("tenants").toString()).doesNotContain("t-a").contains("t-b");
        assertThat(refused.get(0).get("bad_rows")).as("the reset tenant's count left the total").isEqualTo(1L);
        assertThat(ValidateConstraintsJobs.forgetTenant(jobs, "t-a")).as("an empty registry has nothing to scrub").isZero();
        jobs.shutdown();
    }

    /**
     * A refused step from the admin pre-check names no tenant, and its example chashes may be any tenant's: every reset clears
     * them (the count stays, it is not attributable). Kills: a scrub that only touches steps naming the reset tenant.
     */
    @Test
    void aRefusedStepThatNamesNoTenant_losesItsExampleChashesOnAnyReset() {
        var progress = new ValidateConstraintsJobs.Progress(false);
        progress.stepped(new dev.nexus.service.db.ChashConstraintValidator.Step("nexus.chunks_m1_t_1", "chunks_chash_octet_check",
            dev.nexus.service.db.ChashConstraintValidator.Outcome.REFUSED, "bad_rows", 2L, false,
            java.util.List.of("bb", "cc"), "bad_rows", java.util.Map.of()));
        assertThat(progress.forgetTenant("any-tenant")).isEqualTo(1);
        @SuppressWarnings("unchecked")
        var refused = (java.util.List<java.util.Map<String, Object>>) progress.view().get("refused");
        assertThat(refused.get(0).get("example_chashes")).isEqualTo(java.util.List.of());
        assertThat(refused.get(0).get("bad_rows")).as("the count is not attributable and stays").isEqualTo(2L);
        assertThat(progress.forgetTenant("any-tenant")).as("a second reset has nothing left").isZero();
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.ChashConstraintValidator.Detail;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.db.LegacyChunkSurvey;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * nexus-f3yqd: how a refused step's per-reason account reaches the job status, and how a tenant reset treats it, without a
 * database: the refusal's JSON for a step with no account, and the scrub of an account that names a tenant the step's own
 * per-tenant counts do not.
 */
class ValidateRefusalDetailTest {

    private static Step refused(String table, Map<String, Long> tenantRows, List<String> examples, Detail detail) {
        return new Step(table, "c", Outcome.REFUSED, "bad_rows", 2L, false, examples, "unavailable", tenantRows, detail);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> refusal(Step s) {
        var p = new ValidateConstraintsJobs.Progress(false);
        p.stepped(s);
        return ((List<Map<String, Object>>) p.view().get("refused")).get(0);
    }

    /**
     * A refusal whose rows the session could not read in full (no account) says so, with no reasons and no route; one with an
     * account carries it. Kills: an absent account passing as "no reasons to report"; a route in the refusal.
     */
    @Test
    void aRefusalWithNoAccount_saysReasonsUnavailable_andNamesNoRoute() {
        Map<String, Object> none = refusal(refused("nexus.chunks_m1_t_1", Map.of(), List.of(), null));
        assertThat(none).containsEntry("reasons_unavailable", true).doesNotContainKeys("reasons", "chunks", "documents", "reasons_capped");
        assertThat(none.toString().toLowerCase()).doesNotContain("next_step", "rekey", "/v1/");

        Detail d = new Detail(Map.of("no_text", 2L), true, List.of(), false, Map.of(), Set.of("t1"), false);
        Map<String, Object> some = refusal(refused("nexus.catalog_document_chunks", Map.of("t1", 2L), List.of(), d));
        assertThat(some).containsEntry("reasons", Map.of("no_text", 2L)).containsEntry("reasons_capped", true);
        assertThat(some).doesNotContainKey("reasons_unavailable");
    }

    /**
     * The account names a tenant (t2) that the step's own per-tenant counts and example chashes do not: a reset of t2 still drops
     * it, and a reset of an unrelated tenant leaves it. A leaf's account (counts of any tenant's rows) is dropped by any reset.
     * Kills: a scrub keyed only on tenantRows and example chashes (the account of t2's rows would stay for twelve hours).
     */
    @Test
    void aTenantResetDropsAnAccountThatNamesTheTenant_evenWhenTheStepsOwnCountsDoNot() {
        var p = new ValidateConstraintsJobs.Progress(false);
        Detail manifest = new Detail(Map.of("no_text", 1L), false, List.of(), false,
            Map.of("no_text", new LegacyChunkSurvey.DocList(List.of(new LegacyChunkSurvey.DocRef("t2", "1.1", "t2's doc")), false)), Set.of("t2"), false);
        p.stepped(refused("nexus.catalog_document_chunks", Map.of(), List.of(), manifest));   // no tenantRows, no examples
        Detail leaf = new Detail(Map.of("no_text", 1L), false, List.of(), false, Map.of(), Set.of("t3"), true);
        p.stepped(refused("nexus.chunks_m1_t_3", Map.of(), List.of(), leaf));

        assertThat(p.forgetTenant("unrelated")).as("only the leaf's any-tenant account is dropped by an unrelated reset").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var after = (List<Map<String, Object>>) p.view().get("refused");
        assertThat(after.get(0)).as("the manifest's account names t2 only").containsKey("reasons");
        assertThat(after.get(1)).containsEntry("reasons_unavailable", true);

        assertThat(p.forgetTenant("t2")).as("t2's account goes").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var gone = (List<Map<String, Object>>) p.view().get("refused");
        assertThat(gone.get(0)).containsEntry("reasons_unavailable", true).doesNotContainKeys("reasons", "documents");
    }
}

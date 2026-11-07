// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.vectors.LeafBloatSweep;
import dev.nexus.service.vectors.PciMaintenanceStatus;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 Phase 1, Sam's ruling of 2026-11-06 (ask pci-names-on-status, option b): the per-index canary list left the
 * unauthenticated {@code GET /v1/status} and is an additive key, {@code canary_indexes}, on the operator-only
 * {@code GET /v1/admin/chunks-leaves}. Hermetic: the body is built without a server. The auth rules of the route are
 * pinned by {@code ChunksLeavesRouteTest}.
 */
class ChunksLeavesHandlerBodyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Instant NOW = Instant.parse("2026-10-22T12:00:00Z");
    private static final Instant MEASURED_AT = Instant.parse("2026-10-20T08:01:10.456Z");

    private static final LeafBloatSweep.Status SWEEP = new LeafBloatSweep.Status(true, NOW, null, 0, false, List.of());

    private static PciMaintenanceStatus.Entry entry(String index, String state, String reason, int queries, Double min,
                                                    Double mean, int top1, String trigger, String anchor,
                                                    Double sizeRatio, Double deletes) {
        return new PciMaintenanceStatus.Entry(index, state, reason, queries, min, mean, top1, trigger, MEASURED_AT,
            anchor, sizeRatio, deletes);
    }

    /** A green, a red and an unmeasured entry, given in the order the engine does not use. */
    private static PciMaintenanceStatus canaryHistory() {
        return PciMaintenanceStatus.phase1(List.of(
            entry("pci_3f9c0a1b2c3d4e5f6a7b8c9d", "green", null, 10, 0.9834, 0.9981, 0, "after_build", "built",
                1.0712, 0.0312),
            entry("pci_bbbbbbbbbbbbbbbbbbbbbbbb", "unmeasured", "no_queries", 2, null, null, 0, "daily", null, null,
                null),
            entry("pci_aaaaaaaaaaaaaaaaaaaaaaaa", "red", null, 10, 0.82, 0.9, 2, "vacuum", "adopted", null, 0.25)));
    }

    private static JsonNode render(PciMaintenanceStatus maintenance) throws Exception {
        return MAPPER.readTree(MAPPER.writeValueAsString(ChunksLeavesHandler.body(SWEEP, 300L, maintenance)));
    }

    private static List<String> keys(JsonNode o) {
        var names = new ArrayList<String>();
        o.fieldNames().forEachRemaining(names::add);
        return names;
    }

    @Test
    void theKeyIsAbsentWhenNoCanarySourceIsWired_andTheOtherKeysAreUnchanged() throws Exception {
        JsonNode without = render(null);
        assertThat(without.has("canary_indexes")).as("unwired: cannot tell, not an empty list").isFalse();
        assertThat(keys(without)).containsExactly("last_read_at", "expired", "sweep_seconds", "ever_read",
            "consecutive_failures", "leaves_total", "needing_attention", "leaves");

        JsonNode with = render(canaryHistory());
        assertThat(keys(with)).containsExactly("last_read_at", "expired", "sweep_seconds", "ever_read",
            "consecutive_failures", "leaves_total", "needing_attention", "leaves", "canary_indexes");
        var stripped = with.deepCopy();
        ((com.fasterxml.jackson.databind.node.ObjectNode) stripped).remove("canary_indexes");
        assertThat(stripped).as("the new key changes no existing key").isEqualTo(without);
    }

    @Test
    void aWiredSourceWithNothingMeasuredAnswersAnEmptyList() throws Exception {
        JsonNode body = render(PciMaintenanceStatus.phase1(List.of()));
        assertThat(body.get("canary_indexes").isArray()).isTrue();
        assertThat(body.get("canary_indexes")).isEmpty();
    }

    @Test
    void eachEntryHasExactlyTheTwelveDocumentedKeys_redFirstThenUnmeasuredThenGreen() throws Exception {
        JsonNode list = render(canaryHistory()).get("canary_indexes");

        assertThat(list).hasSize(3);
        assertThat(list.get(0).get("state").asText()).isEqualTo("red");
        assertThat(list.get(1).get("state").asText()).isEqualTo("unmeasured");
        assertThat(list.get(2).get("state").asText()).isEqualTo("green");
        for (JsonNode e : list) {
            assertThat(keys(e)).containsExactly("index", "state", "reason", "queries", "min_recall", "mean_recall",
                "top1_misses", "trigger", "measured_at", "anchor", "size_ratio", "deletes_since_build");
            assertThat(e.get("index").asText()).matches("^pci_[0-9a-f]{24}$");
            assertThat(e.get("measured_at").asText()).as("whole seconds, like the other times")
                .isEqualTo("2026-10-20T08:01:10Z");
        }
    }

    @Test
    void numbersAreRoundedAsTheStatusRouteRoundedThem_andAnUnmeasuredEntryCarriesItsQueriesAndNoRecall()
            throws Exception {
        JsonNode list = render(canaryHistory()).get("canary_indexes");

        JsonNode green = list.get(2);
        assertThat(green.get("min_recall").decimalValue()).isEqualByComparingTo("0.983");
        assertThat(green.get("mean_recall").decimalValue()).isEqualByComparingTo("0.998");
        assertThat(green.get("size_ratio").decimalValue()).isEqualByComparingTo("1.07");
        assertThat(green.get("deletes_since_build").decimalValue()).isEqualByComparingTo("0.031");
        assertThat(green.get("anchor").asText()).isEqualTo("built");

        JsonNode unmeasured = list.get(1);
        assertThat(unmeasured.get("reason").asText()).isEqualTo("no_queries");
        assertThat(unmeasured.get("queries").asInt()).as("the ring's count, not zero").isEqualTo(2);
        assertThat(unmeasured.get("min_recall").isNull()).isTrue();
        assertThat(unmeasured.get("mean_recall").isNull()).isTrue();
        assertThat(unmeasured.get("anchor").isNull()).isTrue();
        assertThat(unmeasured.get("size_ratio").isNull()).isTrue();
    }

    @Test
    void aValueThatIsNotFiniteIsNull() throws Exception {
        var status = PciMaintenanceStatus.phase1(List.of(
            entry("pci_" + "c".repeat(24), "green", null, 10, Double.NaN, Double.POSITIVE_INFINITY, 0, "daily", "built",
                Double.NaN, Double.NEGATIVE_INFINITY)));
        JsonNode e = render(status).get("canary_indexes").get(0);
        assertThat(e.get("min_recall").isNull()).isTrue();
        assertThat(e.get("mean_recall").isNull()).isTrue();
        assertThat(e.get("size_ratio").isNull()).isTrue();
        assertThat(e.get("deletes_since_build").isNull()).isTrue();
    }

    @Test
    void theListIsCappedAndOrderedByTheStatusObject() throws Exception {
        var all = new ArrayList<PciMaintenanceStatus.Entry>();
        for (int n = 0; n < 80; n++) {
            all.add(entry(String.format("pci_%024x", n), n % 10 == 0 ? "red" : "green", null, 10, 0.99, 0.99,
                n % 10 == 0 ? 1 : 0, "daily", "built", 1.0, 0.0));
        }
        JsonNode list = render(PciMaintenanceStatus.phase1(all)).get("canary_indexes");

        assertThat(list).hasSize(64);
        for (int i = 0; i < 8; i++) {
            assertThat(list.get(i).get("state").asText()).isEqualTo("red");
        }
        assertThat(list.get(8).get("state").asText()).isEqualTo("green");
    }
}

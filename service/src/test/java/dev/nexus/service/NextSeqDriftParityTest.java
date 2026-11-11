// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/owners/next_seq_drift} agrees with the client's allocator row
 * ({@code nexus.health._check_next_seq_drift}, which counts tombstoned children as the engine's floor does) on every case
 * of ONE shared table, {@code next-seq-drift-cases.json}; {@code tests/catalog/test_next_seq_drift_cases.py} runs the same
 * file through the real row. Each case is seeded (owners, live and tombstoned documents), the route must list exactly the
 * table's drifted owners, and the sweep that follows must floor exactly those to exactly the same mark.
 */
class NextSeqDriftParityTest extends IndexRunCensusTestBase {

    private static final String RESOURCE = "next-seq-drift-cases.json";

    static Stream<String> caseIds() throws Exception {
        try (var in = NextSeqDriftParityTest.class.getResourceAsStream("/" + RESOURCE)) {
            var ids = new ArrayList<String>();
            new com.fasterxml.jackson.databind.ObjectMapper().readTree(in).get("cases").forEach(s -> ids.add(s.get("id").asText()));
            return ids.stream();
        }
    }

    @Test
    void theTableIsReadWhole() throws Exception {
        JsonNode cases = json(RESOURCE).get("cases");
        assertThat(cases.size()).isGreaterThanOrEqualTo(10);
        int drifted = 0, healthy = 0;
        for (JsonNode c : cases) { if (c.get("expect").get("drifted").size() > 0) drifted++; else healthy++; }
        assertThat(drifted).isGreaterThanOrEqualTo(6);
        assertThat(healthy).isGreaterThanOrEqualTo(3);
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("caseIds")
    void theRouteListsExactlyTheTablesDriftedOwners_andTheSweepFloorsThem(String id) throws Exception {
        JsonNode sc = null;
        for (JsonNode c : json(RESOURCE).get("cases")) if (c.get("id").asText().equals(id)) sc = c;
        assertThat(sc).isNotNull();

        for (JsonNode o : sc.get("owners")) {
            String prefix = o.get("prefix").asText();
            repo.upsertOwner(TENANT, Map.of("tumbler_prefix", prefix, "name", "owner " + prefix, "owner_type", "repo"));
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_OWNERS).set(CATALOG_OWNERS.NEXT_SEQ, o.get("next_seq").asLong())
                    .set(CATALOG_OWNERS.DEACTIVATED_AT, o.get("deactivated").asBoolean() ? OffsetDateTime.parse("2026-10-01T00:00:00+00:00") : null)
                    .where(CATALOG_OWNERS.TENANT_ID.eq(TENANT).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))).execute();
            }
        }
        for (JsonNode t : sc.get("live")) insert(TENANT, Doc.of(t.asText()));
        for (JsonNode t : sc.get("tombstoned")) insert(TENANT, Doc.of(t.asText()).tombstoned());

        JsonNode want = sc.get("expect");
        var body = drift("");
        assertThat(body).as(id).containsEntry("checked", want.get("checked").asInt())
            .containsEntry("drifted", want.get("drifted").size()).containsEntry("capped", false);
        var got = new ArrayList<List<Object>>();
        for (var e : list(body, "examples")) got.add(List.of(e.get("owner"), ((Number) e.get("next_seq")).longValue(), ((Number) e.get("highest_child_seq")).longValue()));
        var expected = new ArrayList<List<Object>>();
        for (JsonNode d : want.get("drifted")) expected.add(List.of(d.get("owner").asText(), d.get("next_seq").asLong(), d.get("highest_child_seq").asLong()));
        assertThat(got).as(id + " drifted owners").containsExactlyElementsOf(expected);

        // the sweep floors the same owners to the same mark: one computation
        var resp = post(TOKEN, TENANT, "/v1/catalog/owners/sweep_next_seq_drift", "{}");
        var swept = mapper.readValue(resp.body(), MAP_T);
        var healed = new ArrayList<List<Object>>();
        for (var h : list(swept, "owners")) healed.add(List.of(h.get("tumbler_prefix"), ((Number) h.get("next_seq")).longValue(), ((Number) h.get("high_water")).longValue()));
        assertThat(healed).as(id + " healed by the sweep").containsExactlyInAnyOrderElementsOf(expected);
        assertThat(drift("")).containsEntry("drifted", 0);
    }
}

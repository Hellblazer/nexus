// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: each bucket of {@code GET /v1/catalog/index-run/census} agrees with the client's doctor rows on every
 * scenario of ONE shared table, {@code index-run-census-cases.json}. {@code tests/catalog/test_index_run_census_cases.py}
 * reads the same file and runs the real rows ({@code _check_stale_indexing_runs}, {@code _check_failed_runs_hidden_chunks})
 * over the same documents, so a change to either side's rule that the other does not follow fails one of the two.
 *
 * <p>Each scenario is seeded into an empty tenant (documents, registered collections, chunk rows carrying a content hash,
 * the manifest rows of the documents that keep a manifest) and the route's three buckets must equal the table's
 * {@code expect}. The fixture's clock is fixed; the route runs on the real one, so each {@code index_started_at} is shifted
 * by the same amount to keep its age (a document at exactly six hours is therefore a hair over six hours here; the
 * inclusive edge itself is {@link IndexRunCensusRouteTest#theSixHourEdgeIsInclusive}'s).
 */
class IndexRunCensusParityTest extends IndexRunCensusTestBase {

    private static final String RESOURCE = "index-run-census-cases.json";

    static Stream<String> scenarioIds() throws Exception {
        try (var in = IndexRunCensusParityTest.class.getResourceAsStream("/" + RESOURCE)) {
            var ids = new ArrayList<String>();
            new com.fasterxml.jackson.databind.ObjectMapper().readTree(in).get("scenarios").forEach(s -> ids.add(s.get("id").asText()));
            return ids.stream();
        }
    }

    @Test
    void theTableIsReadWhole() throws Exception {
        // A table that lost its rows would pass the parity test below with nothing to check.
        JsonNode table = json(RESOURCE);
        assertThat(table.get("constants").get("stale_threshold_s").asLong()).isEqualTo(21_600L);
        int stale = 0, fence = 0, failed = 0;
        for (JsonNode s : table.get("scenarios")) {
            if (s.get("expect").has("stale")) stale++;
            if (s.get("expect").has("fence")) fence++;
            if (s.get("expect").has("failed_hidden")) failed++;
        }
        assertThat(stale).isGreaterThanOrEqualTo(3);
        assertThat(fence).isGreaterThanOrEqualTo(7);
        assertThat(failed).isGreaterThanOrEqualTo(4);
    }

    @Test
    void theRouteEchoesTheConstantsTheTableHoldsTheClientTo() throws Exception {
        JsonNode constants = json(RESOURCE).get("constants");
        var body = census("");
        assertThat(((Number) body.get("threshold_s")).longValue()).isEqualTo(constants.get("stale_threshold_s").asLong());
        assertThat(body.get("floor")).isEqualTo(constants.get("floor").asText());
    }

    private static String name(JsonNode doc) {
        String uri = doc.get("source_uri").asText();
        return uri.isEmpty() ? doc.get("tumbler").asText() : uri;
    }

    private String collectionName(String key) {
        return switch (key) {
            case "" -> "";
            case "c1" -> C1;
            case "c2" -> C2;
            default -> "knowledge__census-" + key + "__" + MODEL + "__v1";   // not registered
        };
    }

    private static String asJson(JsonNode n) {
        return n == null || n.isNull() ? "{}" : n.toString();
    }

    private static OffsetDateTime time(JsonNode n) {
        return n == null || n.isNull() ? null : OffsetDateTime.ofInstant(Instant.parse(n.asText()), ZoneOffset.UTC);
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("scenarioIds")
    void theRoutesBucketsEqualTheTablesExpectation(String id) throws Exception {
        JsonNode table = json(RESOURCE);
        JsonNode sc = null;
        for (JsonNode s : table.get("scenarios")) if (s.get("id").asText().equals(id)) sc = s;
        assertThat(sc).isNotNull();
        // the fixture's now, moved to the real one
        Duration shift = Duration.between(Instant.parse(table.get("now").asText()), Instant.now());

        if (sc.has("collections")) for (JsonNode c : sc.get("collections")) collection(TENANT, collectionName(c.asText()));
        if (sc.has("chunk_groups")) {
            for (JsonNode g : sc.get("chunk_groups")) {
                for (int i = 0; i < g.get("count").asInt(); i++) {
                    chunk(TENANT, collectionName(g.get("collection").asText()), "{\"content_hash\":\"" + g.get("hash").asText() + "\"}");
                }
            }
        }
        Map<String, String> tumblerName = new HashMap<>();
        for (JsonNode d : sc.get("documents")) {
            String coll = collectionName(d.get("collection").asText());
            var doc = Doc.of(d.get("tumbler").asText()).type(d.get("content_type").asText()).uri(d.get("source_uri").asText())
                .collection(coll).chunks(d.get("chunk_count").asInt()).fenceHash(d.get("index_content_hash").asText())
                .run(d.get("index_run_id").asText()).meta(asJson(d.get("meta")))
                .state(d.get("index_state").isNull() ? null : d.get("index_state").asText())
                .started(time(d.get("index_started_at")) == null ? null : time(d.get("index_started_at")).plus(shift))
                .indexed(time(d.get("indexed_at")));
            doc.filePath = d.get("file_path").asText();
            if (d.get("deleted").asBoolean()) doc.tombstoned();
            insert(TENANT, doc);
            tumblerName.put(d.get("tumbler").asText(), name(d));
            for (int p = 0; p < d.get("manifest_rows").asInt(); p++) {
                manifest(TENANT, doc.tumbler, p, coll, chunk(TENANT, coll, "{}"));
            }
        }

        var body = census("?limit=100");
        JsonNode expect = sc.get("expect");
        if (expect.has("stale")) {
            JsonNode want = expect.get("stale");
            var stale = bucket(body, "stale");
            assertThat(stale).as(id + " stale").containsEntry("count", want.get("count").asInt())
                .containsEntry("notes", want.get("notes").asInt()).containsEntry("capped", false);
            var names = new ArrayList<String>();
            for (var e : list(stale, "examples")) names.add(tumblerName.get((String) e.get("tumbler")));
            assertThat(names).as(id + " stale names").containsExactlyElementsOf(textList(want.get("names")));
        }
        if (expect.has("fence")) {
            JsonNode want = expect.get("fence");
            var fence = bucket(body, "fence");
            assertThat(fence).as(id + " fence")
                .containsEntry("checked", want.get("checked").asInt())
                .containsEntry("reported_null", want.get("reported_null").asInt())
                .containsEntry("undated_reported_null", want.get("undated_reported_null").asInt())
                .containsEntry("floor_candidates", want.get("floor_candidates").asInt());
            var anchor = bucket(fence, "anchor");
            assertThat(anchor.get("kind")).as(id + " anchor kind").isEqualTo(text(want.get("anchor_kind")));
            assertThat(anchor.get("at")).as(id + " anchor at").isEqualTo(text(want.get("anchor_at")));
            assertThat(anchor.get("earliest_stamped_at")).as(id + " earliest").isEqualTo(text(want.get("earliest_stamped_at")));
            assertThat(anchor.get("proven_at")).as(id + " proven").isEqualTo(text(want.get("proven_at")));
            var post = bucket(fence, "post_anchor");
            var names = new ArrayList<String>();
            for (var e : list(post, "examples")) names.add(tumblerName.get((String) e.get("tumbler")));
            assertThat(post).as(id + " post_anchor").containsEntry("count", want.get("post_anchor").size()).containsEntry("capped", false);
            assertThat(names).as(id + " post_anchor names").containsExactlyElementsOf(textList(want.get("post_anchor")));
        }
        if (expect.has("failed_hidden")) {
            JsonNode want = expect.get("failed_hidden");
            var fh = bucket(body, "failed_hidden");
            assertThat(bucket(fh, "failed")).as(id + " failed").containsEntry("count", want.get("failed").asInt()).containsEntry("capped", false);
            assertThat(fh).as(id + " hidden").containsEntry("count", want.get("count").asInt()).containsEntry("capped", false);
            var tumblers = new ArrayList<String>();
            for (var e : list(fh, "examples")) tumblers.add((String) e.get("tumbler"));
            assertThat(tumblers).as(id + " hidden documents").containsExactlyElementsOf(textList(want.get("hidden")));
        }
    }

    private static String text(JsonNode n) {
        return n == null || n.isNull() ? null : n.asText();
    }

    private static List<String> textList(JsonNode n) {
        var out = new ArrayList<String>();
        n.forEach(x -> out.add(x.asText()));
        return out;
    }
}
